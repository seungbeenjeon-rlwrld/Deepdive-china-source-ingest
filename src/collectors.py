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
GATED_HOSTS = ("mp.weixin.qq.com", "weixin.sogou.com", "channels.weixin.qq.com")


def is_gated(url: Optional[str]) -> bool:
    if not url:
        return False
    try:
        host = (urlparse(url).netloc or "").lower()
    except ValueError:
        # Unparseable URL: treat as gated so we never try to fetch it.
        return True
    return any(g in host for g in GATED_HOSTS)


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
                if not url or is_gated(url):
                    continue
                tried.append(url)
                try:
                    page = self.fetcher.fetch(url)
                except (FetchError, FetchBlocked) as exc:
                    self.log.debug("repost candidate %s failed: %s", url, exc)
                    continue
                if page.blocked or not page.text or len(page.text) < 200:
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
                    "reason": "no readable repost found",
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
    ) -> tuple[list[SourceRecord], list[dict]]:
        import requests

        records: list[SourceRecord] = []
        failures: list[dict] = []
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

        return records, failures


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
    ) -> tuple[list[SourceRecord], list[dict], Optional[int]]:
        import requests
        from urllib.parse import quote

        records: list[SourceRecord] = []
        failures: list[dict] = []
        total: Optional[int] = None
        per_page = 100

        for page in range(0, max(1, -(-max_records // per_page)) + 1):
            inner = quote(f'q="{assignee}"&num={per_page}&page={page}', safe="")
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

        return records, failures, total
