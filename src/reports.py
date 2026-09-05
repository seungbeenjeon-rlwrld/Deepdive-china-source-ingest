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