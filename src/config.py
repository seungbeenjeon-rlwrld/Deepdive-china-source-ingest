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
    "provider": "tencent",
    "output": {
        "root_dir": "./research",
        "save_markdown": True,
        "save_json": True,
        "save_raw_response": True,
        "save_raw_sources": True,
    },
    "research": {
        "stage1_prompt": "./prompts/prompt1_entity_discovery.md",
        "stage2_prompt": "./prompts/prompt2_source_collection.md",
        # Soft guard only: we warn, we never silently truncate evidence.
        "max_context_chars": 120000,
        # Retrieval we control, injected into the stage prompts. This replaces
        # a provider's own (often paid) search tool: it is free, it uses the
        # Baidu index, and the exact evidence given to the model is logged.
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
    "tencent": {
        # hunyuan-lite has no search-enhancement capability; do not use it.
        "model": "hunyuan-turbos-latest",
        "temperature": 0.4,
        "timeout_seconds": 900,
        "enable_enhancement": True,
        "force_search_enhancement": True,
        "citation": True,
        "search_info": True,
        "enable_multimedia": True,
        "enable_speed_search": False,
    },
    "zhipu": {
        # Z.ai international platform. glm-4.7-flash is free; glm-5.3 is the
        # strongest. Anything on the platform works.
        "model": "glm-4.7-flash",
        "base_url": "https://api.z.ai/api/paas/v4",
        "temperature": 0.4,
        "timeout_seconds": 900,
        "max_tokens": 32768,
        "thinking": False,
        # Paid add-on ($0.01/use), refused at zero balance. Prefer injected
        # retrieval, which is free and logs exactly what the model was given.
        "use_builtin_search": False,
        "search_engine": "search_pro_jina",
        "search_api_engine": "search-prime",
        "content_size": "high",
        "require_search": True,
        "search_count": 20,
        "search_recency": None,
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
    # The company's own newsroom: a primary source that carries much of what it
    # also posts to WeChat. Needs a per-company URL, so it is off by default.
    "official_site": {
        "enabled": False,
        "index_url": None,
        "page_param": "page",
        "max_pages": 3,
        "max_articles": 40,
        "detail_pattern": r"/detail/\d+\.html",
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
    },
    # Second evidence channel: structured search over the queries stage 1
    # recommended (SearchPro on Tencent, /web_search on Z.ai).
    "search_sweep": {
        "enabled": True,
        # Which provider runs the sweep. None = the main provider. Set to
        # "serpapi" to sweep the Baidu index while stages 1-2 run elsewhere.
        "provider": None,
        "max_queries": 12,
        "results_per_query": 20,
        "mode": 2,  # 0 web results, 1 VR cards, 2 mixed
        "freshness": None,  # e.g. "y2" for the last two years
        "site_filters": [None, "mp.weixin.qq.com"],
        "industries": [],  # e.g. ["gov", "acad"]
    },
}


@dataclass
class TencentSettings:
    secret_id: str | None = None
    secret_key: str | None = None
    region: str = "ap-guangzhou"
    model: str = "hunyuan-turbos-latest"
    temperature: float = 0.4
    timeout_seconds: int = 900
    enable_enhancement: bool = True
    force_search_enhancement: bool = True
    citation: bool = True
    search_info: bool = True
    enable_multimedia: bool = True
    enable_speed_search: bool = False

    @property
    def has_credentials(self) -> bool:
        return bool(self.secret_id and self.secret_key)


@dataclass
class ZhipuSettings:
    api_key: str | None = None
    model: str = "glm-4.7-flash"
    base_url: str = "https://api.z.ai/api/paas/v4"
    temperature: float = 0.4
    timeout_seconds: int = 900
    max_tokens: int | None = 32768
    thinking: bool = False
    use_builtin_search: bool = False
    # Engine names differ between the chat tool and the standalone endpoint.
    search_engine: str = "search_pro_jina"
    search_api_engine: str = "search-prime"
    content_size: str = "high"
    require_search: bool = True
    search_count: int = 20
    search_recency: str | None = None

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key)


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
    official_site: dict[str, Any]
    repost_resolution: dict[str, Any]
    registries: dict[str, Any]
    tencent: TencentSettings
    zhipu: ZhipuSettings
    serpapi: SerpApiSettings
    claude_cli: ClaudeCliSettings
    project_root: Path
    source_file: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def prompt_path(self, stage: int) -> Path:
        key = "stage1_prompt" if stage == 1 else "stage2_prompt"
        return self._resolve(self.research[key])

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

    tencent_cfg = data.get("tencent", {})
    tencent = TencentSettings(
        secret_id=os.getenv("TENCENT_SECRET_ID") or None,
        secret_key=os.getenv("TENCENT_SECRET_KEY") or None,
        region=os.getenv("TENCENT_REGION") or tencent_cfg.get("region") or "ap-guangzhou",
        model=os.getenv("TENCENT_MODEL") or tencent_cfg.get("model", "hunyuan-turbos-latest"),
        temperature=float(tencent_cfg.get("temperature", 0.4)),
        timeout_seconds=int(tencent_cfg.get("timeout_seconds", 900)),
        enable_enhancement=bool(tencent_cfg.get("enable_enhancement", True)),
        force_search_enhancement=bool(tencent_cfg.get("force_search_enhancement", True)),
        citation=bool(tencent_cfg.get("citation", True)),
        search_info=bool(tencent_cfg.get("search_info", True)),
        enable_multimedia=bool(tencent_cfg.get("enable_multimedia", True)),
        enable_speed_search=bool(tencent_cfg.get("enable_speed_search", False)),
    )

    zhipu_cfg = data.get("zhipu", {})
    zhipu = ZhipuSettings(
        api_key=os.getenv("ZHIPU_API_KEY") or os.getenv("ZAI_API_KEY") or None,
        model=os.getenv("ZHIPU_MODEL") or zhipu_cfg.get("model", "glm-4.7-flash"),
        base_url=os.getenv("ZHIPU_BASE_URL")
        or zhipu_cfg.get("base_url", "https://api.z.ai/api/paas/v4"),
        temperature=float(zhipu_cfg.get("temperature", 0.4)),
        timeout_seconds=int(zhipu_cfg.get("timeout_seconds", 900)),
        max_tokens=zhipu_cfg.get("max_tokens") or None,
        thinking=bool(zhipu_cfg.get("thinking", False)),
        use_builtin_search=bool(zhipu_cfg.get("use_builtin_search", False)),
        search_engine=zhipu_cfg.get("search_engine", "search_pro_jina"),
        search_api_engine=zhipu_cfg.get("search_api_engine", "search-prime"),
        content_size=zhipu_cfg.get("content_size", "high"),
        require_search=bool(zhipu_cfg.get("require_search", True)),
        search_count=int(zhipu_cfg.get("search_count", 20)),
        search_recency=zhipu_cfg.get("search_recency") or None,
    )

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

    provider = str(data.get("provider", "tencent")).strip().lower()
    if provider not in ("tencent", "zhipu", "serpapi", "claude-cli", "mock"):
        raise ValueError(
            f"unknown provider {provider!r} in config — "
            "expected tencent, zhipu, serpapi, claude-cli or mock"
        )

    return Config(
        provider=provider,
        output=data.get("output", {}),
        research=data.get("research", {}),
        search_sweep=data.get("search_sweep", {}),
        fetch=data.get("fetch", {}),
        official_site=data.get("official_site", {}),
        repost_resolution=data.get("repost_resolution", {}),
        registries=data.get("registries", {}),
        tencent=tencent,
        zhipu=zhipu,
        serpapi=serpapi,
        claude_cli=claude_cli,
        project_root=root,
        source_file=source_file,
        raw=data,
    )
