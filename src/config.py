"""Configuration: ``config.yaml`` for behaviour, ``.env`` for credentials.

YAML is optional — if PyYAML or the file is missing we fall back to the
defaults below so the tool still runs on a bare ``pip install requests``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .utils import get_logger

DEFAULTS: dict[str, Any] = {
    "provider": "claude-cli",
    "output": {
        "root_dir": "./research",
        "save_markdown": True,
        "save_json": True,
        "save_raw_response": True,
        "save_raw_sources": True,
    },
    "research": {
        "stage0_prompt": "./prompts/prompt0_name_resolution.md",
        "stage1_prompt": "./prompts/prompt1_entity_discovery.md",
        "stage2_prompt": "./prompts/prompt2_source_collection.md",
        # Soft guard only: we warn, we never silently truncate evidence.
        "max_context_chars": 120000,
        # Retrieval we control, injected into the stage prompts. This replaces
        # a provider's own (often paid) search tool: it is free, it uses the
        # Baidu index, and the exact evidence given to the model is logged.
        # The three per-company channel inputs are derived from what stages 0-1
        # found rather than asked for. Locating the newsroom needs one network
        # probe, so it can be turned off independently.
        "derive_channels": {"enabled": True, "probe_filings": True},
        # Expand the one global name the user typed into Chinese search names.
        # Without this, a Chinese index both misses the company and returns
        # same-name companies instead.
        "name_resolution": {"enabled": True, "max_names": 8},
        # Check the model's CONTENT_ACCESS_STATUS claims against the retrieval
        # actually injected. Downgrades only; never upgrades.
        "verify_labels": True,
        "retrieval_injection": {
            "enabled": True,
            # Independent of search_sweep on purpose: disabling the sweep must
            # not silently blind the stage prompts. None = fall back to
            # search_sweep.provider.
            "provider": "serpapi",
            "seed_queries": [
                "{company}",
                "{company} 工商 法定代表人 注册资本",
                "{company} 子公司 分公司 合资",
                "{company} 产品 型号 发布",
                "{company} 融资 股东 估值",
                "{company} 客户 合作 订单",
            ],
            "results_per_query": 20,
            "max_results": 60,
            "chars_per_result": 220,
            # Snippets give the model nothing to preserve. Fetch the top
            # results' actual pages so SOURCE_CONTENT has real text in it.
            "fetch_pages": True,
            "fetch_top_n": 12,
            "chars_per_fetched_page": 3000,
        },
    },
    # Polite HTTP fetching for article bodies. Never used against hosts that
    # gate automated access (see collectors.GATED_HOSTS).
    "fetch": {
        "user_agent": "china-research/0.1 (research source-collection tool)",
        "delay_seconds": 1.5,
        "timeout_seconds": 30,
        "max_bytes": 3000000,
        "respect_robots": True,
    },
    # For sources whose original is gated, find a readable repost instead.
    "repost_resolution": {"enabled": True, "max_sources": 10},
    # Claude Code CLI as the LLM. No API key — uses the local CLI's existing
    # authentication. Shares the interactive subscription allowance.
    "claude_cli": {
        "binary": "claude",
        "model": None,              # None = the CLI's own default
        "timeout_seconds": 900,
        # Retrieval is injected by the pipeline, so the CLI's own tools are not
        # needed. Restricting them keeps runs reproducible and stops unlogged
        # searches from leaking into the evidence trail.
        "disallowed_tools": "WebSearch,WebFetch",
        "extra_args": [],
        "cwd": None,
    },
    # SerpApi — programmatic access to the Baidu index. Search-only.
    "serpapi": {
        "timeout_seconds": 120,          # Baidu queries routinely take 15-30s
        "simplified_chinese_only": True, # ct=2
    },
    # Primary-source registries. These are prompt 2 §10 priorities 1 and 5, and
    # they are what a chat window cannot do: enumerate the whole set.
    "registries": {
        "filings_search_key": None,   # listed-entity name for cninfo, e.g. 上纬新材
        "patent_assignee": None,      # legal entity name, e.g. 上海智元新创技术有限公司
        "max_filings": 60,
        "max_patents": 60,
        # Pull the text out of the primary filings. Off would leave the channel
        # stopping at the PDF link, which no chat-side fetch can decode.
        "extract_filing_text": True,
        "max_pdf_mb": 40,
        "max_section_chars": 40000,
    },
    # Second evidence channel: structured search over the queries stage 1
    "search_sweep": {
        "enabled": True,
        # Which provider runs the sweep. None = the main provider. Set to
        # "serpapi" to sweep the Baidu index while stages 1-2 run elsewhere.
        "provider": None,
        "max_queries": 6,
        "results_per_query": 20,
        "mode": 2,  # 0 web results, 1 VR cards, 2 mixed
        "freshness": None,  # e.g. "y2" for the last two years
        "site_filters": [None, "mp.weixin.qq.com"],
        "industries": [],  # e.g. ["gov", "acad"]
    },
}


@dataclass
class ClaudeCliSettings:
    binary: str = "claude"
    model: str | None = None
    timeout_seconds: int = 900
    disallowed_tools: str | None = "WebSearch,WebFetch"
    extra_args: list[str] = field(default_factory=list)
    cwd: str | None = None

    @property
    def has_credentials(self) -> bool:
        # The CLI carries its own auth; nothing for us to hold.
        return True


@dataclass
class SerpApiSettings:
    # Several keys may be supplied; the client rotates to the next one when a
    # key's monthly quota is exhausted. Useful for a team where each member has
    # their own key, or a paid key with a free one as backstop.
    api_keys: list[str] = field(default_factory=list)
    timeout_seconds: int = 120
    simplified_chinese_only: bool = True

    @property
    def api_key(self) -> str | None:
        """The first key. Kept so existing call sites and logs still work."""
        return self.api_keys[0] if self.api_keys else None

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_keys)


@dataclass
class Config:
    provider: str
    output: dict[str, Any]
    research: dict[str, Any]
    search_sweep: dict[str, Any]
    fetch: dict[str, Any]
    repost_resolution: dict[str, Any]
    registries: dict[str, Any]
    serpapi: SerpApiSettings
    claude_cli: ClaudeCliSettings
    project_root: Path
    source_file: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def prompt_path(self, stage: int) -> Path:
        key = {0: "stage0_prompt", 1: "stage1_prompt", 2: "stage2_prompt"}[stage]
        return self._resolve(
            self.research.get(key) or DEFAULTS["research"][key]
        )

    @property
    def research_root(self) -> Path:
        return self._resolve(self.output["root_dir"])

    def _resolve(self, value: str | Path) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.project_root / p).resolve()


def _collect_serpapi_keys() -> list[str]:
    """Gather SerpApi keys in a deterministic order, de-duplicated.

    Accepts either a comma-separated ``SERPAPI_KEYS`` or numbered variables
    (``SERPAPI_KEY``, ``SERPAPI_KEY_2``, ``SERPAPI_KEY_3``, ...). Numbered
    variables are read in order so rotation is predictable across runs.
    """
    keys: list[str] = []

    for raw in (os.getenv("SERPAPI_KEYS") or "").split(","):
        candidate = raw.strip()
        if candidate:
            keys.append(candidate)

    primary = (os.getenv("SERPAPI_KEY") or "").strip()
    if primary:
        keys.append(primary)

    index = 2
    while True:
        value = (os.getenv(f"SERPAPI_KEY_{index}") or "").strip()
        if not value:
            break
        keys.append(value)
        index += 1

    seen: set[str] = set()
    ordered: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = None, project_root: Path | None = None) -> Config:
    root = Path(project_root or Path(__file__).resolve().parent.parent)
    log = get_logger()

    candidate = Path(path) if path else root / "config.yaml"
    data = dict(DEFAULTS)
    source_file: Path | None = None

    if candidate.is_file():
        try:
            import yaml  # type: ignore

            loaded = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError("config root must be a mapping")
            data = _deep_merge(DEFAULTS, loaded)
            source_file = candidate
            log.debug("loaded config from %s", candidate)
        except ImportError:
            log.warning("PyYAML not installed — using built-in defaults for config")
        except Exception as exc:  # malformed YAML should not be fatal
            log.warning("could not parse %s (%s) — using built-in defaults", candidate, exc)
    elif path:
        raise FileNotFoundError(f"config file not found: {candidate}")

    serp_cfg = data.get("serpapi", {})
    serpapi = SerpApiSettings(
        api_keys=_collect_serpapi_keys(),
        timeout_seconds=int(serp_cfg.get("timeout_seconds", 120)),
        simplified_chinese_only=bool(serp_cfg.get("simplified_chinese_only", True)),
    )

    cli_cfg = data.get("claude_cli", {})
    claude_cli = ClaudeCliSettings(
        binary=os.getenv("CLAUDE_CLI_BINARY") or cli_cfg.get("binary") or "claude",
        model=os.getenv("CLAUDE_CLI_MODEL") or cli_cfg.get("model") or None,
        timeout_seconds=int(cli_cfg.get("timeout_seconds", 900)),
        disallowed_tools=cli_cfg.get("disallowed_tools") or None,
        extra_args=list(cli_cfg.get("extra_args") or []),
        cwd=cli_cfg.get("cwd") or None,
    )

    provider = str(data.get("provider", "claude-cli")).strip().lower()
    if provider not in ("claude-cli", "serpapi", "mock"):
        raise ValueError(
            f"unknown provider {provider!r} in config — "
            "expected claude-cli, serpapi or mock"
        )

    return Config(
        provider=provider,
        output=data.get("output", {}),
        research=data.get("research", {}),
        search_sweep=data.get("search_sweep", {}),
        fetch=data.get("fetch", {}),
        repost_resolution=data.get("repost_resolution", {}),
        registries=data.get("registries", {}),
        serpapi=serpapi,
        claude_cli=claude_cli,
        project_root=root,
        source_file=source_file,
        raw=data,
    )
