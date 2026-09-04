"""Polite HTTP fetching and HTML-to-text extraction.

Scope, stated plainly: this fetches **public pages that serve their content to a
normal request**. It identifies itself, rate-limits, and honours robots.txt.

It deliberately does NOT defeat bot detection. If a host answers with a
verification/CAPTCHA interstitial (WeChat's ``环境异常`` page being the case that
matters here), the fetch is reported as blocked and the pipeline records the
source as ``URL_ONLY``. Prompt 2 forbids circumventing those controls, and a
label produced by circumvention would not be trustworthy anyway.
"""

from __future__ import annotations

import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

from .utils import get_logger

DEFAULT_UA = "china-research/0.1 (research source-collection tool)"

# Interstitials that mean "we served you a wall, not the article".
_BLOCK_MARKERS = (
    "环境异常",
    "完成验证后即可继续访问",
    "去验证",
    "请输入验证码",
    "访问验证",
    "滑动验证",
    "captcha",
    "security check",
    "unusual traffic",
)

# A WAF/JS challenge often returns plenty of "text" — base64 blobs, JSON
# tokens — which passes a naive length check and would be stored as evidence.
# Measured: xueqiu.com returned 34,071 chars of `{"_waf_...}` payload for four
# different article URLs, identical in length and near-identical in content.
_WAF_MARKERS = ("_waf_", "__cf_", "cf-challenge", "jschl", "slider_verify",
                "_acw_sc_", "x5secdata", "distil_")

_STRIP_TAGS = ("script", "style", "noscript", "iframe", "svg", "form", "nav",
               "header", "footer", "aside")

# Containers Chinese CMSes typically use for the article body.
_BODY_HINTS = (
    "article", "main",
    '[class*="article-content"]', '[class*="article_content"]',
    '[class*="artical"]', '[class*="content-detail"]', '[class*="detail-content"]',
    '[class*="rich_media_content"]', '[id*="content"]', '[class*="content"]',
)

_DATE_RE = re.compile(
    r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})"
    r"(?:\s*日)?(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)


class FetchBlocked(RuntimeError):
    """The host served a verification wall instead of content."""


class FetchError(RuntimeError):
    """Network/HTTP failure."""


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    title: Optional[str]
    text: Optional[str]
    published: Optional[str]
    html_bytes: int
    blocked: bool = False
    block_reason: Optional[str] = None
    links: list[tuple[str, str]] = field(default_factory=list)  # (text, absolute url)


@dataclass
class FetchPolicy:
    user_agent: str = DEFAULT_UA
    delay_seconds: float = 1.5
    timeout_seconds: int = 30
    max_bytes: int = 3_000_000
    respect_robots: bool = True


class Fetcher:
    def __init__(self, policy: Optional[FetchPolicy] = None) -> None:
        self.policy = policy or FetchPolicy()
        self.log = get_logger()
        self._last_request_at = 0.0
        self._robots: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        try:
            import requests

            self._session = requests.Session()
            self._session.headers.update({
                "User-Agent": self.policy.user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            })
        except ImportError as exc:  # pragma: no cover
            raise FetchError(f"requests is not installed: {exc}") from exc

    # -- politeness -------------------------------------------------------
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.policy.delay_seconds:
            time.sleep(self.policy.delay_seconds - elapsed)
        self._last_request_at = time.monotonic()

    def _allowed(self, url: str) -> bool:
        if not self.policy.respect_robots:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(f"{origin}/robots.txt")
            try:
                parser.read()
                self._robots[origin] = parser
            except Exception:
                # No robots.txt (or unreadable) means nothing is disallowed.
                self._robots[origin] = None
        parser = self._robots[origin]
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.policy.user_agent, url)
        except Exception:
            return True

    # -- fetch ------------------------------------------------------------
    def fetch(self, url: str) -> FetchResult:
        if not self._allowed(url):
            raise FetchBlocked(f"robots.txt disallows fetching {url}")

        self._throttle()
        self.log.debug("fetching %s", url)
        try:
            response = self._session.get(
                url, timeout=self.policy.timeout_seconds, allow_redirects=True
            )
        except Exception as exc:
            raise FetchError(f"could not fetch {url}: {exc}") from exc

        if response.status_code >= 400:
            raise FetchError(f"{url} returned HTTP {response.status_code}")

        raw = response.content[: self.policy.max_bytes]
        if not response.encoding or response.encoding.lower() == "iso-8859-1":
            response.encoding = response.apparent_encoding or "utf-8"
        html = raw.decode(response.encoding or "utf-8", errors="replace")

        marker = self._blocked_by(html)
        if not marker:
            marker = next((w for w in _WAF_MARKERS if w in html[:4000]), None)
        if marker:
            return FetchResult(
                url=url, final_url=response.url, status=response.status_code,
                title=None, text=None, published=None, html_bytes=len(raw),
                blocked=True,
                block_reason=f"host served a verification interstitial ({marker!r})",
            )

        title, text, published, links = extract(html, base_url=response.url)

        # Even without a marker, verify the extraction actually looks like
        # article prose. A challenge page can slip past the markers and would
        # otherwise be stored as evidence.
        if text:
            problem = looks_unusable(text)
            if problem:
                return FetchResult(
                    url=url, final_url=response.url, status=response.status_code,
                    title=title, text=None, published=published,
                    html_bytes=len(raw), links=links,
                    blocked=True,
                    block_reason=f"extracted text is not article prose ({problem})",
                )

        return FetchResult(
            url=url, final_url=response.url, status=response.status_code,
            title=title, text=text, published=published,
            html_bytes=len(raw), links=links,
        )

    @staticmethod
    def _blocked_by(html: str) -> Optional[str]:
        head = html[:8000].lower()
        for marker in _BLOCK_MARKERS:
            if marker.lower() in head:
                return marker
        return None


def looks_unusable(text: str) -> Optional[str]:
    """Return a reason if ``text`` is clearly not readable article content.

    Guards against anti-bot payloads that are long enough to pass a length
    check. Two signals, both cheap: almost no CJK in a page we reached through
    a Chinese-language search, and a high density of base64/token characters.
    """
    sample = text[:4000]
    if not sample.strip():
        return "empty"

    letters = [c for c in sample if not c.isspace()]
    if not letters:
        return "whitespace only"

    cjk = sum(1 for c in letters if "\u4e00" <= c <= "\u9fff")
    cjk_ratio = cjk / len(letters)

    # base64/JWT-ish payloads: long runs of [A-Za-z0-9+/=] with no spaces.
    longest_token = max((len(t) for t in re.split(r"\s+", sample) if t), default=0)

    if cjk_ratio < 0.05 and longest_token > 200:
        return f"looks like an encoded payload (CJK {cjk_ratio:.1%}, token {longest_token} chars)"
    if cjk_ratio < 0.02 and len(letters) > 500:
        return f"almost no Chinese text (CJK {cjk_ratio:.1%})"
    return None


def extract(html: str, *, base_url: str = "") -> tuple[
    Optional[str], Optional[str], Optional[str], list[tuple[str, str]]
]:
    """Return (title, body text, published date, links) from an HTML document."""
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover
        raise FetchError(
            f"beautifulsoup4 is not installed ({exc}). Run: pip install -r requirements.txt"
        ) from exc

    soup = BeautifulSoup(html, "html.parser")

    links: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        label = " ".join(anchor.get_text(" ", strip=True).split())
        links.append((label, urljoin(base_url, anchor["href"])))

    title = None
    for candidate in (soup.find("h1"), soup.find("title")):
        if candidate and candidate.get_text(strip=True):
            title = " ".join(candidate.get_text(" ", strip=True).split())
            break
    if title:
        # Trim the trailing site name Chinese CMSes append to <title>.
        title = re.split(r"\s*[-—|｜]\s*", title)[0].strip() or title

    for tag in soup(list(_STRIP_TAGS)):
        tag.decompose()

    published = None
    match = _DATE_RE.search(soup.get_text(" ", strip=True)[:2000])
    if match:
        y, m, d = match.group(1), int(match.group(2)), int(match.group(3))
        published = f"{y}-{m:02d}-{d:02d}"
        if match.group(4):
            published += f" {int(match.group(4)):02d}:{match.group(5)}"
            if match.group(6):
                published += f":{match.group(6)}"

    body = _best_body(soup)

    return title, (body or None), published, links


def _best_body(soup) -> Optional[str]:
    """Pick the article body generically, with no per-site selectors.

    Every block container is scored by how much text it holds, penalised
    quadratically by link density. That is what separates an article body
    (prose, few links) from site chrome — a product mega-menu can be longer
    than the article yet is almost entirely anchor text.
    """
    best_text, best_score = None, 0.0
    for node in soup.find_all(["article", "main", "section", "div", "td"]):
        text = _node_text(node)
        if len(text) < 120:
            continue
        density = _link_density(node)
        if density > 0.6:
            continue
        score = len(text) * (1.0 - density) ** 2
        if score > best_score:
            best_text, best_score = text, score

    if best_score < 150:
        # Nothing container-shaped looked like prose; fall back to the document.
        fallback = _node_text(soup.body or soup)
        if len(fallback) > len(best_text or ""):
            return fallback
    return best_text


def _link_density(node) -> float:
    """Fraction of a node's text that sits inside anchors (0.0-1.0)."""
    total = len(" ".join(node.get_text(" ", strip=True).split()))
    if not total:
        return 1.0
    linked = sum(
        len(" ".join(a.get_text(" ", strip=True).split()))
        for a in node.find_all("a")
    )
    return min(1.0, linked / total)


def _node_text(node) -> str:
    """Block-aware text extraction so paragraph structure survives."""
    parts: list[str] = []
    for element in node.find_all(["p", "h1", "h2", "h3", "h4", "li", "blockquote", "td", "div"]):
        if element.find(["p", "li", "td"]):
            continue  # a container; its children are handled on their own
        text = " ".join(element.get_text(" ", strip=True).split())
        if text:
            parts.append(text)
    if not parts:
        text = " ".join(node.get_text(" ", strip=True).split())
        return text
    # Collapse consecutive duplicates that nested markup often produces.
    deduped = [p for i, p in enumerate(parts) if i == 0 or p != parts[i - 1]]
    return "\n\n".join(deduped).strip()
