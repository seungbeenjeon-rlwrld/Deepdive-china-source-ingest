"""Turning model output and page text into structure.

Everything here is a pure function: text in, data out, no I/O and no provider
calls. That is deliberate — these are the parts most likely to meet malformed
input, so they need to be testable without a network or an API key.

Split out of ``pipeline.py``, which had grown to ~2,000 lines with orchestration
and parsing interleaved.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from .models import (
    CONTENT_ACCESS_STATUSES,
    SourceRecord,
    classify_url,
    guess_platform,
)

# Labels that assert the model actually read source text. A model given only
# search snippets cannot support any of these, so they are verifiable.
_TEXT_CLAIMING_STATUSES = (
    "VERBATIM_FULL_TEXT",
    "VERBATIM_PARTIAL_TEXT",
    "TRANSCRIPT_EXTRACTED",
    "HIGH_FIDELITY_EXTRACTION",
)


# Values that mean "not available" in the model's own output.
_NULLISH = {"", "null", "none", "n/a", "na", "-", "—", "未知", "无", "不适用"}


# Field labels emitted by prompt 2 §15 -> SourceRecord attribute names.
_FIELD_MAP = {
    "SOURCE_ID": "source_id",
    "TARGET_COMPANY": "target_company",
    "SOURCE_PLATFORM": "source_platform",
    "SOURCE_TYPE": "source_type",
    "TITLE": "title",
    "PUBLISHER / ACCOUNT": "publisher",
    "PUBLISHER/ACCOUNT": "publisher",
    "PUBLISHER": "publisher",
    "ACCOUNT": "publisher",
    "AUTHOR": "author",
    "PUBLICATION_DATE": "publication_date",
    "MATCHED_ENTITY": "matched_entity",
    "MATCHED_ALIAS": "matched_alias",
    "DISCOVERY_QUERY": "discovery_query",
    "RETRIEVAL_URL": "retrieval_url",
    "CANONICAL_URL": "canonical_url",
    "URL_TYPE": "url_type",
    "REACCESS_STATUS": "reaccess_status",
    "CONTENT_ACCESS_STATUS": "content_access_status",
    "TRANSCRIPT_METHOD": "transcript_method",
}


_FIELD_LINE = re.compile(r"^\s*\**\s*([A-Z][A-Z0-9_ /]{2,40})\s*\**\s*:\s*(.*)$")


# Words a Chinese corporate site uses for its news listing.
_NEWS_KEYWORDS = (
    "新闻", "资讯", "动态", "news", "media", "press", "公告", "article",
)


# Chinese has no word separators, so a name captured before a stock code can
# start with the verb that preceded it ("取得上纬新材").
_LEADING_VERBS = r"^(取得|收购|入主|控股|参股|持有|旗下|和|与|及|的|为|即)"

# Label words that sit next to a stock code but are not a company name.
# Measured: "科创板代码：688836" yielded 创板代码 as the listed entity, which then
# searched cninfo for a phrase and returned nothing.
_NOT_A_COMPANY = (
    "代码", "股票", "证券", "板块", "上市", "简称", "指数", "行情", "股价",
    "主板", "创板", "科创", "创业板", "港股", "美股", "交易", "编号",
)


# Mainland listing codes: 6 digits with an optional exchange suffix.
_STOCK_CODE = re.compile(r"\b(6\d{5}|00\d{4}|30\d{4})(?:\.(?:SH|SZ|sh|sz))?\b")


def _clean(value: Optional[str]) -> Optional[str]:
    """Normalise a metadata value; never fabricate one."""
    if value is None:
        return None
    text = value.strip().strip("*").strip()
    text = re.sub(r"^\[(.*)\]$", r"\1", text).strip()
    if text.casefold() in _NULLISH:
        return None
    return text or None


def parse_source_blocks(text: str, company: str) -> list[SourceRecord]:
    """Turn prompt 2's ``SOURCE_ID:`` blocks into structured records.

    ``SOURCE_CONTENT`` is copied verbatim — no re-wrapping, no summarising.
    """
    lines = text.splitlines()

    # Prompt 2 puts NEW_SEARCH_ANCHOR and the Final Collection Summary after all
    # source blocks. Those footers contain their own SOURCE_ID: lines, so we stop
    # scanning at the first footer marker rather than mistaking them for sources.
    stop_pattern = re.compile(
        r"^\s*\**\s*(NEW_SEARCH_ANCHOR|Final Collection Summary"
        r"|TOTAL_SOURCES_DISCOVERED|REMAINING_SOURCE_GAPS)\b",
        re.I,
    )
    hard_stop = len(lines)
    for i, line in enumerate(lines):
        if stop_pattern.match(line):
            hard_stop = i
            break

    starts = [
        i for i, line in enumerate(lines[:hard_stop])
        if re.match(r"^\s*\**\s*SOURCE_ID\s*\**\s*:", line)
    ]
    if not starts:
        return []

    bounds = []
    for pos, start in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else hard_stop
        bounds.append((start, end))

    records: list[SourceRecord] = []
    for order, (start, end) in enumerate(bounds, start=1):
        record = _parse_one_block(lines[start:end], company, fallback_id=f"SOURCE_{order:03d}")
        records.append(record)
    return records


def _parse_one_block(block: list[str], company: str, *, fallback_id: str) -> SourceRecord:
    fields: dict[str, Any] = {}
    unknown: dict[str, str] = {}
    content_lines: list[str] | None = None

    for line in block:
        if content_lines is not None:
            content_lines.append(line)
            continue

        match = _FIELD_LINE.match(line)
        if not match:
            continue
        label = match.group(1).strip().upper()
        value = match.group(2)

        if label.replace(" ", "") == "SOURCE_CONTENT":
            content_lines = []
            if value.strip():
                content_lines.append(value)
            continue

        attr = _FIELD_MAP.get(label) or _FIELD_MAP.get(label.replace(" ", ""))
        if attr:
            fields[attr] = _clean(value)
        else:
            cleaned = _clean(value)
            if cleaned is not None:
                unknown[label] = cleaned

    content = None
    if content_lines is not None:
        joined = "\n".join(content_lines)
        # Drop only the horizontal-rule separators that bound the block.
        joined = re.sub(r"\n\s*-{3,}\s*$", "", joined.rstrip())
        content = joined.strip("\n") or None

    record = SourceRecord(
        source_id=fields.pop("source_id", None) or fallback_id,
        target_company=fields.pop("target_company", None) or company,
        origin="stage2_model_output",
        **{k: v for k, v in fields.items()},
    )
    record.content = content
    if unknown:
        record.extra["unmapped_fields"] = unknown
    record.derived = {
        **classify_url(record.canonical_url or record.retrieval_url),
        "content_chars": len(content) if content else 0,
        "has_content": bool(content),
    }
    return record


def split_status(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Separate a CONTENT_ACCESS_STATUS from any commentary the model appended.

    Models annotate the field instead of keeping it clean — measured:
    "VERBATIM_PARTIAL_TEXT（正文全文保留；文末 ETF 推介段落省略）". Treating that as
    an invalid value normalised genuinely good evidence down to a snippet, which
    is worse than the mislabelling this check exists to catch. So take the
    leading known status and keep the rest as a note.
    """
    if not raw:
        return None, None
    text = raw.strip()
    for status in CONTENT_ACCESS_STATUSES:
        if text == status:
            return status, None
        if text.startswith(status):
            note = text[len(status):].strip(" ：:（）()[]，,;；")
            return status, note or None
    return None, None


def verify_labels(
    records: list[SourceRecord], retrieval: dict[str, Any], *, snippet_cap: int
) -> dict[str, Any]:
    """Downgrade access labels the retrieval cannot support.

    Prompt 2 §6 forbids presenting a summary as verbatim text, but a model will
    do it anyway — measured: glm-4.7-flash labelled 58 records
    ``VERBATIM_PARTIAL_TEXT`` whose content was 32-161 chars, i.e. the very
    search snippets it had been handed.

    The pipeline knows what it injected, so it can check. This only ever
    **downgrades**, and it keeps the model's original claim in
    ``derived.label_claimed`` so nothing is hidden. If the source content is no
    longer than the snippet budget, the model could not have read more than a
    snippet, whatever it says.
    """
    if not retrieval or retrieval.get("skipped"):
        return {"checked": 0, "downgraded": 0, "note": "no injected retrieval to check against"}

    # Generous threshold: a real partial-text read would exceed the snippet
    # budget by a wide margin.
    # Generous threshold: a real partial-text read exceeds the snippet budget by
    # a wide margin, so this only catches claims the retrieval cannot support.
    limit = max(int(snippet_cap * 1.5), 400)
    checked = downgraded = invalid = 0
    details: list[dict[str, Any]] = []
    invalid_details: list[dict[str, Any]] = []

    for record in records:
        claim = record.content_access_status

        # Models annotate the field instead of keeping it clean — measured:
        # "VERBATIM_PARTIAL_TEXT（正文全文保留；文末 ETF 推介段落省略）". Recover the
        # status first, or genuinely good evidence gets normalised down to a
        # snippet, which is worse than the mislabelling this check exists for.
        if claim and claim not in CONTENT_ACCESS_STATUSES:
            recovered, note = split_status(claim)
            if recovered:
                record.derived = {
                    **(record.derived or {}),
                    "label_raw": claim,
                    "label_note": note,
                }
                record.content_access_status = recovered
                claim = recovered

        # Prompt 2 §6 fixes the permitted vocabulary, but models mistype it —
        # measured: "SEARCH_SNIPPED". An unrecognised value must not be stored
        # as if it were a valid label, or downstream filtering silently misses
        # the record. Keep the raw value and fall back to the weakest claim.
        # A missing label is as unusable downstream as a mistyped one: filtering
        # on content_access_status would skip the record entirely.
        if claim not in CONTENT_ACCESS_STATUSES:
            invalid += 1
            record.derived = {
                **(record.derived or {}),
                "label_claimed": claim,
                "label_verified": "URL_ONLY" if not record.content else "SEARCH_SNIPPET_ONLY",
                "label_invalid_reason": (
                    (f"{claim!r} is not one of the CONTENT_ACCESS_STATUS values prompt 2 "
                     "permits" if claim else
                     "the model omitted CONTENT_ACCESS_STATUS")
                    + "; normalised to the weakest claim the content supports."
                ),
            }
            record.content_access_status = record.derived["label_verified"]
            invalid_details.append({"source_id": record.source_id, "claimed": claim})
            claim = record.content_access_status

        if claim not in _TEXT_CLAIMING_STATUSES:
            continue
        checked += 1
        length = len(record.content or "")
        if length > limit:
            continue
        downgraded += 1
        record.derived = {
            **(record.derived or {}),
            "label_claimed": claim,
            "label_verified": "SEARCH_SNIPPET_ONLY",
            "label_downgrade_reason": (
                f"content is {length} chars, within the {snippet_cap}-char snippet budget "
                f"injected into the prompt, so the model cannot have read source text. "
                f"Claim {claim!r} is not supported by what it was given."
            ),
        }
        record.content_access_status = "SEARCH_SNIPPET_ONLY"
        details.append({
            "source_id": record.source_id, "claimed": claim, "content_chars": length,
        })

    return {
        "checked": checked,
        "downgraded": downgraded,
        "invalid_labels": invalid,
        "invalid_label_details": invalid_details,
        "threshold_chars": limit,
        "snippet_cap": snippet_cap,
        "downgrades": details,
        "note": (
            "Labels are verified against the retrieval actually injected. Only "
            "downgrades are applied; the model's original claim is kept in "
            "derived.label_claimed."
        ),
    }


def _record_from_citation(item: dict[str, Any], company: str, *, index: int) -> SourceRecord:
    """A normalised citation returned alongside a stage's generated text.

    Providers differ in what they give back: Hunyuan citations carry a link
    only, while Zhipu's ``web_search`` entries include a search summary. The
    label follows what actually arrived — never an assumption.
    """
    url = _clean(item.get("url"))
    site = _clean(item.get("site"))
    content = item.get("content") or None
    record = SourceRecord(
        source_id=f"CITATION_{index:03d}",
        title=_clean(item.get("title")),
        publisher=site,
        publication_date=_clean(item.get("publication_date")),
        source_platform=guess_platform(url, site),
        target_company=company,
        retrieval_url=url,
        canonical_url=None,
        content_access_status="SEARCH_SNIPPET_ONLY" if content else "URL_ONLY",
        content=content,
        origin="provider_citation",
        extra={
            "citation_index": item.get("index"),
            "icon": _clean(item.get("icon")),
            "provider_payload": item.get("_raw"),
        },
    )
    record.derived = {
        **classify_url(url),
        "content_chars": len(content) if content else 0,
    }
    return record


def _record_from_page(
    page: dict[str, Any], company: str, *, query: str, site: Optional[str]
) -> SourceRecord:
    """A normalised structured search result.

    Search APIs return a *summary* of the page, not its body text, so this is
    labelled ``SEARCH_SNIPPET_ONLY`` at best — never ``VERBATIM_FULL_TEXT``.
    """
    url = _clean(page.get("url"))
    body = page.get("content") or None

    record = SourceRecord(
        source_id="SEARCH_000",  # renumbered by the caller
        title=_clean(page.get("title")),
        publisher=_clean(page.get("site")),
        publication_date=_clean(page.get("publication_date")),
        source_platform=guess_platform(url, _clean(page.get("site"))),
        target_company=company,
        discovery_query=query,
        retrieval_url=url,
        canonical_url=None,
        content_access_status="SEARCH_SNIPPET_ONLY" if body else "URL_ONLY",
        content=body,
        origin="provider_search",
        extra={
            key: page[key]
            for key in ("score", "authority_level", "icon", "images", "deeplinks", "index")
            if page.get(key) not in (None, "", [])
        }
        | {"site_filter": site, "provider_payload": page.get("_raw")},
    )
    record.derived = classify_url(url)
    return record


_QUERY_SECTION = re.compile(
    r"^\s*#*\s*\**\s*(?:E\.|E\b)?\s*\**\s*.{0,20}Recommended Search Quer\w*",
    re.I | re.M,
)


_NEXT_SECTION = re.compile(r"^\s*#*\s*\**\s*[F-Z]\.\s", re.M)


_LIST_ITEM = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+(.*\S)\s*$")


def extract_recommended_queries(text: str) -> list[str]:
    """Pull section E's query list out of stage 1 output, for the WSA sweep."""
    match = _QUERY_SECTION.search(text)
    if not match:
        return []
    tail = text[match.end():]
    end = _NEXT_SECTION.search(tail)
    if end:
        tail = tail[: end.start()]

    queries: list[str] = []
    seen: set[str] = set()
    for line in tail.splitlines():
        item = _LIST_ITEM.match(line)
        if not item:
            continue
        value = item.group(1).strip()
        # Strip markdown emphasis, backticks and surrounding quotes.
        value = re.sub(r"^[`\"'“”‘’*]+|[`\"'“”‘’*]+$", "", value).strip()
        value = re.sub(r"\s*\|\s*.*$", "", value).strip()  # table leftovers
        if not value or len(value) > 200:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        queries.append(value)
    return queries


# Fragments a model emits inside section E that are labels, not queries.
_NOT_A_QUERY = re.compile(
    r"^\s*(?:[A-F]\.|#{1,6}\s|\*{2}[^*]+\*{2}\s*[:：]?\s*$)"
    r"|^[^\w\u4e00-\u9fff]*$"          # punctuation/symbols only
    r"|[:：]\s*$",                        # trailing colon => a heading
)


def clean_queries(
    queries: list[str], *, company: str, aliases: Optional[list[str]] = None,
    drop_site_operator: Optional[str] = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """Filter a model's recommended-query list down to things worth searching.

    Measured failure this guards against: a run whose first "query" was
    ``微信生态搜索**:`` — a bolded section label the list parser picked up — and
    a block of ``site:mp.weixin.qq.com`` queries which Baidu answers with pages
    *about* WeChat search, because it barely indexes WeChat articles at all.
    Both wasted searches and dragged the retrieval off-topic.
    """
    anchors = [company] + list(aliases or [])
    anchors = [a for a in anchors if a and len(a) >= 2]
    kept: list[str] = []
    dropped: list[dict[str, str]] = []

    for raw in queries:
        query = re.sub(r"\*{1,2}", "", raw).strip().strip("：:").strip()
        if not query or _NOT_A_QUERY.search(raw):
            dropped.append({"query": raw, "reason": "not a query (heading or label)"})
            continue
        if drop_site_operator and f"site:{drop_site_operator}" in query:
            # Keep the search intent, drop only the operator this engine cannot
            # honour. Discarding the whole query threw away every query in a
            # measured run whose section E was entirely site:-scoped.
            stripped = query.replace(f"site:{drop_site_operator}", "").strip()
            dropped.append({
                "query": raw,
                "reason": (
                    f"site:{drop_site_operator} is not usefully indexed by this engine; "
                    f"operator stripped, searched as {stripped!r}"
                    if stripped else
                    f"site:{drop_site_operator} query had no other terms"
                ),
            })
            if not stripped:
                continue
            query = stripped
        # A query that never names the company drifts off-target.
        if anchors and not any(a in query for a in anchors):
            dropped.append({"query": raw, "reason": "does not mention the company or an alias"})
            continue
        if query not in kept:
            kept.append(query)

    return kept, dropped


def parse_new_search_anchors(text: str) -> list[dict[str, Optional[str]]]:
    anchors: list[dict[str, Optional[str]]] = []
    for match in re.finditer(r"NEW_SEARCH_ANCHOR\s*:?\s*\n(.*?)(?=\n\s*(?:NEW_SEARCH_ANCHOR|\Z|-{3,}|\*\*))",
                             text, re.S):
        chunk = match.group(1)
        entry = {"entity_type": None, "name": None, "source_id": None}
        for line in chunk.splitlines():
            field = _FIELD_LINE.match(line)
            if not field:
                continue
            label = field.group(1).strip().upper()
            if label == "ENTITY_TYPE":
                entry["entity_type"] = _clean(field.group(2))
            elif label == "NAME":
                entry["name"] = _clean(field.group(2))
            elif label == "SOURCE_ID":
                entry["source_id"] = _clean(field.group(2))
        if any(entry.values()):
            anchors.append(entry)
    return anchors


_SUMMARY_KEYS = (
    "TOTAL_SOURCES_DISCOVERED",
    "VERBATIM_FULL_TEXT",
    "VERBATIM_PARTIAL_TEXT",
    "TRANSCRIPT_EXTRACTED",
    "HIGH_FIDELITY_EXTRACTION",
    "SEARCH_SNIPPET_ONLY",
    "URL_ONLY",
    "STABLE_CANONICAL_URL_FOUND",
    "TEMPORARY_URL_ONLY",
    "NOT_REOPENABLE",
    "NEW_SEARCH_ANCHORS",
    "REMAINING_SOURCE_GAPS",
)


def parse_collection_summary(text: str) -> dict[str, Optional[str]]:
    """Read back the model's own final tally, verbatim, without recomputing it."""
    summary: dict[str, Optional[str]] = {}
    for key in _SUMMARY_KEYS:
        match = re.search(rf"^\s*\**\s*{key}\s*\**\s*:\s*(.*)$", text, re.M)
        if match:
            summary[key.lower()] = _clean(match.group(1))
    return summary


def _parse_json_object(text: str) -> Optional[dict[str, Any]]:
    """Pull the first JSON object out of a model response.

    Models wrap JSON in code fences even when told not to, and sometimes add a
    sentence before it. Try the whole string, then the outermost braces.
    """
    candidates = [text.strip()]

    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        candidates.append(fenced.group(1).strip())

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _find_listed_entity(text: str) -> Optional[dict[str, str]]:
    """Find a listed company named next to a mainland stock code.

    Stage 1 writes "上纬新材（688585.SH）", so the bracket is the anchor: the
    Chinese characters immediately before it are the name. Chinese has no word
    separators, so scanning backwards without that anchor swallows the
    surrounding sentence ("智元机器人取得上纬新材").

    A-share short names run 2-6 characters; anything longer is sentence, not name.
    """
    counts: dict[str, int] = {}

    # Name followed by the code in brackets — the reliable form.
    bracketed = re.finditer(
        r"([\u4e00-\u9fff]{2,4})\s*[（(]\s*(6\d{5}|00\d{4}|30\d{4})"
        r"(?:\.(?:SH|SZ|sh|sz))?\s*[）)]",
        text,
    )
    for match in bracketed:
        name = re.sub(_LEADING_VERBS, "", match.group(1))
        if len(name) < 2 or any(bad in name for bad in _NOT_A_COMPANY):
            continue
        key = f"{name}|{match.group(2)}"
        counts[key] = counts.get(key, 0) + 2  # weight the trustworthy form

    if not counts:
        # Fallback: no brackets. Take the trailing characters before the code and
        # strip leading connectives, accepting that this is less reliable.
        for match in _STOCK_CODE.finditer(text):
            window = text[max(0, match.start() - 8): match.start()]
            name = "".join(re.findall(r"[\u4e00-\u9fff]", window))[-4:]
            name = re.sub(_LEADING_VERBS, "", name)
            if len(name) < 2 or any(bad in name for bad in _NOT_A_COMPANY):
                continue
            key = f"{name}|{match.group(1)}"
            counts[key] = counts.get(key, 0) + 1

    if not counts:
        return None
    key = max(counts, key=lambda k: counts[k])
    name, code = key.split("|", 1)
    return {"name": name, "code": code, "mentions": str(counts[key])}


def _official_host_candidates(text: str) -> list[str]:
    """Hosts in stage 1 output that look like the company's own site.

    Ranked by how often stage 1 cited them, after removing the media, registry
    and community domains that dominate the source list.
    """
    excluded = (
        "baidu.com", "weibo.com", "zhihu.com", "sina.com", "qq.com", "163.com",
        "sohu.com", "bilibili.com", "eastmoney.com", "10jqka.com", "cls.cn",
        "jiemian.com", "thepaper.cn", "tmtpost.com", "stcn.com", "21jingji.com",
        "xueqiu.com", "github.com", "arxiv.org", "huggingface.co", "wikipedia.org",
        "tianyancha.com", "qcc.com", "qichacha.com", "cninfo.com.cn", "gov.cn",
        "zhipin.com", "liepin.com", "feishu.cn", "csdn.net", "smzdm.com",
        "chinadaily.com.cn", "cnr.cn", "ifeng.com", "guancha.cn", "36kr.com",
        "nbd.com.cn", "caijing", "leaderobot.com", "serpapi.com",
    )
    counts: dict[str, int] = {}

    # Stage 1 writes bare hosts inside markdown tables as often as full URLs
    # ("zhiyuan-robot.com" with no scheme), so match both.
    patterns = (
        r"https?://([^\s/\)\]\|\"'>]+)",
        r"\b((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:com|cn|net|org|io|ai)"
        r"(?:\.[a-z]{2})?)\b",
    )
    for pattern in patterns:
        for raw in re.findall(pattern, text, re.I):
            host = raw.lower().strip(".").removeprefix("www.")
            if not host or "." not in host:
                continue
            if any(bad in host for bad in excluded):
                continue
            counts[host] = counts.get(host, 0) + 1
    return sorted(counts, key=lambda h: -counts[h])


# Words shared by thousands of Chinese companies; they identify nothing.
_GENERIC_NAME_PARTS = (
    "股份有限公司", "有限责任公司", "有限公司", "合伙企业", "有限合伙",
    "科技", "技术", "创新", "机器人", "智能", "集团", "控股", "实业",
    "信息", "电子", "网络", "数据", "国际", "发展",
)


_LOCATION_PREFIXES = (
    "上海", "北京", "深圳", "广州", "杭州", "苏州", "南京", "成都", "武汉",
    "天津", "重庆", "西安", "宁波", "无锡", "合肥", "青岛", "长沙", "东莞",
)


def _identity_tokens(names: list[str]) -> list[str]:
    """Reduce company names to the parts that actually identify them.

    "智元机器人" and "智元创新（上海）科技股份有限公司" are the same company but
    share no full name — only 智元. Stripping the location prefix and the
    industry/corporate-form words leaves that.
    """
    tokens: list[str] = []
    for name in names:
        cleaned = re.sub(r"[（()）\s]", "", name)
        for prefix in _LOCATION_PREFIXES:
            if cleaned.startswith(prefix) and len(cleaned) > len(prefix) + 1:
                cleaned = cleaned[len(prefix):]
        for part in _GENERIC_NAME_PARTS:
            cleaned = cleaned.replace(part, "")
        # An ASCII name is already distinctive; keep it whole.
        candidate = cleaned if cleaned else re.sub(r"[（()）\s]", "", name)
        if len(candidate) >= 2:
            tokens.append(candidate)
        if name.isascii() and len(name) >= 3:
            tokens.append(name)

    seen: set[str] = set()
    ordered: list[str] = []
    for token in tokens:
        key = token.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(token)
    return ordered