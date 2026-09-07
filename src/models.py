"""Data structures shared by the pipeline, providers and storage.

Design rule for this whole module: **never invent metadata.** Every field a
source may or may not carry is Optional and defaults to ``None``. A missing
value is serialised as JSON ``null``, not as an empty string or a guess.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

# CONTENT_ACCESS_STATUS values defined by prompt 2. The pipeline only ever
# assigns the two weakest ones itself; the stronger labels can only come from
# the model's own output.
CONTENT_ACCESS_STATUSES = (
    "VERBATIM_FULL_TEXT",
    "VERBATIM_PARTIAL_TEXT",
    "TRANSCRIPT_EXTRACTED",
    "HIGH_FIDELITY_EXTRACTION",
    "SEARCH_SNIPPET_ONLY",
    "URL_ONLY",
)

URL_TYPES = (
    "STABLE_PUBLIC_URL",
    "STABLE_WECHAT_ARTICLE_URL",
    "STABLE_VIDEO_URL",
    "DIRECT_DOCUMENT_URL",
    "TEMPORARY_SOGOU_SIGNED_URL",
    "TEMPORARY_SESSION_URL",
    "UNKNOWN",
)

# Query params that mark a URL as session-scoped rather than canonical.
_EPHEMERAL_PARAMS = ("timestamp", "signature", "token", "sign", "expire", "expires")

# WeChat's legacy query-form article link (/s?__biz=...&mid=...&sn=...). It is not
# the canonical short form (/s/<id>) even though it carries no signature, so it
# must not be reported as a stable canonical WeChat URL.
_WECHAT_QUERY_PARAMS = {"__biz", "mid", "sn", "chksm", "idx", "scene"}
_WECHAT_SHORT_PATH = re.compile(r"^/s/[A-Za-z0-9_\-]+/?$")


@dataclass
class ResearchResponse:
    """Provider-agnostic result of running one research prompt.

    ``raw`` holds the untouched provider payload and is always persisted, so no
    provider-specific field is ever lost to normalisation.
    """

    text: str
    raw: dict[str, Any]
    provider: str
    model: Optional[str] = None
    search_results: list[dict[str, Any]] = field(default_factory=list)
    usage: Optional[dict[str, Any]] = None
    request_id: Optional[str] = None
    finish_reason: Optional[str] = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceRecord:
    """One preserved source. Mirrors prompt 2's output block field-for-field."""

    source_id: str
    title: Optional[str] = None
    publisher: Optional[str] = None
    author: Optional[str] = None
    publication_date: Optional[str] = None
    source_platform: Optional[str] = None
    source_type: Optional[str] = None
    target_company: Optional[str] = None
    matched_entity: Optional[str] = None
    matched_alias: Optional[str] = None
    discovery_query: Optional[str] = None
    retrieval_url: Optional[str] = None
    canonical_url: Optional[str] = None
    url_type: Optional[str] = None
    reaccess_status: Optional[str] = None
    content_access_status: Optional[str] = None
    transcript_method: Optional[str] = None
    content: Optional[str] = None
    # Provenance of the record itself, so downstream Claude can weight it.
    origin: Optional[str] = None  # stage2_model_output | wsa_search | hunyuan_citation
    extra: dict[str, Any] = field(default_factory=dict)
    derived: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # -- YAML front matter for raw_sources/*.md ---------------------------
    def front_matter(self) -> str:
        ordered = [
            "source_id",
            "title",
            "publisher",
            "author",
            "publication_date",
            "source_platform",
            "source_type",
            "target_company",
            "matched_entity",
            "matched_alias",
            "discovery_query",
            "retrieval_url",
            "canonical_url",
            "url_type",
            "reaccess_status",
            "content_access_status",
            "transcript_method",
            "origin",
        ]
        lines = ["---"]
        data = self.to_dict()
        for key in ordered:
            lines.append(f"{key}: {_yaml_scalar(data.get(key))}")
        if self.derived:
            lines.append("derived:")
            for key, value in self.derived.items():
                lines.append(f"  {key}: {_yaml_scalar(value)}")
        lines.append("---")
        return "\n".join(lines)

    def to_markdown(self) -> str:
        body = self.content if self.content else "(no content was accessible for this source)"
        return f"{self.front_matter()}\n\n# {self.title or self.source_id}\n\n{body}\n"


def _yaml_scalar(value: Any) -> str:
    """Quote defensively — Chinese titles routinely contain ``:`` and ``#``."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\n", " ").replace("\r", " ")
    return f'"{text}"'


def classify_url(url: Optional[str]) -> dict[str, Any]:
    """Heuristic URL classification.

    This is stored under ``SourceRecord.derived`` and deliberately kept apart
    from ``url_type``: whatever the model reported stays untouched, while this
    gives the downstream pipeline a mechanical second opinion. Implements the
    ephemeral-link rule from prompt 2 §7.
    """
    if not url:
        return {
            "url_type_heuristic": None,
            "is_ephemeral": None,
            "host": None,
            "wechat_url_form": None,
        }

    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        params = {k.lower() for k in parse_qs(parsed.query)}
    except ValueError as exc:
        # Models emit things like "[1]" or bracketed placeholders as URLs.
        # urlparse raises "Invalid IPv6 URL" on those; a bad URL must not sink
        # a run whose evidence is otherwise fine.
        return {
            "url_type_heuristic": "UNKNOWN",
            "is_ephemeral": None,
            "host": None,
            "wechat_url_form": None,
            "url_parse_error": str(exc),
        }
    ephemeral = bool(params & set(_EPHEMERAL_PARAMS)) or "src=11" in parsed.query

    wechat_form = None
    if "mp.weixin.qq.com" in host:
        if ephemeral:
            guess = "TEMPORARY_SOGOU_SIGNED_URL"
        elif _WECHAT_SHORT_PATH.match(parsed.path):
            wechat_form = "short_path"
            guess = "STABLE_WECHAT_ARTICLE_URL"
        elif params & _WECHAT_QUERY_PARAMS:
            # Legacy query form: re-openable in principle, but not canonical.
            wechat_form = "query_legacy"
            guess = "TEMPORARY_SESSION_URL"
        else:
            guess = "UNKNOWN"
    elif ephemeral:
        guess = "TEMPORARY_SESSION_URL"
    elif re.search(r"\.(pdf|docx?|xlsx?|pptx?)($|\?)", parsed.path, re.I):
        guess = "DIRECT_DOCUMENT_URL"
    elif any(v in host for v in ("v.qq.com", "channels.weixin.qq.com", "bilibili.com", "youku.com")):
        guess = "STABLE_VIDEO_URL"
    elif host:
        guess = "STABLE_PUBLIC_URL"
    else:
        guess = "UNKNOWN"

    return {
        "url_type_heuristic": guess,
        "is_ephemeral": ephemeral,
        "host": host or None,
        "wechat_url_form": wechat_form,
    }


def guess_platform(url: Optional[str], site: Optional[str] = None) -> Optional[str]:
    """Map a host to a platform label. Returns ``None`` rather than guessing."""
    if not url:
        return site or None
    try:
        host = (urlparse(url).netloc or "").lower()
    except ValueError:
        return site or None
    table = {
        "mp.weixin.qq.com": "WeChat Official Account",
        "channels.weixin.qq.com": "WeChat Channels (视频号)",
        "weixin.qq.com": "WeChat",
        "news.qq.com": "Tencent News",
        "new.qq.com": "Tencent News",
        "stockapp.finance.qq.com": "Tencent Securities",
        "finance.qq.com": "Tencent Finance",
        "weixin.sogou.com": "Sogou WeChat Search",
        "v.qq.com": "Tencent Video",
    }
    for needle, label in table.items():
        if needle in host:
            return label
    return site or host or None


@dataclass
class RunMetadata:
    """Contents of ``metadata.json``. Written after every state change."""

    target_company: str
    company_slug: str
    run_dir: str
    started_at: str
    provider: str
    model: Optional[str] = None
    completed_at: Optional[str] = None
    stage1_status: str = "pending"  # pending|running|completed|failed|skipped|loaded_from_disk
    stage2_status: str = "pending"
    search_sweep_status: str = "pending"
    local_sources_status: str = "pending"
    repost_status: str = "pending"
    filings_status: str = "pending"
    patents_status: str = "pending"
    patents_error: Optional[str] = None
    stage1_error: Optional[str] = None
    stage2_error: Optional[str] = None
    search_sweep_error: Optional[str] = None
    stage1_request_id: Optional[str] = None
    stage2_request_id: Optional[str] = None
    stage1_usage: Optional[dict[str, Any]] = None
    stage2_usage: Optional[dict[str, Any]] = None
    counts: dict[str, Any] = field(default_factory=dict)
    tool_version: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
