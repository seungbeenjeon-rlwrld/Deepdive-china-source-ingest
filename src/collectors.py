"""Automated collectors that close the WeChat gap without circumventing anything.

The problem these solve: WeChat serves a verification wall to automated
requests, so 公众号 article bodies cannot be fetched. But Chinese corporate
communications are published *redundantly* — the same announcement goes to the
company's own newsroom and to its WeChat account, and is then reposted across
news outlets. Both of those channels serve their content to a normal request.

So instead of attacking the wall, we collect the same content from the channels
that are open:

* :class:`OfficialSiteCollector` crawls the company's own newsroom. This is a
  primary source under prompt 2 §10 (priority 2), ranking above any repost.
* :class:`RepostResolver` takes a source stuck at ``URL_ONLY`` and looks for a
  readable repost of it, recording the repost as its **own** source linked to
  the original — never overwriting the original's label.

Labelling rule, from prompt 2 §6 and §10: a repost's full text is the *repost's*
full text, not the original's. The WeChat record therefore stays ``URL_ONLY``
and a separate record carries the repost content, tagged with its priority-10
provenance. Nothing is presented as the original when it is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

from .fetcher import FetchBlocked, FetchError, Fetcher
from .models import SourceRecord, classify_url, guess_platform
from .utils import get_logger

# Hosts we must not try to fetch bodies from: they gate automated access, and
# working around that gate is out of bounds.
# Hosts that answer an automated request with a login wall or a verification
# page. Never fetched — the search snippet is kept and labelled as a snippet.
# The 工商 registries are here because Baidu indexes their pages (so the titles
# alone reveal entity names, shareholders, litigation) while the pages
# themselves require an account.
GATED_HOSTS = (
    "mp.weixin.qq.com", "weixin.sogou.com", "channels.weixin.qq.com",
    "tianyancha.com", "qcc.com", "qichacha.com", "aiqicha.baidu.com",
    "qixin.com", "shuidi.cn",
)


def is_gated(url: Optional[str]) -> bool:
    if not url:
        return False
    try:
        host = (urlparse(url).netloc or "").lower()
    except ValueError:
        # Unparseable URL: treat as gated so we never try to fetch it.
        return True
    return any(g in host for g in GATED_HOSTS)


# Search engines answer a title query with their own results page, which is
# fetchable, long, and completely worthless as a source.
# (host, path prefix). Anchored on the host because substring matching was
# wrong: "baidu.com/s" also matches baijiahao.baidu.com/s?id=..., which is a
# Baidu content-platform article, not a results page.
_SERP_PATHS = (
    ("baidu.com", "/s"),
    ("google.com", "/search"),
    ("bing.com", "/search"),
    ("sogou.com", "/web"),
    ("so.com", "/s"),
    ("sm.cn", "/s"),
    ("yandex.com", "/search"),
    ("search.cctv.com", "/"),
    ("so.toutiao.com", "/"),
)
_SERP_HOSTS = ("duckduckgo.com",)


def _is_serp(url: str) -> bool:
    try:
        parsed = urlparse((url or "").lower())
    except ValueError:
        return False
    host = (parsed.netloc or "").removeprefix("www.")
    if any(host == h or host.endswith("." + h) for h in _SERP_HOSTS):
        return True
    path = parsed.path or "/"
    return any(
        host == h and path.startswith(prefix) for h, prefix in _SERP_PATHS
    )


# A video page carries no article body — what the extractor returns is player
# chrome (play counts, timestamps, "下载客户端"). Measured on the Unitree corpus:
# two such pages were filed as VERBATIM_FULL_TEXT reposts on 224 and 434 chars.
# The pipeline does not pull transcripts, so a video cannot stand in for the
# gated article's text.
_VIDEO_MARKERS = (
    "haokan.baidu.com/v", "bilibili.com/video", "v.qq.com", "youku.com",
    "ixigua.com", "douyin.com", "kuaishou.com", "iqiyi.com", "tv.sohu.com",
    "weibo.com/tv", "video.weibo.com", "miaopai.com", "/video/",
)

# A genuine repost runs to paragraphs. 200 chars let player chrome through.
_MIN_REPOST_CHARS = 500


def _is_video_page(url: str) -> bool:
    u = (url or "").lower()
    return any(marker in u for marker in _VIDEO_MARKERS)


def _bigrams(text: str) -> set[str]:
    """Character bigrams. Chinese has no spaces, so words are not separable."""
    cleaned = re.sub(r"[\s\W_]+", "", text or "")
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def _is_same_story(original_title: str, candidate_title: str, body: str) -> bool:
    """Does this candidate actually carry the gated article, or just turn up?

    Without this the resolver kept the first page it could fetch. Measured on
    the Unitree corpus that meant a Baidu results page and an unrelated
    company landing page were both filed as full-text reposts of a WeChat
    post — labelled VERBATIM_FULL_TEXT, which is exactly the kind of evidence
    inflation the pipeline exists to prevent.
    """
    wanted = _bigrams(original_title)
    if len(wanted) < 4:
        return False  # too short to judge; refuse rather than guess
    if len(wanted & _bigrams(candidate_title)) / len(wanted) >= 0.5:
        return True
    return len(wanted & _bigrams(body[:4000])) / len(wanted) >= 0.7


class RepostResolver:
    """Finds a readable repost for sources whose original cannot be fetched."""

    def __init__(
        self,
        fetcher: Fetcher,
        search: SearchFn,
        *,
        official_hosts: Optional[list[str]] = None,
    ) -> None:
        self.fetcher = fetcher
        self.search = search
        # A hit on the company's own domain is a primary source, not a repost.
        # Labelling it "Secondary Repost" would understate it (prompt 2 §10).
        self.official_hosts = [h.lower() for h in (official_hosts or [])]
        self.log = get_logger()

    def _classify(self, url: str) -> tuple[str, str]:
        host = (urlparse(url).netloc or "").lower()
        if any(h and h in host for h in self.official_hosts):
            return ("Official Company Source",
                    "2 — Company Official Source (prompt 2 §10)")
        return "Secondary Repost", "10 — Secondary Repost (prompt 2 §10)"

    def resolve(
        self,
        blocked: list[SourceRecord],
        company: str,
        *,
        start_index: int = 1,
        max_sources: int = 10,
    ) -> tuple[list[SourceRecord], list[dict]]:
        records: list[SourceRecord] = []
        gaps: list[dict] = []
        produced = 0

        for original in blocked[:max_sources]:
            title = (original.title or "").strip()
            if not title:
                gaps.append({"source_id": original.source_id, "reason": "no title to search on"})
                continue

            try:
                candidates = self.search(title)
            except Exception as exc:
                gaps.append({"source_id": original.source_id, "reason": f"search failed: {exc}"})
                continue

            record = None
            tried: list[str] = []
            for candidate in candidates:
                url = (candidate.get("url") or "").strip()
                if not url or is_gated(url) or _is_serp(url) or _is_video_page(url):
                    continue
                tried.append(url)
                try:
                    page = self.fetcher.fetch(url)
                except (FetchError, FetchBlocked) as exc:
                    self.log.debug("repost candidate %s failed: %s", url, exc)
                    continue
                if page.blocked or not page.text or len(page.text) < _MIN_REPOST_CHARS:
                    continue
                if _is_serp(page.final_url) or _is_video_page(page.final_url):
                    continue
                if not _is_same_story(title, page.title or "", page.text):
                    self.log.debug("repost candidate %s is a different story", url)
                    continue

                produced += 1
                source_type, priority = self._classify(page.final_url)
                is_official = source_type == "Official Company Source"
                record = SourceRecord(
                    source_id=(f"OFFICIAL_ALT_{start_index + produced - 1:03d}"
                               if is_official
                               else f"REPOST_{start_index + produced - 1:03d}"),
                    title=page.title or candidate.get("title") or title,
                    publisher=candidate.get("site") or urlparse(page.final_url).netloc,
                    publication_date=page.published or candidate.get("publication_date"),
                    source_platform=guess_platform(page.final_url)
                    or urlparse(page.final_url).netloc,
                    source_type=source_type,
                    target_company=company,
                    matched_entity=original.matched_entity,
                    matched_alias=original.matched_alias,
                    discovery_query=title,
                    retrieval_url=url,
                    canonical_url=page.final_url,
                    reaccess_status="VERIFIED_REOPENABLE",
                    # Full text OF THE REPOST. The original stays URL_ONLY.
                    content_access_status="VERBATIM_FULL_TEXT",
                    content=page.text,
                    origin="repost_resolution",
                    extra={
                        "reposts_source_id": original.source_id,
                        "original_url": original.canonical_url or original.retrieval_url,
                        "original_platform": original.source_platform,
                        "source_priority": priority,
                        "label_note": (
                            "VERBATIM_FULL_TEXT refers to THIS page's own text, not the "
                            "gated original's. The original could not be read; its record "
                            "remains URL_ONLY."
                            + ("" if is_official else " Wording may differ from the original.")
                        ),
                        "resolved_to": "official_site" if is_official else "repost",
                        "candidates_tried": tried,
                    },
                )
                record.derived = {
                    **classify_url(page.final_url),
                    "content_chars": len(page.text),
                }
                break

            if record is None:
                gaps.append({
                    "source_id": original.source_id,
                    "title": title,
                    "reason": "no readable repost carrying the same story",
                    "candidates_tried": tried,
                })
            else:
                records.append(record)

        return records, gaps


# ---------------------------------------------------------------------------
# Primary-source registries. These are the highest-priority sources under
# prompt 2 §10, and they are the real answer to "what can this see that a chat
# window cannot?" — not privileged access, but *enumeration* of structured
# Chinese registries. A chat assistant surfaces two or three filings via
# search; these endpoints return the whole set, dated and machine-readable.
#
# Both are open, need no Chinese account, and are queried through their own
# public JSON endpoints — no scraping and nothing circumvented.
# ---------------------------------------------------------------------------

CNINFO_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC_BASE = "http://static.cninfo.com.cn/"
PATENTS_QUERY_URL = "https://patents.google.com/xhr/query"

# Backoff base for the patents endpoint, in seconds. Module-level so tests can
# zero it out instead of actually sleeping.
PATENTS_BACKOFF_BASE = 2.0


def _strip_em(value: Optional[str]) -> Optional[str]:
    """cninfo wraps search hits in <em> tags."""
    if not value:
        return None
    return re.sub(r"</?em>", "", value).strip() or None


def _cninfo_date(millis) -> Optional[str]:
    try:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(int(millis) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return None


# Filings worth pulling the text out of. These are the primary documents — the
# ones a chat window can find a link to but not read: WebFetch returns "raw PDF
# binary stream" for a 6MB prospectus and for a 114KB announcement alike, so
# size is not the discriminator, PDF-ness is.
#
# 提示性公告 is excluded on purpose: it is a one-paragraph pointer saying the
# real document has been published, so extracting it adds a file and no facts.
FILING_TEXT_PATTERNS = (
    "招股说明书", "招股意向书", "上市公告书", "公司章程", "年度报告",
    "半年度报告", "审计报告", "财务报表", "募集说明书", "法律意见",
    "问询函", "回复", "注册资本", "工商变更", "重大资产", "关联交易",
)
FILING_TEXT_EXCLUDE = ("提示性公告", "摘要")

# 招股意向书 is the draft of 招股说明书 and reads almost identically. Taking both
# would double the largest document in the corpus for no new facts, so the
# draft is only read when the final was never filed.
_DRAFT, _FINAL = "招股意向书", "招股说明书"


def wants_filing_text(title: str) -> bool:
    title = title or ""
    if any(skip in title for skip in FILING_TEXT_EXCLUDE):
        return False
    return any(pattern in title for pattern in FILING_TEXT_PATTERNS)


def _clean_assignee(value: str) -> str:
    """Google marks the matched span with <b> and variant chars with ▲▼."""
    return re.sub(r"<[^>]+>|[▲▼]", "", value or "").strip()


def _is_design_patent(publication_number: str) -> bool:
    """CN + digits + S is a 外观设计 patent: drawings, no claims text."""
    return bool(re.fullmatch(r"CN\d+S", (publication_number or "").strip(), re.I))


def _assignee_matches(got: str, asked: str) -> bool:
    """Is this patent assigned to the entity we queried?

    Not string containment: Google returns the traditional-character form of a
    simplified name — 杭州宇▲樹▼科技有限公司 for 杭州宇树科技有限公司 — and a
    containment check rejected it as a different company. Character overlap
    absorbs the variant (9 of 10 characters match) while still rejecting an
    unrelated assignee (普元信息 against 智元机器人 shares only 元).
    """
    got, asked = _clean_assignee(got), _clean_assignee(asked)
    if not got or not asked:
        return not got  # no assignee reported: keep, the query already filtered
    if asked in got or got in asked:
        return True
    shared = sum(1 for ch in set(asked) if ch in got)
    return shared / len(set(asked)) >= 0.7


def _issuer_matches(sec_name: str, search_key: str) -> bool:
    """Is this filing actually filed BY the entity we searched for?

    巨潮资讯网 search is full-text, so a filing is returned whenever the query
    string appears anywhere in the document. Measured on AgiBot: searching
    智元机器人 returns three filings, all of them 普元信息 (688118) — an
    unrelated STAR-market software company whose independent-director reports
    happen to mention 智元机器人. Searching 智元 returns 泰禾智能, 正元智慧,
    创世纪 and 上纬新材: four unrelated issuers.

    The check is against the search key rather than the target company's own
    names, because both cases must pass: a listed company filing under its own
    name (Unitree searched as 宇树科技, secName 宇树科技) and a listed entity a
    private company controls (AgiBot's filings are 上纬新材's). Containment
    either way handles 简称 against 全称.
    """
    a = (sec_name or "").strip()
    b = (search_key or "").strip()
    if not a or not b:
        return False
    return a in b or b in a


class ExchangeFilingCollector:
    """Exchange/regulatory disclosures from 巨潮资讯网 (cninfo).

    Prompt 2 §10 ranks these first: they carry legal liability, so they settle
    questions that media reports only paraphrase. Each record includes a direct
    PDF URL, which is preserved as ``DIRECT_DOCUMENT_URL`` rather than being
    re-typed — prompt 2 §14 says to return the document link, not to
    reconstruct the document.
    """

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher
        self.log = get_logger()

    def collect(
        self,
        company: str,
        search_key: str,
        *,
        max_records: int = 60,
        start_index: int = 1,
        extract_text: bool = False,
        max_pdf_bytes: int = 40 * 1_048_576,
        max_section_chars: int = 40_000,
    ) -> tuple[list[SourceRecord], list[dict]]:
        import requests

        records: list[SourceRecord] = []
        failures: list[dict] = []
        wrong_issuer: dict[str, int] = {}
        page_size = 30
        seen: set[str] = set()

        for page in range(1, max(1, -(-max_records // page_size)) + 1):
            payload = None
            last_error = None
            # cninfo answers 504 under load. Transient — retry rather than
            # reporting an empty filing set for a company that has hundreds.
            for attempt in range(4):
                if attempt:
                    import time as _time

                    _time.sleep(3.0 * (2 ** (attempt - 1)))
                    self.log.info("retrying cninfo page %d (attempt %d)", page, attempt + 1)
                try:
                    self.fetcher._throttle()
                    response = requests.post(
                        CNINFO_QUERY_URL,
                        headers={
                            "User-Agent": self.fetcher.policy.user_agent,
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                        data={
                            "searchkey": search_key,
                            "column": "szse",  # 'szse' searches the whole corpus
                            "tabName": "fulltext",
                            "pageSize": page_size,
                            "pageNum": page,
                            "isHLtitle": "true",
                        },
                        timeout=self.fetcher.policy.timeout_seconds,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    break
                except Exception as exc:
                    last_error = str(exc)
            if payload is None:
                failures.append({"page": page, "error": last_error or "unknown"})
                break

            announcements = payload.get("announcements") or []
            if not announcements:
                break

            for item in announcements:
                if len(records) >= max_records:
                    break
                adjunct = item.get("adjunctUrl") or ""
                if not adjunct or adjunct in seen:
                    continue
                seen.add(adjunct)

                title = _strip_em(item.get("announcementTitle"))
                pdf_url = CNINFO_STATIC_BASE + adjunct.lstrip("/")
                sec_code = _strip_em(item.get("secCode"))
                sec_name = _strip_em(item.get("secName"))

                # Full-text search returns other issuers' filings that merely
                # mention the query. Without this the corpus would carry
                # another company's board reports as the target's disclosures.
                if not _issuer_matches(sec_name, search_key):
                    wrong_issuer[sec_name or "?"] = (
                        wrong_issuer.get(sec_name or "?", 0) + 1
                    )
                    continue

                record = SourceRecord(
                    source_id=f"FILING_{start_index + len(records):03d}",
                    title=title,
                    publisher=item.get("orgName") or sec_name,
                    publication_date=_cninfo_date(item.get("announcementTime")),
                    source_platform="巨潮资讯网 (cninfo) — Exchange Disclosure",
                    source_type="Government / Regulatory / Exchange Disclosure",
                    target_company=company,
                    matched_entity=sec_name,
                    matched_alias=sec_code,
                    discovery_query=f"cninfo searchkey={search_key}",
                    retrieval_url=pdf_url,
                    canonical_url=pdf_url,
                    url_type="DIRECT_DOCUMENT_URL",
                    reaccess_status="NOT_TESTED",
                    # Prompt 2 §14: return the document link, do not rebuild the
                    # document. The PDF body is deliberately not extracted here.
                    content_access_status="URL_ONLY",
                    content=None,
                    origin="exchange_filing_registry",
                    extra={
                        "sec_code": sec_code,
                        "sec_name": sec_name,
                        "announcement_id": item.get("announcementId"),
                        "org_id": item.get("orgId"),
                        "adjunct_size_kb": item.get("adjunctSize"),
                        "adjunct_type": item.get("adjunctType"),
                        "important": item.get("important"),
                        "source_priority": (
                            "1 — Government / Regulatory / Exchange Disclosure (prompt 2 §10)"
                        ),
                        "note": (
                            "Direct PDF link preserved per prompt 2 §14. The filing text was "
                            "not extracted; open the DIRECT_DOCUMENT_URL to read it."
                        ),
                    },
                )
                record.derived = classify_url(pdf_url)
                records.append(record)

            if len(records) >= max_records:
                break

        if wrong_issuer:
            self.log.info(
                "dropped filings from other issuers for %r: %s",
                search_key, wrong_issuer,
            )
            failures.append({
                "stage": "issuer_check",
                "search_key": search_key,
                "error": "filings from other issuers dropped (cninfo search is "
                         "full-text, so unrelated companies that mention the "
                         "query are returned)",
                "dropped_by_issuer": wrong_issuer,
            })

        if extract_text:
            extra_records, extract_failures = self._extract_texts(
                records, company,
                max_pdf_bytes=max_pdf_bytes,
                max_section_chars=max_section_chars,
            )
            records.extend(extra_records)
            failures.extend(extract_failures)

        return records, failures

    def _extract_texts(
        self,
        filings: list[SourceRecord],
        company: str,
        *,
        max_pdf_bytes: int,
        max_section_chars: int,
    ) -> tuple[list[SourceRecord], list[dict]]:
        """Read the primary filings and save their text section by section.

        The registry channel used to stop at the PDF link, which for a reader
        is the same as not having the document: measured, WebFetch cannot
        decode a cninfo PDF at any size. Meanwhile the prospectus holds the
        registered capital, the legal representative, the affiliate network and
        the named competitor set — the layer a web search answers by pointing
        at 国家企业信用信息公示系统.
        """
        import requests

        from .pdf_extract import extract_sections

        have_final = any(
            _FINAL in (f.title or "") and wants_filing_text(f.title or "")
            for f in filings
        )

        out: list[SourceRecord] = []
        failures: list[dict] = []
        for filing in filings:
            title = filing.title or ""
            if not wants_filing_text(title):
                continue
            if _DRAFT in title and have_final:
                self.log.info("skipping %s: the final %s was filed", _DRAFT, _FINAL)
                continue
            url = filing.canonical_url or filing.retrieval_url or ""
            if not url:
                continue
            try:
                response = requests.get(
                    url, timeout=90, stream=True,
                    headers={"User-Agent": self.fetcher.policy.user_agent},
                )
                response.raise_for_status()
                data = response.content
            except Exception as exc:
                failures.append({"stage": "filing_text", "title": title,
                                 "url": url, "error": str(exc)})
                continue

            if len(data) > max_pdf_bytes:
                failures.append({
                    "stage": "filing_text", "title": title, "url": url,
                    "error": f"{len(data) / 1_048_576:.1f}MB over the "
                             f"{max_pdf_bytes / 1_048_576:.0f}MB limit",
                })
                continue

            result = extract_sections(data, max_section_chars=max_section_chars)
            if result.error:
                failures.append({"stage": "filing_text", "title": title,
                                 "url": url, "error": result.error})
                continue

            self.log.info(
                "extracted %s: %d pages, %d chars, %d section(s)",
                title[:30], result.page_count, result.char_count,
                len(result.sections),
            )
            for number, section in enumerate(result.sections, start=1):
                out.append(self._section_record(
                    filing, company, section, number, len(result.sections), url
                ))
        return out, failures

    def _section_record(
        self, filing: SourceRecord, company: str, section, number: int,
        total: int, pdf_url: str,
    ) -> SourceRecord:
        part = f" ({section.part}/{section.of_parts})" if section.of_parts > 1 else ""
        # A distinct URL per section, so the corpus deduper — which collapses
        # records sharing a URL — keeps them apart, and so a reader can jump
        # straight to the pages the text came from.
        section_url = f"{pdf_url}#page={section.page_start}"
        record = SourceRecord(
            source_id=f"{filing.source_id}_TEXT_{number:02d}",
            title=f"{filing.title} — {section.heading}{part}",
            publisher=filing.publisher,
            publication_date=filing.publication_date,
            source_platform=filing.source_platform,
            source_type=filing.source_type,
            target_company=company,
            matched_entity=filing.matched_entity,
            discovery_query=filing.discovery_query,
            retrieval_url=section_url,
            canonical_url=section_url,
            url_type="DIRECT_DOCUMENT_URL",
            reaccess_status="VERIFIED_REOPENABLE",
            # Not VERBATIM_FULL_TEXT: extraction preserves the wording but
            # flattens tables and carries running headers into the body, so
            # numbers and names are trustworthy while layout is not.
            content_access_status="HIGH_FIDELITY_EXTRACTION",
            content=section.text,
            origin="exchange_filing_text",
            extra={
                **{k: v for k, v in filing.extra.items()
                   if k in ("sec_code", "sec_name", "announcement_id", "org_id")},
                "part_of": filing.source_id,
                "section_heading": section.heading,
                "section_part": section.part,
                "section_of_parts": section.of_parts,
                "page_start": section.page_start,
                "page_end": section.page_end,
                "pdf_url": pdf_url,
                "extraction_tool": "pypdf",
                "source_priority": (
                    "1 — Government / Regulatory / Exchange Disclosure (prompt 2 §10)"
                ),
                "note": (
                    "Text extracted from the filing PDF, which no chat-side fetch "
                    "decodes. Wording is the document's own; tables are flattened."
                ),
            },
        )
        record.derived = {**classify_url(pdf_url), "content_chars": len(section.text)}
        return record


class PatentCollector:
    """Patents by assignee, via Google Patents' public query endpoint.

    Google Patents indexes CNIPA, so a Chinese assignee name returns the
    company's Chinese filings with titles and abstracts in Chinese. Prompt 2
    §10 ranks patents fifth, above any media coverage of the same technology.

    Caveat, measured: this unauthenticated endpoint rate-limits bursts with
    HTTP 503 and the block can persist for a while. Backoff is implemented and
    a throttled run is reported as a failure rather than an empty result — but
    if you need patents reliably or at volume, use an API with a key (EPO OPS
    has a free tier and also indexes CN) instead of leaning on this.
    """

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher
        self.log = get_logger()

    def collect(
        self,
        company: str,
        assignee: str,
        *,
        max_records: int = 60,
        start_index: int = 1,
        claims_for: int = 0,
    ) -> tuple[list[SourceRecord], list[dict], Optional[int]]:
        import requests
        from urllib.parse import quote

        records: list[SourceRecord] = []
        failures: list[dict] = []
        wrong_assignee: dict[str, int] = {}
        total: Optional[int] = None
        per_page = 100

        for page in range(0, max(1, -(-max_records // per_page)) + 1):
            # assignee=, not q=. q= is a full-text search, so it returns
            # patents that merely mention the company: measured, q="宇树科技"
            # gives 276 results against assignee="宇树科技"'s 188 — 88 of them
            # (32%) assigned to somebody else and filed here as the target's IP.
            inner = quote(
                f'assignee="{assignee}"&num={per_page}&page={page}', safe=""
            )
            url = f"{PATENTS_QUERY_URL}?url={inner}&exp="
            payload = None
            last_error = None
            # Google Patents throttles bursts with 503. Back off rather than
            # hammering it; a throttled run reports a failure, never a silent
            # empty result.
            for attempt in range(3):
                try:
                    self.fetcher._throttle()
                    if attempt and PATENTS_BACKOFF_BASE:
                        import time as _time

                        _time.sleep(PATENTS_BACKOFF_BASE * (2 ** attempt))
                    response = requests.get(
                        url,
                        headers={
                            "User-Agent": self.fetcher.policy.user_agent,
                            "Accept": "application/json, text/plain, */*",
                            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                            "Referer": "https://patents.google.com/",
                        },
                        timeout=self.fetcher.policy.timeout_seconds,
                    )
                    if response.status_code in (429, 503):
                        last_error = f"HTTP {response.status_code} (throttled)"
                        continue
                    response.raise_for_status()
                    payload = response.json()
                    break
                except Exception as exc:
                    last_error = str(exc)
            if payload is None:
                failures.append({"page": page, "error": last_error or "unknown"})
                break

            results = payload.get("results") or {}
            if total is None:
                total = results.get("total_num_results")
            clusters = results.get("cluster") or []
            items = [it for c in clusters for it in (c.get("result") or [])]
            if not items:
                break

            for item in items:
                if len(records) >= max_records:
                    break
                patent = item.get("patent") or {}
                # Belt and braces alongside the assignee= query, and the same
                # guard the filings channel uses: the returned assignee must
                # actually be the entity we asked for. Google highlights the
                # match with <b> tags, so strip them before comparing.
                got = patent.get("assignee") or ""
                if not _assignee_matches(got, assignee):
                    key = _clean_assignee(got) or "?"
                    wrong_assignee[key] = wrong_assignee.get(key, 0) + 1
                    continue
                number = patent.get("publication_number")
                if not number:
                    continue
                page_url = f"https://patents.google.com/patent/{number}/zh"
                snippet = re.sub(r"\s+", " ", (patent.get("snippet") or "")).strip() or None

                record = SourceRecord(
                    source_id=f"PATENT_{start_index + len(records):03d}",
                    title=(patent.get("title") or "").strip() or None,
                    publisher="CNIPA (via Google Patents)",
                    author=patent.get("inventor") or None,
                    publication_date=patent.get("publication_date") or patent.get("filing_date"),
                    source_platform="Google Patents / CNIPA",
                    source_type="Patent",
                    target_company=company,
                    matched_entity=patent.get("assignee") or assignee,
                    matched_alias=assignee,
                    discovery_query=f'google patents assignee="{assignee}"',
                    retrieval_url=page_url,
                    canonical_url=page_url,
                    url_type="STABLE_PUBLIC_URL",
                    reaccess_status="NOT_TESTED",
                    # The snippet is the published abstract as returned, not the
                    # full specification, so it is labelled as a snippet.
                    content_access_status="SEARCH_SNIPPET_ONLY" if snippet else "URL_ONLY",
                    content=snippet,
                    origin="patent_registry",
                    extra={
                        "publication_number": number,
                        "filing_date": patent.get("filing_date"),
                        "grant_date": patent.get("grant_date"),
                        "priority_date": patent.get("priority_date"),
                        "language": patent.get("language"),
                        "pdf": patent.get("pdf"),
                        "source_priority": "5 — Patent / Paper (prompt 2 §10)",
                        "note": (
                            "Content is the published abstract as returned by the query "
                            "endpoint, not the full specification."
                        ),
                    },
                )
                record.derived = classify_url(page_url)
                records.append(record)

            if len(records) >= max_records or len(items) < per_page:
                break

        if claims_for:
            failures.extend(self._add_claims(records[:claims_for]))

        if wrong_assignee:
            self.log.info(
                "dropped patents assigned elsewhere for %r: %s",
                assignee, wrong_assignee,
            )
            failures.append({
                "stage": "assignee_check",
                "assignee": assignee,
                "error": "patents assigned to other entities dropped",
                "dropped_by_assignee": wrong_assignee,
            })

        return records, failures, total

    def _add_claims(self, records: list[SourceRecord]) -> list[dict]:
        """Replace the abstract with the patent's claims.

        The query endpoint returns the published abstract, which says what the
        invention is for. The claims say what it actually covers — the legally
        operative text, and the part a technical comparison needs. Both are on
        the public detail page, free, with no search API involved.

        The description is deliberately not taken. Measured on CN109941369B:
        abstract 807 chars, claims 1,328, description 24,348 — mostly
        background prose that would swell the corpus for little.
        """
        import requests
        from bs4 import BeautifulSoup

        failures: list[dict] = []
        for record in records:
            url = record.canonical_url or record.retrieval_url
            if not url:
                continue
            try:
                self.fetcher._throttle()
                response = requests.get(
                    url,
                    headers={
                        "User-Agent": self.fetcher.policy.user_agent,
                        "Accept": "text/html",
                        "Referer": "https://patents.google.com/",
                    },
                    timeout=self.fetcher.policy.timeout_seconds,
                )
                if response.status_code in (429, 503):
                    failures.append({
                        "stage": "patent_claims",
                        "publication_number": (record.extra or {}).get(
                            "publication_number"),
                        "error": f"HTTP {response.status_code} (throttled); "
                                 "abstract kept",
                    })
                    break  # the rest will be throttled too
                response.raise_for_status()
                # requests mis-guesses the charset here and returns mojibake.
                response.encoding = "utf-8"
                soup = BeautifulSoup(response.text, "html.parser")
                claims = " ".join(
                    element.get_text(" ", strip=True)
                    for element in soup.select("div.claim-text")
                ).strip()
            except Exception as exc:
                failures.append({
                    "stage": "patent_claims",
                    "publication_number": (record.extra or {}).get(
                        "publication_number"),
                    "error": str(exc)[:120],
                })
                continue

            if len(claims) < 200:
                number = str((record.extra or {}).get("publication_number") or "")
                if _is_design_patent(number):
                    # A 外观设计 patent protects an appearance, so it has
                    # drawings and no claims text. Reporting that as a failure
                    # filled the failures list with correct behaviour.
                    record.extra = {**(record.extra or {}), "content_is": "abstract",
                                    "patent_kind": "design"}
                    continue
                failures.append({
                    "stage": "patent_claims",
                    "publication_number": number,
                    "error": f"claims too short to be real ({len(claims)} chars); "
                             "abstract kept",
                })
                continue

            abstract = record.content
            record.content = claims
            # Extraction from the page's own markup, so the wording is the
            # patent's; the layout of numbered claims does not survive.
            record.content_access_status = "HIGH_FIDELITY_EXTRACTION"
            record.extra = {
                **(record.extra or {}),
                "abstract": abstract,
                "claims_chars": len(claims),
                "content_is": "claims",
            }
            record.derived = {**(record.derived or {}), "content_chars": len(claims)}
        return failures

