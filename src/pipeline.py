"""The two-stage research pipeline.

State is held here, never on the server. Stage 2 receives the **complete,
unmodified** stage 1 text injected into prompt 2 inside a delimited block:

    <STAGE_1_RESEARCH>
    ... full stage 1 output ...
    </STAGE_1_RESEARCH>

Stage 1 output is never summarised, trimmed or re-ordered before injection.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from .collectors import (
    CNINFO_QUERY_URL,
    PATENTS_QUERY_URL,
    ExchangeFilingCollector,
    OfficialSiteCollector,
    PatentCollector,
    OfficialSiteConfig,
    RepostResolver,
    is_gated,
)
from .config import Config
from .fetcher import FetchBlocked, FetchError, Fetcher, FetchPolicy
from .models import (
    ResearchResponse,
    RunMetadata,
    SourceRecord,
)
from .parsing import (
    _NEWS_KEYWORDS,
    cluster_by_title,
    dedupe_records,
    _find_listed_entity,
    _identity_tokens,
    _official_host_candidates,
    _parse_json_object,
    clean_queries,
    extract_recommended_queries,
    parse_collection_summary,
    parse_new_search_anchors,
    parse_source_blocks,
    verify_labels,
)
from .parsing import _record_from_citation, _record_from_page
from .provider import ProviderError, ResearchProvider
from .reports import _names_markdown, _records_markdown, _sweep_markdown, index_markdown
from .storage import (
    LocalStorageBackend,
    RAW_STAGE1,
    RAW_STAGE2,
    RAW_SWEEP,
    STAGE1_JSON,
    STAGE1_MD,
    STAGE2_JSON,
    STAGE2_MD,
    SWEEP_JSON,
    SWEEP_MD,
    md_document,
)
INDEX_MD = "00_INDEX.md"
NAMES_JSON, NAMES_MD = "00_name_resolution.json", "00_name_resolution.md"
OFFICIAL_JSON, OFFICIAL_MD = "04_official_site.json", "04_official_site.md"
FILINGS_JSON, FILINGS_MD = "06_exchange_filings.json", "06_exchange_filings.md"
PATENTS_JSON, PATENTS_MD = "07_patents.json", "07_patents.md"
REPOST_JSON, REPOST_MD = "05_reposts.json", "05_reposts.md"
from .utils import get_logger, utc_now_iso

RETRIEVAL_OPEN = "<SEARCH_RESULTS>"
RETRIEVAL_CLOSE = "</SEARCH_RESULTS>"

STAGE1_OPEN = "<STAGE_1_RESEARCH>"
STAGE1_CLOSE = "</STAGE_1_RESEARCH>"
TARGET_TOKEN = "{TARGET_COMPANY}"


Progress = Callable[[str], None]


@dataclass
class StageResult:
    stage: int
    response: ResearchResponse
    files: dict[str, str]
    parsed: dict[str, Any]


class Pipeline:
    def __init__(
        self,
        config: Config,
        provider: ResearchProvider,
        storage: LocalStorageBackend,
        metadata: RunMetadata,
        *,
        progress: Optional[Progress] = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.storage = storage
        self.metadata = metadata
        self.log = get_logger()
        self._progress = progress or (lambda _msg: None)
        self._fetcher: Optional[Fetcher] = None
        self._search_cache: dict[str, ResearchProvider] = {}
        # Every source written in this run, so duplicates can be merged and the
        # index rebuilt without re-reading the directory.
        self._saved: list[SourceRecord] = []

    def _get_fetcher(self) -> Fetcher:
        if self._fetcher is None:
            cfg = self.config.fetch
            self._fetcher = Fetcher(FetchPolicy(
                user_agent=cfg.get("user_agent") or FetchPolicy.user_agent,
                delay_seconds=float(cfg.get("delay_seconds", 1.5)),
                timeout_seconds=int(cfg.get("timeout_seconds", 30)),
                max_bytes=int(cfg.get("max_bytes", 3_000_000)),
                respect_robots=bool(cfg.get("respect_robots", True)),
            ))
        return self._fetcher

    def _sweep_provider(self) -> ResearchProvider:
        """The sweep may run on a different provider than stages 1-2.

        `serpapi` is search-only, so it cannot run the prompts — but it reaches
        the Baidu index, which measurably covers a different layer of Chinese
        sources. This lets stages 1-2 use a chat provider while the sweep uses
        Baidu.
        """
        return self._search_provider(self.config.search_sweep.get("provider"))

    def _retrieval_provider(self) -> ResearchProvider:
        """Search provider for prompt injection.

        Deliberately independent of ``search_sweep``. They were coupled once and
        turning the sweep off silently disabled retrieval injection too, so
        stage 2 ran blind and could only restructure stage 1's evidence. Falls
        back to the sweep's provider so the common case needs no extra config.
        """
        cfg = self.config.research.get("retrieval_injection") or {}
        name = cfg.get("provider") or self.config.search_sweep.get("provider")
        return self._search_provider(name)

    def _search_provider(self, name: Optional[str]) -> ResearchProvider:
        name = (name or "").strip().lower()
        if not name or name == self.provider.name:
            return self.provider
        if name not in self._search_cache:
            from .provider import ProviderError, build_provider

            try:
                self._search_cache[name] = build_provider(name, self.config)
                self.log.info("search provider: %s", name)
            except ProviderError as exc:
                # A missing sweep credential must not sink a run whose stages 1
                # and 2 already succeeded. Fall back and say so.
                self.log.warning(
                    "sweep provider %r unavailable (%s) — falling back to %s",
                    name, exc, self.provider.name,
                )
                self.metadata.notes.append(
                    f"search provider fell back from {name!r} to "
                    f"{self.provider.name!r}: {exc}"
                )
                self._search_cache[name] = self.provider
        return self._search_cache[name]

    def _next_source_index(self) -> int:
        return int(self.metadata.counts.get("raw_source_files", 0)) + 1

    def _persist_records(self, records: list[SourceRecord]) -> None:
        """Write new sources, skipping URLs already saved in this run."""
        if not self.config.output.get("save_raw_sources", True):
            return

        fresh = [r for r in records if not self._already_saved(r)]
        skipped = len(records) - len(fresh)
        if skipped:
            self.log.info("skipped %d source(s) already saved in this run", skipped)
            self.metadata.counts["duplicate_sources_skipped"] = (
                int(self.metadata.counts.get("duplicate_sources_skipped", 0)) + skipped
            )

        start = self._next_source_index()
        for offset, record in enumerate(fresh):
            self.storage.save_source(record, index=start + offset)
            self._saved.append(record)
        self.metadata.counts["raw_source_files"] = start + len(fresh) - 1
        self._write_index()

    def _already_saved(self, record: SourceRecord) -> bool:
        """Has this URL been written already? Merge into the existing record if so."""
        url = (record.canonical_url or record.retrieval_url or "").strip()
        if not url:
            return False
        for existing in self._saved:
            if (existing.canonical_url or existing.retrieval_url or "").strip() != url:
                continue
            seen = list(existing.extra.get("also_found_by") or [existing.origin])
            if record.origin and record.origin not in seen:
                seen.append(record.origin)
            existing.extra = {**existing.extra, "also_found_by": seen}
            return True
        return False

    def _write_index(self) -> None:
        """Rewrite the corpus index so it always reflects what is on disk."""
        if not self._saved or not self.config.output.get("save_markdown", True):
            return
        cluster_by_title(self._saved)
        self.storage.save(
            INDEX_MD, index_markdown(self.metadata.target_company, self._saved)
        )
        self.metadata.counts["indexed_sources"] = len(self._saved)

    # -- official newsroom crawl (primary source, prompt 2 §10 priority 2) --
    def run_official_site(self, company: str, index_url: str) -> dict[str, Any]:
        cfg = self.config.official_site
        self._progress(f"[+] Crawling official newsroom: {index_url}")
        self.metadata.official_site_status = "running"
        self.storage.write_metadata(self.metadata)

        collector = OfficialSiteCollector(self._get_fetcher(), OfficialSiteConfig(
            index_url=index_url,
            page_param=cfg.get("page_param", "page"),
            max_pages=int(cfg.get("max_pages", 3)),
            max_articles=int(cfg.get("max_articles", 40)),
            detail_pattern=cfg.get("detail_pattern", r"/detail/\d+\.html"),
        ))
        records, failures = collector.collect(company)

        payload = {
            "target_company": company,
            "index_url": index_url,
            "articles_collected": len(records),
            "failures": failures,
            "sources": [r.to_dict() for r in records],
            "generated_at": utc_now_iso(),
            "note": (
                "Crawled from the company's own newsroom, which serves its content to a "
                "normal request. This is a primary source (prompt 2 §10 priority 2) and "
                "carries much of the same material the company posts to WeChat."
            ),
        }
        if self.config.output.get("save_json", True):
            self.storage.save_json(OFFICIAL_JSON, payload)
        if self.config.output.get("save_markdown", True):
            self.storage.save(OFFICIAL_MD, _records_markdown(
                f"Stage 4 — Official Newsroom: {company}", payload, records))
        self._persist_records(records)

        self.metadata.official_site_status = "completed" if not failures else "completed_with_errors"
        self.metadata.counts["official_site_articles"] = len(records)
        self.metadata.counts["official_site_failures"] = len(failures)
        self.storage.write_metadata(self.metadata)
        self._progress(f"✓ Preserved {len(records)} official articles in full text")
        return payload

    # -- primary-source registries (prompt 2 §10 priorities 1 and 5) --------
    def run_exchange_filings(self, company: str, search_key: str) -> dict[str, Any]:
        self._progress(f"[+] Fetching exchange filings for {search_key}...")
        self.metadata.filings_status = "running"
        self.storage.write_metadata(self.metadata)

        cfg = self.config.registries
        records, failures = ExchangeFilingCollector(self._get_fetcher()).collect(
            company, search_key, max_records=int(cfg.get("max_filings", 60))
        )
        payload = {
            "target_company": company,
            "search_key": search_key,
            "filings_collected": len(records),
            "failures": failures,
            "sources": [r.to_dict() for r in records],
            "generated_at": utc_now_iso(),
            "endpoint": CNINFO_QUERY_URL,
            "note": (
                "Exchange/regulatory disclosures from 巨潮资讯网. Prompt 2 §10 priority 1 — "
                "these carry legal liability and settle what media only paraphrase. Each "
                "record keeps a DIRECT_DOCUMENT_URL to the PDF; the filing text is not "
                "extracted (prompt 2 §14: return the document link, do not rebuild it)."
            ),
        }
        if self.config.output.get("save_json", True):
            self.storage.save_json(FILINGS_JSON, payload)
        if self.config.output.get("save_markdown", True):
            self.storage.save(FILINGS_MD, _records_markdown(
                f"Stage 6 — Exchange Filings: {search_key}", payload, records))
        self._persist_records(records)

        self.metadata.filings_status = "completed" if not failures else "completed_with_errors"
        self.metadata.counts["exchange_filings"] = len(records)
        self.storage.write_metadata(self.metadata)
        self._progress(f"✓ Indexed {len(records)} exchange filings with direct PDF links")
        return payload

    def run_patents(self, company: str, assignee: str) -> dict[str, Any]:
        self._progress(f"[+] Fetching patents for {assignee}...")
        self.metadata.patents_status = "running"
        self.storage.write_metadata(self.metadata)

        cfg = self.config.registries
        records, failures, total = PatentCollector(self._get_fetcher()).collect(
            company, assignee, max_records=int(cfg.get("max_patents", 60))
        )
        payload = {
            "target_company": company,
            "assignee": assignee,
            "patents_collected": len(records),
            "total_reported_by_endpoint": total,
            "failures": failures,
            "sources": [r.to_dict() for r in records],
            "generated_at": utc_now_iso(),
            "endpoint": PATENTS_QUERY_URL,
            "note": (
                "CNIPA patents by assignee, via Google Patents. Prompt 2 §10 priority 5. "
                "Content is the published abstract as returned, not the full "
                "specification. This endpoint rate-limits bursts with HTTP 503; a "
                "throttled run reports a failure rather than an empty result."
            ),
        }
        if self.config.output.get("save_json", True):
            self.storage.save_json(PATENTS_JSON, payload)
        if self.config.output.get("save_markdown", True):
            self.storage.save(PATENTS_MD, _records_markdown(
                f"Stage 7 — Patents: {assignee}", payload, records))
        self._persist_records(records)

        if failures and not records:
            self.metadata.patents_status = "failed"
            self.metadata.patents_error = failures[0].get("error")
        else:
            self.metadata.patents_status = "completed" if not failures else "completed_with_errors"
        self.metadata.counts["patents"] = len(records)
        self.storage.write_metadata(self.metadata)
        if records:
            self._progress(f"✓ Indexed {len(records)} of {total} patents")
        else:
            self._progress(f"  patents unavailable: {failures[0].get('error') if failures else 'none found'}")
        return payload

    # -- repost resolution for sources whose original is gated --------------
    def run_repost_resolution(self, company: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
        cfg = self.config.repost_resolution
        if not cfg.get("enabled", True):
            self.metadata.repost_status = "disabled"
            return {"skipped": "disabled in config"}
        if not self.provider.supports_search:
            self.metadata.repost_status = "unsupported"
            return {"skipped": f"provider {self.provider.name} has no structured search"}

        blocked = [
            SourceRecord(**{k: v for k, v in s.items() if k in SourceRecord.__annotations__})
            for s in sources
            if s.get("content_access_status") == "URL_ONLY"
            and is_gated(s.get("canonical_url") or s.get("retrieval_url"))
        ]
        if not blocked:
            self.metadata.repost_status = "not_needed"
            return {"skipped": "no gated URL_ONLY sources to resolve"}

        self._progress(f"[+] Resolving reposts for {len(blocked)} unreadable source(s)...")
        self.metadata.repost_status = "running"
        self.storage.write_metadata(self.metadata)

        def search(title: str) -> list[dict[str, Any]]:
            return self.provider.search(title, count=10).get("pages", [])

        official_index = self.config.official_site.get("index_url")
        official_hosts: list[str] = []
        if official_index:
            official_hosts.append(urlparse(official_index).netloc)
        resolver = RepostResolver(
            self._get_fetcher(), search, official_hosts=official_hosts
        )
        records, gaps = resolver.resolve(
            blocked, company, max_sources=int(cfg.get("max_sources", 10))
        )

        payload = {
            "target_company": company,
            "gated_sources": len(blocked),
            "reposts_found": len(records),
            "unresolved": gaps,
            "sources": [r.to_dict() for r in records],
            "generated_at": utc_now_iso(),
            "note": (
                "Each record here is the full text OF A REPOST, not of the original. The "
                "originals are gated (WeChat serves a verification page to automated "
                "requests, which was not circumvented) and their records remain URL_ONLY. "
                "Wording may differ from the original; treat these as prompt 2 §10 "
                "priority-10 sources and prefer the original where it matters."
            ),
        }
        if self.config.output.get("save_json", True):
            self.storage.save_json(REPOST_JSON, payload)
        if self.config.output.get("save_markdown", True):
            self.storage.save(REPOST_MD, _records_markdown(
                f"Stage 5 — Repost Resolution: {company}", payload, records))
        self._persist_records(records)

        self.metadata.repost_status = "completed"
        self.metadata.counts["reposts_found"] = len(records)
        self.metadata.counts["reposts_unresolved"] = len(gaps)
        self.storage.write_metadata(self.metadata)
        self._progress(f"✓ Recovered {len(records)} of {len(blocked)} via readable reposts")
        return payload

    # -- prompt loading ---------------------------------------------------
    def _load_prompt(self, stage: int) -> str:
        path = self.config.prompt_path(stage)
        if not path.is_file():
            raise FileNotFoundError(
                f"prompt file for stage {stage} not found: {path}\n"
                f"Check research.stage{stage}_prompt in config.yaml."
            )
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError(f"prompt file is empty: {path}")
        return text

    def build_stage1_prompt(self, company: str) -> str:
        template = self._load_prompt(1)
        if TARGET_TOKEN not in template:
            self.log.warning(
                "%s does not contain %s — the company name cannot be injected. "
                "Appending it explicitly instead.",
                self.config.prompt_path(1).name,
                TARGET_TOKEN,
            )
            return f"{template.rstrip()}\n\n---\n\nTARGET_COMPANY: {company}\n"
        return template.replace(TARGET_TOKEN, company)

    def build_stage2_prompt(self, company: str, stage1_text: str) -> str:
        """Prompt 2 + the full stage 1 result. Nothing is summarised."""
        template = self._load_prompt(2)
        if not stage1_text.strip():
            raise ValueError("stage 1 result is empty — refusing to run stage 2 without context")

        # Guard against the model's own output closing our delimiter early.
        safe = stage1_text.replace(STAGE1_CLOSE, "</STAGE_1_RESEARCH_ESCAPED>")
        if safe != stage1_text:
            self.log.warning("stage 1 text contained the closing delimiter; it was escaped")

        block = (
            f"{STAGE1_OPEN}\n"
            f"TARGET_COMPANY: {company}\n\n"
            f"{safe.strip()}\n"
            f"{STAGE1_CLOSE}"
        )
        prompt = (
            f"{template.rstrip()}\n\n"
            "---\n\n"
            "以下是上一阶段（Prompt 1）完整的 Company Entity & Source Discovery Research 结果。\n"
            "请直接使用其中的信息，不要要求用户重新提供公司名称。\n\n"
            f"{block}\n"
        )

        limit = int(self.config.research.get("max_context_chars") or 0)
        if limit and len(prompt) > limit:
            # Warn, never truncate: losing stage 1 evidence defeats the purpose.
            self.log.warning(
                "stage 2 prompt is %d chars, above the %d char soft limit. Sending it in "
                "full anyway; if the API rejects it, raise research.max_context_chars or "
                "use a larger-context model.",
                len(prompt),
                limit,
            )
            self.metadata.notes.append(
                f"stage2 prompt length {len(prompt)} exceeded soft limit {limit} (sent in full)"
            )
        return prompt

    # -- stage 0: expand one global name into Chinese search names ---------
    def resolve_names(self, company: str) -> dict[str, Any]:
        """Turn the single name the user typed into names worth searching.

        The user supplies one global (usually English) name. Searching a Chinese
        index with that alone both misses and misleads — measured on "AgiBot",
        Baidu returned agibot.net (AGIBOT敏捷机器人, a surgical-robotics company)
        alongside the intended one. Expanding to the Chinese brand and legal
        entity names first is what makes every later query land.

        The names are **candidates**, not facts. Stage 1 verifies them against
        sources; this step only decides what to look for.
        """
        cfg = self.config.research.get("name_resolution") or {}
        if not cfg.get("enabled", True):
            return {"skipped": "name_resolution disabled", "search_names": [company]}

        path = self.config.prompt_path(0)
        if not path.is_file():
            self.log.warning("name-resolution prompt missing at %s — using the raw name", path)
            return {"skipped": f"prompt not found: {path}", "search_names": [company]}

        self._progress("[0/2] Resolving Chinese names...")
        prompt = path.read_text(encoding="utf-8").replace(TARGET_TOKEN, company)

        try:
            response = self.provider.run_research(prompt, label="stage0")
        except ProviderError as exc:
            # Never fatal: fall back to the raw name and say so.
            self.log.warning("name resolution failed (%s) — using the raw name", exc)
            self.metadata.notes.append(f"name resolution failed: {exc}")
            return {"error": str(exc), "search_names": [company]}

        parsed = _parse_json_object(response.text)
        if parsed is None:
            self.log.warning("name resolution returned unparseable output — using the raw name")
            self.metadata.notes.append("name resolution output was not valid JSON")
            return {
                "error": "output was not valid JSON",
                "raw_text": response.text,
                "search_names": [company],
            }

        names = [n for n in (parsed.get("search_names") or []) if isinstance(n, str) and n.strip()]
        # Always keep what the user typed: it is the one name we know they meant.
        if company not in names:
            names.insert(0, company)
        limit = int(cfg.get("max_names", 8))

        result = {
            **parsed,
            "input_name": company,
            "search_names": names[:limit],
            "search_names_dropped": names[limit:],
            "provider": response.provider,
            "model": response.model,
            "generated_at": utc_now_iso(),
            "raw_text": response.text,
            "note": (
                "These are search candidates produced by a model, not verified facts. "
                "Stage 1 checks them against sources. 'collisions' lists same-name "
                "companies that are NOT the target."
            ),
        }

        chinese = [n for n in result["search_names"] if any("\u4e00" <= ch <= "\u9fff" for ch in n)]
        self._progress(
            f"      {len(result['search_names'])} search names "
            f"({len(chinese)} Chinese), {len(parsed.get('collisions') or [])} name collision(s)"
        )
        if not chinese:
            self.metadata.notes.append(
                "name resolution produced no Chinese names; Chinese-index recall will be poor"
            )

        if self.config.output.get("save_json", True):
            self.storage.save_json(NAMES_JSON, result)
        if self.config.output.get("save_markdown", True):
            self.storage.save(NAMES_MD, _names_markdown(company, result))

        self.metadata.counts["search_names"] = len(result["search_names"])
        self.metadata.counts["name_collisions"] = len(parsed.get("collisions") or [])
        self.storage.write_metadata(self.metadata)
        return result

    # -- derive the per-company channel inputs instead of asking ----------
    def derive_channels(
        self, names_meta: dict[str, Any], stage1_text: str
    ) -> dict[str, Any]:
        """Work out the three channel inputs from what stages 0-1 already found.

        These used to be three interactive questions, which was the same mistake
        as asking the user for Chinese names: the pipeline already knows them.

        - patent assignee  -> stage 0's legal-entity name
        - listed entity    -> a stock code in stage 1's output, with its name
        - newsroom index   -> stage 1's most-cited official domain, then its
                              news listing page
        """
        derived: dict[str, Any] = {"patent_assignee": None, "filings_search_key": None,
                                   "official_site": None, "evidence": {}}

        # 1) Patent assignee: the registered legal entity, not the brand.
        legal = [
            e for e in (names_meta.get("chinese_names") or [])
            if isinstance(e, dict) and e.get("type") == "legal_entity" and e.get("name")
        ]
        rank = {"high": 0, "medium": 1, "low": 2}
        legal.sort(key=lambda e: rank.get(str(e.get("confidence")), 3))
        # Prefer a full 有限公司 / 股份有限公司 name; a partnership is a holding
        # vehicle, not the operating entity that files patents.
        preferred = [e for e in legal if "有限公司" in e["name"] and "合伙" not in e["name"]]
        chosen = (preferred or legal)
        if chosen:
            derived["patent_assignee"] = chosen[0]["name"]
            derived["evidence"]["patent_assignee"] = (
                f"stage 0 legal_entity, confidence={chosen[0].get('confidence')}"
            )

        # 2) Listed entity: a mainland stock code names the company right before it.
        listed = _find_listed_entity(stage1_text)
        if listed:
            derived["filings_search_key"] = listed["name"]
            derived["evidence"]["filings_search_key"] = (
                f"stage 1 mentions {listed['code']} next to {listed['name']!r}"
            )

        # 3) Newsroom index: find the official domain, then its news listing.
        # This one needs a network probe, so it is separately switchable — tests
        # and offline runs must not reach out.
        cfg = self.config.research.get("derive_channels") or {}
        site = (
            self._find_newsroom(stage1_text, names_meta)
            if cfg.get("probe_official_site", True)
            else None
        )
        if site:
            derived["official_site"] = site["url"]
            derived["evidence"]["official_site"] = site["why"]

        return derived

    def _find_newsroom(
        self, stage1_text: str, names_meta: dict[str, Any]
    ) -> Optional[dict[str, str]]:
        """Locate the company's own news listing page.

        Two checks matter. The host has to actually be the company's — stage 1
        cites job boards and quote pages too, and without verification this
        happily returned jobui.com's news feed. And the link has to be a listing
        page, not one article.
        """
        hosts = _official_host_candidates(stage1_text)
        if not hosts:
            return None

        # Tokens that confirm a homepage really belongs to the target. Full
        # names are too strict: the brand is 智元机器人 but the official site
        # titles itself 智元创新（上海）科技股份有限公司, sharing only 智元.
        raw_names = [n for n in (names_meta.get("search_names") or []) if n]
        for entry in names_meta.get("chinese_names") or []:
            if isinstance(entry, dict) and entry.get("name"):
                raw_names.append(entry["name"])
        if names_meta.get("canonical_english"):
            raw_names.append(str(names_meta["canonical_english"]))
        expected = _identity_tokens(raw_names)

        fetcher = self._get_fetcher()
        for host in hosts[:4]:
            try:
                page = fetcher.fetch(f"https://{host}/")
            except (FetchError, FetchBlocked) as exc:
                self.log.debug("newsroom probe failed for %s: %s", host, exc)
                continue
            if page.blocked:
                continue

            # Does this site actually claim to be the company?
            haystack = f"{page.title or ''} {(page.text or '')[:3000]}"
            if expected and not any(name in haystack for name in expected):
                self.log.debug("%s does not mention the company — skipping", host)
                continue

            final_host = urlparse(page.final_url).netloc.lower().removeprefix("www.")
            for label, url in page.links:
                link_host = urlparse(url).netloc.lower().removeprefix("www.")
                if link_host not in (host, final_host):
                    continue
                path = urlparse(url).path
                if not any(k in f"{label} {url}".lower() for k in _NEWS_KEYWORDS):
                    continue
                # A listing page, not an individual article.
                if re.search(r"/detail/\d+|/\d{6,}|\.html?$", path) and "list" not in path:
                    continue
                return {"url": url, "why": f"news link on https://{final_host}/ "
                                          f"(page mentions the company)"}
        return None

    # -- retrieval we control, injected into the stage prompts -------------
    def build_retrieval_block(
        self, company: str, queries: list[str]
    ) -> tuple[Optional[str], dict[str, Any]]:
        """Run searches ourselves and format them for injection.

        This exists because a provider's own search tool is often a paid add-on
        (Zhipu's is $0.01/use and is refused at zero balance), and because
        retrieval we run is *auditable*: the exact evidence handed to the model
        is written to disk, so a downstream reader can check what the model
        could and could not have seen.
        """
        cfg = self.config.research.get("retrieval_injection") or {}
        if not cfg.get("enabled", True):
            return None, {"skipped": "retrieval_injection disabled"}

        searcher = self._retrieval_provider()
        if not searcher.supports_search:
            return None, {
                "skipped": f"provider {searcher.name} has no structured search",
                "hint": "set research.retrieval_injection.provider to serpapi",
            }

        per_query = int(cfg.get("results_per_query", 20))
        cap = int(cfg.get("max_results", 60))
        clip = int(cfg.get("chars_per_result", 220))
        fetch_pages = bool(cfg.get("fetch_pages", True))
        fetch_top_n = int(cfg.get("fetch_top_n", 12))
        page_clip = int(cfg.get("chars_per_fetched_page", 3000))

        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        failures: list[dict[str, str]] = []

        for query in queries:
            if len(collected) >= cap:
                break
            self._progress(f"      retrieval: {query}")
            try:
                result = searcher.search(query, count=per_query)
            except ProviderError as exc:
                self.log.warning("retrieval query %r failed: %s", query, exc)
                failures.append({"query": query, "error": str(exc)})
                continue
            for page in result.get("pages", []):
                url = (page.get("url") or "").strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                collected.append({**page, "_query": query})
                if len(collected) >= cap:
                    break

        if not collected:
            return None, {
                "skipped": "no retrieval results",
                "failures": failures,
                "queries": queries,
            }

        # Snippets alone give the model nothing to preserve — measured, it then
        # emits source blocks with metadata and an empty SOURCE_CONTENT. So
        # fetch the top results' actual pages and inject their text. The same
        # fetcher that crawls the official newsroom does this; gated hosts are
        # never fetched.
        fetched = 0
        fetch_failures: list[dict[str, str]] = []
        if fetch_pages:
            fetcher = self._get_fetcher()
            for page in collected[:fetch_top_n]:
                url = page.get("url") or ""
                if is_gated(url):
                    page["_fetch_skipped"] = "gated host"
                    continue
                self._progress(f"      reading: {(page.get('title') or url)[:56]}")
                try:
                    result = fetcher.fetch(url)
                except (FetchError, FetchBlocked) as exc:
                    fetch_failures.append({"url": url, "error": str(exc)})
                    continue
                if result.blocked:
                    page["_fetch_skipped"] = result.block_reason
                    fetch_failures.append({"url": url, "error": result.block_reason or "blocked"})
                    continue
                if result.text and len(result.text) > 200:
                    page["_full_text"] = result.text[:page_clip]
                    page["_full_text_chars"] = len(result.text)
                    page["_fetched_title"] = result.title
                    page["_fetched_date"] = result.published
                    fetched += 1
                else:
                    # Record it. A silent skip made 16 of 40 results vanish
                    # without trace in a measured run.
                    reason = (
                        "no extractable body text" if not result.text
                        else f"body too short ({len(result.text)} chars)"
                    )
                    page["_fetch_skipped"] = reason
                    fetch_failures.append({"url": url, "error": reason})

        lines = [RETRIEVAL_OPEN]
        for index, page in enumerate(collected, start=1):
            lines.append(f"[{index}] {page.get('_fetched_title') or page.get('title') or '(no title)'}")
            lines.append(f"    URL: {page.get('url')}")
            date = page.get("_fetched_date") or page.get("publication_date")
            if date:
                lines.append(f"    DATE: {date}")
            body = page.get("_full_text")
            if body:
                # Flagged so the model can tell what it may quote verbatim.
                lines.append(f"    FULL_TEXT ({page['_full_text_chars']} chars retrieved):")
                lines.append(body)
            else:
                snippet = (page.get("content") or "").strip().replace("\n", " ")
                if snippet:
                    lines.append(f"    SNIPPET_ONLY: {snippet[:clip]}")
        lines.append(RETRIEVAL_CLOSE)

        meta = {
            "provider": searcher.name,
            "queries": queries,
            "results": len(collected),
            "pages_fetched": fetched,
            "fetch_failures": fetch_failures,
            "failures": failures,
            "sources": collected,
            "chars": sum(len(line) for line in lines),
        }
        return "\n".join(lines), meta

    def _seed_queries(self, company: str, names: Optional[list[str]] = None) -> list[str]:
        """Cross the query templates with every resolved name.

        One English name yields one weak query set; the Chinese brand and legal
        entity names are what reach 工商, 招投标 and 公众号 content. Capped so a
        long name list cannot blow the search budget.
        """
        cfg = self.config.research.get("retrieval_injection") or {}
        templates = cfg.get("seed_queries") or ["{company}"]
        pool = [n for n in (names or [company]) if n] or [company]
        cap = int(cfg.get("max_seed_queries", 8))

        queries: list[str] = []
        # Name-major order: the best name gets every template before the next
        # name is tried, so truncation loses the weakest names, not the best
        # query types.
        for name in pool:
            for template in templates:
                query = template.replace("{company}", name)
                if query not in queries:
                    queries.append(query)
            if len(queries) >= cap:
                break
        return queries[:cap]

    @staticmethod
    def _with_retrieval(prompt: str, block: Optional[str]) -> str:
        if not block:
            return prompt
        return (
            f"{prompt.rstrip()}\n\n---\n\n"
            "以下是通过中文搜索引擎（百度）检索并实际抓取取得的资料。\n"
            "请优先基于这些资料作答；不要把记忆中的信息当作已验证事实。\n"
            "资料不足时请明确标记 Unverified。\n\n"
            "标记为 FULL_TEXT 的条目是实际抓取到的正文，"
            "你必须在 SOURCE_CONTENT 中原样保留其中的关键段落、数字、日期、人名、"
            "职位、型号与引语，并可标记 VERBATIM_PARTIAL_TEXT。\n"
            "标记为 SNIPPET_ONLY 的条目只有搜索摘要，"
            "只能标记 SEARCH_SNIPPET_ONLY，不得声称是原文。\n\n"
            f"{block}\n"
        )

    # -- stage 1 ----------------------------------------------------------
    def run_stage1(self, company: str) -> StageResult:
        self.metadata.stage1_status = "running"
        self.storage.write_metadata(self.metadata)

        names_meta = self.resolve_names(company)
        search_names = names_meta.get("search_names") or [company]

        self._progress("[1/2] Discovering company entities...")
        base_prompt = self.build_stage1_prompt(company)
        if len(search_names) > 1:
            # Tell stage 1 what the retrieval was built from, and that the names
            # are candidates it must verify rather than accept.
            base_prompt += (
                "\n\n---\n\n以下名称候选由上一步生成，用于构建本次检索；"
                "它们**尚未经过验证**，请在你的 Entity / Alias Dictionary 中逐一核实：\n"
                + "\n".join(f"- {n}" for n in search_names)
            )
            collisions = names_meta.get("collisions") or []
            if collisions:
                base_prompt += (
                    "\n\n已知同名但可能无关的实体（请勿与目标公司合并）：\n"
                    + "\n".join(
                        f"- {c.get('name')}：{c.get('note', '')}"
                        for c in collisions if isinstance(c, dict)
                    )
                )

        block, retrieval_meta = self.build_retrieval_block(
            company, self._seed_queries(company, search_names)
        )
        prompt = self._with_retrieval(base_prompt, block)
        self.log.info(
            "stage 1 prompt: %d chars (retrieval: %s)",
            len(prompt), retrieval_meta.get("results", retrieval_meta.get("skipped")),
        )
        if block:
            self._progress(
                f"      injected {retrieval_meta['results']} results from "
                f"{retrieval_meta['provider']} "
                f"({retrieval_meta.get('pages_fetched', 0)} pages read in full)"
            )

        try:
            response = self.provider.run_research(prompt, label="stage1")
        except ProviderError:
            self.metadata.stage1_status = "failed"
            raise

        for warning in response.warnings:
            self.log.warning("stage 1: %s", warning)

        citations = [
            _record_from_citation(c, company, index=i)
            for i, c in enumerate(response.search_results, start=1)
        ]
        parsed = {
            "stage": 1,
            "target_company": company,
            "text": response.text,
            "citations": [c.to_dict() for c in citations],
            "recommended_queries": extract_recommended_queries(response.text),
            "provider": response.provider,
            "model": response.model,
            "request_id": response.request_id,
            "finish_reason": response.finish_reason,
            "usage": response.usage,
            "warnings": response.warnings,
            "generated_at": utc_now_iso(),
            "prompt_chars": len(prompt),
            "retrieval": retrieval_meta,
            "name_resolution": names_meta,
        }

        files = self._persist_stage(
            1,
            title=f"Stage 1 — Entity & Source Discovery: {company}",
            response=response,
            parsed=parsed,
            md_name=STAGE1_MD,
            json_name=STAGE1_JSON,
            raw_name=RAW_STAGE1,
            extra_header=[
                f"- recommended_queries_found: {len(parsed['recommended_queries'])}",
                f"- search_names_used: {len(search_names)}",
            ],
        )

        self.metadata.stage1_status = "completed"
        self.metadata.stage1_request_id = response.request_id
        self.metadata.stage1_usage = response.usage
        self.metadata.model = response.model or self.metadata.model
        self.metadata.counts["stage1_citations"] = len(citations)
        self.metadata.counts["recommended_queries"] = len(parsed["recommended_queries"])
        self.metadata.counts["stage1_retrieval_results"] = retrieval_meta.get("results", 0)
        self.metadata.counts["stage1_pages_fetched"] = retrieval_meta.get("pages_fetched", 0)
        self.storage.write_metadata(self.metadata)

        return StageResult(1, response, files, parsed)

    # -- stage 2 ----------------------------------------------------------
    def run_stage2(self, company: str, stage1_context: str) -> StageResult:
        self.metadata.stage2_status = "running"
        self.storage.write_metadata(self.metadata)

        base_prompt = self.build_stage2_prompt(company, stage1_context)
        raw_queries = extract_recommended_queries(stage1_context)
        searcher_name = self._sweep_provider().name
        stage2_queries, dropped_queries = clean_queries(
            raw_queries,
            company=company,
            # Baidu does not usefully index mp.weixin.qq.com.
            drop_site_operator="mp.weixin.qq.com" if searcher_name == "serpapi" else None,
        )
        if dropped_queries:
            self.log.info(
                "dropped %d of %d recommended queries before retrieval",
                len(dropped_queries), len(raw_queries),
            )
        stage2_queries = stage2_queries[:8] or self._seed_queries(company)
        block, retrieval_meta = self.build_retrieval_block(company, stage2_queries)
        prompt = self._with_retrieval(base_prompt, block)
        if block:
            self._progress(
                f"      injected {retrieval_meta['results']} results from "
                f"{retrieval_meta['provider']} "
                f"({retrieval_meta.get('pages_fetched', 0)} pages read in full)"
            )
        self.log.info(
            "stage 2 prompt: %d chars (of which %d chars are stage 1 context)",
            len(prompt),
            len(stage1_context),
        )

        try:
            response = self.provider.run_research(prompt, label="stage2")
        except ProviderError:
            # Stage 1 artefacts on disk are deliberately left untouched.
            self.metadata.stage2_status = "failed"
            raise

        for warning in response.warnings:
            self.log.warning("stage 2: %s", warning)

        blocks = parse_source_blocks(response.text, company)
        label_audit: dict[str, Any] = {"checked": 0, "downgraded": 0}
        if self.config.research.get("verify_labels", True):
            label_audit = verify_labels(
                blocks,
                retrieval_meta,
                snippet_cap=int(
                    (self.config.research.get("retrieval_injection") or {})
                    .get("chars_per_result", 220)
                ),
            )
            if label_audit.get("downgraded") or label_audit.get("invalid_labels"):
                self._progress(
                    f"      label audit: {label_audit['downgraded']} downgraded, "
                    f"{label_audit['invalid_labels']} invalid label(s) normalised"
                )
        citations = [
            _record_from_citation(c, company, index=len(blocks) + i)
            for i, c in enumerate(response.search_results, start=1)
        ]

        parsed = {
            "stage": 2,
            "target_company": company,
            "text": response.text,
            "sources": [b.to_dict() for b in blocks],
            "citations": [c.to_dict() for c in citations],
            "new_search_anchors": parse_new_search_anchors(response.text),
            "collection_summary": parse_collection_summary(response.text),
            "provider": response.provider,
            "model": response.model,
            "request_id": response.request_id,
            "finish_reason": response.finish_reason,
            "usage": response.usage,
            "warnings": response.warnings,
            "generated_at": utc_now_iso(),
            "prompt_chars": len(prompt),
            "stage1_context_chars": len(stage1_context),
            "retrieval": retrieval_meta,
            "queries_dropped": dropped_queries,
            "label_audit": label_audit,
            "parser_note": (
                "'sources' is a structural index of the SOURCE_ID blocks in 'text'. "
                "The full untouched model output is always in 'text' and in 02_sources.md."
            ),
        }

        files = self._persist_stage(
            2,
            title=f"Stage 2 — Source Collection & Evidence Preservation: {company}",
            response=response,
            parsed=parsed,
            md_name=STAGE2_MD,
            json_name=STAGE2_JSON,
            raw_name=RAW_STAGE2,
            extra_header=[
                f"- source_blocks_parsed: {len(blocks)}",
                f"- stage1_context_chars: {len(stage1_context)}",
            ],
        )

        written = self._write_raw_sources(blocks, citations)
        files["raw_sources"] = str(written)

        self.metadata.stage2_status = "completed"
        self.metadata.stage2_request_id = response.request_id
        self.metadata.stage2_usage = response.usage
        self.metadata.counts["stage2_source_blocks"] = len(blocks)
        self.metadata.counts["stage2_retrieval_results"] = retrieval_meta.get("results", 0)
        self.metadata.counts["stage2_pages_fetched"] = retrieval_meta.get("pages_fetched", 0)
        self.metadata.counts["labels_downgraded"] = label_audit.get("downgraded", 0)
        self.metadata.counts["stage2_citations"] = len(citations)
        self.metadata.counts["raw_source_files"] = len(self._saved)
        self.storage.write_metadata(self.metadata)

        return StageResult(2, response, files, parsed)

    # -- shared persistence ----------------------------------------------
    def _persist_stage(
        self,
        stage: int,
        *,
        title: str,
        response: ResearchResponse,
        parsed: dict[str, Any],
        md_name: str,
        json_name: str,
        raw_name: str,
        extra_header: list[str],
    ) -> dict[str, str]:
        out = self.config.output
        files: dict[str, str] = {}

        if out.get("save_markdown", True):
            header = [
                f"- target_company: {parsed['target_company']}",
                f"- stage: {stage}",
                f"- provider: {response.provider}",
                f"- model: {response.model}",
                f"- request_id: {response.request_id}",
                f"- generated_at: {parsed['generated_at']}",
                f"- citations_returned: {len(response.search_results)}",
                *extra_header,
            ]
            if response.warnings:
                header.append("- warnings:")
                header.extend(f"  - {w}" for w in response.warnings)
            files["markdown"] = str(
                self.storage.save(md_name, md_document(title, header, response.text))
            )

        if out.get("save_json", True):
            files["json"] = str(self.storage.save_json(json_name, parsed))

        if out.get("save_raw_response", True):
            files["raw"] = str(self.storage.save_json(raw_name, response.raw))

        return files

    def _write_raw_sources(
        self, blocks: list[SourceRecord], citations: list[SourceRecord]
    ) -> int:
        """Stage 2's sources go through the same path as every other channel.

        This used to number from 1 unconditionally, so a later channel wrote
        over stage 2's files — one run lost all 24 of them. Routing everything
        through _persist_records also gets dedup and the index for free.
        """
        before = len(self._saved)
        self._persist_records(blocks + citations)
        return len(self._saved) - before

    # -- WSA search sweep (second evidence channel) -----------------------
    def run_search_sweep(self, company: str, queries: list[str]) -> dict[str, Any]:
        cfg = self.config.search_sweep
        if not cfg.get("enabled", True):
            self.metadata.search_sweep_status = "disabled"
            return {"skipped": "disabled in config"}
        searcher = self._sweep_provider()
        if not searcher.supports_search:
            self.metadata.search_sweep_status = "unsupported"
            return {"skipped": f"provider {searcher.name} has no structured search"}
        if not queries:
            self.metadata.search_sweep_status = "skipped"
            return {"skipped": "no recommended queries found in stage 1 output"}

        cleaned, dropped = clean_queries(
            queries,
            company=company,
            drop_site_operator="mp.weixin.qq.com" if searcher.name == "serpapi" else None,
        )
        if dropped:
            self.log.info("sweep dropped %d unusable queries", len(dropped))
        queries = cleaned or queries
        max_q = int(cfg.get("max_queries", 12))
        selected = queries[:max_q]
        dropped = len(queries) - len(selected)
        if dropped > 0:
            # Never silently truncate coverage.
            self._progress(
                f"      note: using {len(selected)} of {len(queries)} recommended queries "
                f"(search_sweep.max_queries={max_q}); {dropped} not searched"
            )
            self.metadata.notes.append(
                f"search sweep covered {len(selected)}/{len(queries)} recommended queries; "
                f"{dropped} skipped by search_sweep.max_queries={max_q}"
            )

        sites = cfg.get("site_filters") or [None]
        industries = cfg.get("industries") or [None]
        self.metadata.search_sweep_status = "running"
        self.storage.write_metadata(self.metadata)

        raw_responses: list[dict[str, Any]] = []
        records: list[SourceRecord] = []
        failures: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        # Search engines suggest related queries; those are free new anchors.
        discovered_anchors: list[str] = []

        total = len(selected) * len(sites) * len(industries)
        done = 0
        for query in selected:
            for site in sites:
                for industry in industries:
                    done += 1
                    self._progress(
                        f"      search {done}/{total}: {query}"
                        + (f" [site:{site}]" if site else "")
                        + (f" [industry:{industry}]" if industry else "")
                    )
                    try:
                        result = searcher.search(
                            query,
                            count=int(cfg.get("results_per_query", 20)),
                            site=site,
                            industry=industry,
                            freshness=cfg.get("freshness"),
                            mode=int(cfg.get("mode", 2)),
                        )
                    except ProviderError as exc:
                        # One bad query must not lose the whole sweep.
                        self.log.warning("search failed for %r: %s", query, exc)
                        failures.append({"query": query, "site": site or "", "error": str(exc)})
                        continue

                    for anchor in (result.get("related_searches") or []) + (
                        result.get("people_also_search_for") or []
                    ):
                        if anchor and anchor not in discovered_anchors:
                            discovered_anchors.append(anchor)
                    if result.get("raw"):
                        raw_responses.append(
                            {"query": query, "site": site, "industry": industry,
                             "response": result["raw"]}
                        )
                    for page in result.get("pages", []):
                        url = (page.get("url") or "").strip()
                        key = url or f"{query}|{page.get('title')}"
                        if key in seen_urls:
                            continue
                        seen_urls.add(key)
                        records.append(
                            _record_from_page(page, company, query=query, site=site)
                        )

        for offset, record in enumerate(records, start=1):
            record.source_id = f"SEARCH_{offset:03d}"

        start_index = int(self.metadata.counts.get("raw_source_files", 0)) + 1
        if self.config.output.get("save_raw_sources", True):
            for offset, record in enumerate(records):
                self.storage.save_source(record, index=start_index + offset)

        payload = {
            "target_company": company,
            "queries_available": len(queries),
            "queries_searched": len(selected),
            "queries_not_searched": queries[len(selected):],
            "site_filters": sites,
            "industries": industries,
            "results": [r.to_dict() for r in records],
            "failures": failures,
            "queries_dropped": dropped,
            "engine_suggested_anchors": discovered_anchors,
            "generated_at": utc_now_iso(),
            "provider": searcher.name,
            "endpoint": searcher.describe().get("endpoints", {}).get("search"),
            "content_note": (
                "Structured search returns titles plus a search summary, not article "
                "full text. Every record is therefore labelled SEARCH_SNIPPET_ONLY or "
                "URL_ONLY — never VERBATIM_FULL_TEXT."
            ),
        }

        if self.config.output.get("save_json", True):
            self.storage.save_json(SWEEP_JSON, payload)
        if self.config.output.get("save_raw_response", True) and raw_responses:
            self.storage.save_json(RAW_SWEEP, raw_responses)
        if self.config.output.get("save_markdown", True):
            self.storage.save(SWEEP_MD, _sweep_markdown(company, payload))

        self.metadata.search_sweep_status = "completed" if not failures else "completed_with_errors"
        self.metadata.counts["search_sweep_results"] = len(records)
        self.metadata.counts["engine_suggested_anchors"] = len(discovered_anchors)
        self.metadata.counts["search_sweep_failures"] = len(failures)
        self.metadata.counts["raw_source_files"] = (
            int(self.metadata.counts.get("raw_source_files", 0)) + len(records)
        )
        if failures:
            self.metadata.search_sweep_error = f"{len(failures)} query/queries failed"
        self.storage.write_metadata(self.metadata)
        return payload


# ---------------------------------------------------------------------------
# Parsers. All of these are additive: the untouched model text is always saved
# alongside, so a parse miss can never lose evidence.
# ---------------------------------------------------------------------------


