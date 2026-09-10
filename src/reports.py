"""Markdown rendering for the corpus files.

Kept apart from the pipeline so a change to how a report reads cannot affect
what gets collected. Each function takes an already-built payload and returns a
string; none of them touch disk.
"""

from __future__ import annotations

from typing import Any

from .models import SourceRecord

def _sweep_markdown(company: str, payload: dict[str, Any]) -> str:
    lines = [
        f"# Stage 3 — Structured Search Sweep: {company}",
        "",
        f"- provider: {payload.get('provider')}",
        f"- endpoint: {payload.get('endpoint')}",
        f"- queries_searched: {payload['queries_searched']} / {payload['queries_available']}",
        f"- results: {len(payload['results'])}",
        f"- failures: {len(payload['failures'])}",
        f"- generated_at: {payload['generated_at']}",
        "",
        f"> {payload['content_note']}",
        "",
    ]
    if payload.get("engine_suggested_anchors"):
        lines += ["## Engine-suggested new anchors", ""]
        lines += [f"- {a}" for a in payload["engine_suggested_anchors"]]
        lines.append("")
    if payload["queries_not_searched"]:
        lines += ["## Queries not searched", ""]
        lines += [f"- {q}" for q in payload["queries_not_searched"]]
        lines.append("")
    if payload["failures"]:
        lines += ["## Failed queries", ""]
        lines += [f"- `{f['query']}` — {f['error']}" for f in payload["failures"]]
        lines.append("")

    lines += ["## Results", ""]
    for record in payload["results"]:
        lines += [
            f"### {record['source_id']} — {record.get('title') or '(no title)'}",
            "",
            f"- publisher: {record.get('publisher')}",
            f"- source_platform: {record.get('source_platform')}",
            f"- publication_date: {record.get('publication_date')}",
            f"- discovery_query: {record.get('discovery_query')}",
            f"- retrieval_url: {record.get('retrieval_url')}",
            f"- url_type_heuristic: {(record.get('derived') or {}).get('url_type_heuristic')}",
            f"- content_access_status: {record.get('content_access_status')}",
            "",
        ]
        if record.get("content"):
            lines += ["```text", record["content"], "```", ""]
    return "\n".join(lines) + "\n"


def _records_markdown(title: str, payload: dict[str, Any], records: list[SourceRecord]) -> str:
    lines = [f"# {title}", ""]
    for key in ("index_url", "articles_collected", "gated_sources", "reposts_found",
                "search_key", "filings_collected", "assignee", "patents_collected",
                "total_reported_by_endpoint", "endpoint", "generated_at"):
        if key in payload:
            lines.append(f"- {key}: {payload[key]}")
    lines += ["", f"> {payload['note']}", ""]

    for failure_key in ("failures", "unresolved"):
        items = payload.get(failure_key) or []
        if items:
            lines += [f"## {failure_key}", ""]
            lines += [f"- {item}" for item in items]
            lines.append("")

    lines += ["## Sources", ""]
    for record in records:
        lines += [
            f"### {record.source_id} — {record.title or '(no title)'}",
            "",
            f"- publisher: {record.publisher}",
            f"- publication_date: {record.publication_date}",
            f"- source_type: {record.source_type}",
            f"- canonical_url: {record.canonical_url}",
            f"- content_access_status: {record.content_access_status}",
        ]
        if record.extra.get("reposts_source_id"):
            lines.append(f"- reposts_source_id: {record.extra['reposts_source_id']}")
            lines.append(f"- original_url: {record.extra.get('original_url')}")
        lines += ["", "```text", record.content or "", "```", ""]
    return "\n".join(lines) + "\n"


def _names_markdown(company: str, result: dict[str, Any]) -> str:
    lines = [
        f"# Stage 0 — Name Resolution: {company}",
        "",
        f"- input_name: {result.get('input_name')}",
        f"- canonical_english: {result.get('canonical_english')}",
        f"- provider: {result.get('provider')}",
        f"- generated_at: {result.get('generated_at')}",
        "",
        f"> {result.get('note', '')}",
        "",
        "## Search names used (in order)",
        "",
    ]
    lines += [f"{i}. {n}" for i, n in enumerate(result.get("search_names") or [], 1)]
    if result.get("search_names_dropped"):
        lines += ["", "### Not used (over max_names)", ""]
        lines += [f"- {n}" for n in result["search_names_dropped"]]

    chinese = result.get("chinese_names") or []
    if chinese:
        lines += ["", "## Chinese names", "",
                  "| Name | Type | Confidence | Note |", "| --- | --- | --- | --- |"]
        for entry in chinese:
            if isinstance(entry, dict):
                lines.append(
                    f"| {entry.get('name')} | {entry.get('type')} | "
                    f"{entry.get('confidence')} | {entry.get('note', '')} |"
                )

    if result.get("english_variants"):
        lines += ["", "## English variants", "",
                  ", ".join(str(v) for v in result["english_variants"])]

    collisions = result.get("collisions") or []
    if collisions:
        lines += ["", "## Name collisions — NOT the target company", ""]
        for entry in collisions:
            if isinstance(entry, dict):
                lines.append(f"- **{entry.get('name')}** — {entry.get('note', '')}")

    if result.get("raw_text"):
        lines += ["", "## Raw model output", "", "```json", result["raw_text"], "```"]
    return "\n".join(lines) + "\n"

# What each origin is, in the reader's terms rather than the code's.
_CHANNEL_NAMES = {
    "exchange_filing_text": "exchange filings (full text)",
    "exchange_filing_registry": "exchange filings (PDF links)",
    "patent_registry": "patents",
    "local_domain_search": "Chinese local domains (procurement, 工商)",
    "provider_search": "Baidu search",
    "stage2_model_output": "stage 2 collection",
    "repost_resolution": "reposts of gated originals",
    "provider_citation": "provider citations",
}


def index_markdown(company: str, records: list[SourceRecord]) -> str:
    """One compact table of every source, strongest evidence first.

    This exists so a downstream model can decide what to open without reading
    the corpus. Measured on one run: 2.3MB on disk for 43KB of actual content,
    so blind reading is almost entirely wasted tokens. Everything here is one
    line per source — id, grade, date, host, title, file.
    """
    from .parsing import _grade_rank

    ranked = sorted(
        records,
        key=lambda r: (
            _grade_rank(r.content_access_status),
            r.extra.get("names_company") is False,
            r.extra.get("cluster_role") == "duplicate_coverage",
            -(len(r.content or "")),
        ),
    )

    lines = [
        f"# Source index — {company}",
        "",
        f"{len(records)} sources. Read this file first, then open only what you need;",
        "the corpus as a whole does not fit in a context window.",
        "",
        "Grades, strongest first: VERBATIM_FULL_TEXT, VERBATIM_PARTIAL_TEXT,",
        "TRANSCRIPT_EXTRACTED, HIGH_FIDELITY_EXTRACTION, SEARCH_SNIPPET_ONLY, URL_ONLY.",
        "`dup` marks repeated coverage of a story already listed above — skip unless",
        "you need a second account of it.",
        "",
        "`off` marks a search result whose title and snippet never name the",
        "company — usually unrelated, occasionally a genuine industry piece.",
        "",
    ]

    # A reader should be able to pick a channel without scanning every row.
    # Measured: a 322-source index is 49KB, so the table alone costs ~12k
    # tokens to skim, and most questions only need one channel.
    by_origin: dict[str, list[SourceRecord]] = {}
    for record in records:
        by_origin.setdefault(record.origin or "unknown", []).append(record)
    if by_origin:
        lines += [
            "## What is here",
            "",
            "| channel | sources | chars | strongest grade |",
            "| --- | --- | --- | --- |",
        ]
        for origin, group in sorted(
            by_origin.items(), key=lambda kv: -sum(len(r.content or "") for r in kv[1])
        ):
            chars = sum(len(r.content or "") for r in group)
            best = min(
                (r.content_access_status or "URL_ONLY" for r in group),
                key=_grade_rank,
            )
            lines.append(
                f"| {_CHANNEL_NAMES.get(origin, origin)} | {len(group)} | "
                f"{chars:,} | {best} |"
            )
        lines.append("")

    lines += [
        "## Every source",
        "",
        "| file | grade | date | source | title | chars | dup | off |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    from urllib.parse import urlparse

    for record in ranked:
        path = record.extra.get("_file") or record.source_id
        grade = (record.content_access_status or "?").replace("_", " ").title().replace(" ", "")
        date = (record.publication_date or "")[:10]
        url = record.canonical_url or record.retrieval_url or ""
        host = urlparse(url).netloc.replace("www.", "")[:24] if url else ""
        title = (record.title or "").replace("|", "／")[:52]
        chars = len(record.content or "")
        dup = "dup" if record.extra.get("cluster_role") == "duplicate_coverage" else ""
        off = "off" if record.extra.get("names_company") is False else ""
        lines.append(
            f"| {path} | {grade} | {date} | {host} | {title} | {chars} | "
            f"{dup} | {off} |"
        )

    return "\n".join(lines) + "\n"
