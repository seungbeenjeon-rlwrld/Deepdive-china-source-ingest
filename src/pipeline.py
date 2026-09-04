"""The two-stage research pipeline.

State is held here, never on the server. Stage 2 receives the **complete,
unmodified** stage 1 text injected into prompt 2 inside a delimited block:

    <STAGE_1_RESEARCH>
    ... full stage 1 output ...
    </STAGE_1_RESEARCH>

Stage 1 output is never summarised, trimmed or re-ordered before injection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

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
    CONTENT_ACCESS_STATUSES,
    ResearchResponse,
    RunMetadata,
    SourceRecord,
    classify_url,
    guess_platform,
)
from .provider import ProviderError, ResearchProvider
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
        self._sweep_cache: Optional[ResearchProvider] = None

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
        name = (self.config.search_sweep.get("provider") or "").strip().lower()
        if not name or name == self.provider.name:
            return self.provider
        if self._sweep_cache is None:
            from .provider import ProviderError, build_provider

            try:
                self._sweep_cache = build_provider(name, self.config)
                self.log.info("sweep provider: %s", self._sweep_cache.name)
            except ProviderError as exc:
                # A missing sweep credential must not sink a run whose stages 1
                # and 2 already succeeded. Fall back and say so.
                self.log.warning(
                    "sweep provider %r unavailable (%s) — falling back to %s",
                    name, exc, self.provider.name,
                )
                self.metadata.notes.append(
                    f"search sweep fell back from {name!r} to {self.provider.name!r}: {exc}"
                )
                self._sweep_cache = self.provider
        return self._sweep_cache

    def _next_source_index(self) -> int:
        return int(self.metadata.counts.get("raw_source_files", 0)) + 1

    def _persist_records(self, records: list[SourceRecord]) -> None:
        if not self.config.output.get("save_raw_sources", True):
            return
        start = self._next_source_index()
        for offset, record in enumerate(records):
            self.storage.save_source(record, index=start + offset)
        self.metadata.counts["raw_source_files"] = start + len(records) - 1

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
        official_hosts = []
        if official_index:
            from urllib.parse import urlparse as _urlparse
            official_hosts.append(_urlparse(official_index).netloc)
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

        searcher = self._sweep_provider()
        if not searcher.supports_search:
            return None, {"skipped": f"provider {searcher.name} has no structured search"}

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

    def _seed_queries(self, company: str) -> list[str]:
        cfg = self.config.research.get("retrieval_injection") or {}
        templates = cfg.get("seed_queries") or ["{company}"]
        return [t.replace("{company}", company) for t in templates]

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

        base_prompt = self.build_stage1_prompt(company)
        block, retrieval_meta = self.build_retrieval_block(
            company, self._seed_queries(company)
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
        }

        files = self._persist_stage(
            1,
            title=f"Stage 1 — Entity & Source Discovery: {company}",
            response=response,
            parsed=parsed,
            md_name=STAGE1_MD,
            json_name=STAGE1_JSON,
            raw_name=RAW_STAGE1,
            extra_header=[f"- recommended_queries_found: {len(parsed['recommended_queries'])}"],
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
        files["raw_sources"] = str(len(written))

        self.metadata.stage2_status = "completed"
        self.metadata.stage2_request_id = response.request_id
        self.metadata.stage2_usage = response.usage
        self.metadata.counts["stage2_source_blocks"] = len(blocks)
        self.metadata.counts["stage2_retrieval_results"] = retrieval_meta.get("results", 0)
        self.metadata.counts["stage2_pages_fetched"] = retrieval_meta.get("pages_fetched", 0)
        self.metadata.counts["labels_downgraded"] = label_audit.get("downgraded", 0)
        self.metadata.counts["stage2_citations"] = len(citations)
        self.metadata.counts["raw_source_files"] = len(written)
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
    ) -> list[dict[str, str]]:
        if not self.config.output.get("save_raw_sources", True):
            return []
        written = []
        for index, record in enumerate(blocks + citations, start=1):
            written.append(self.storage.save_source(record, index=index))
        return written

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
    # If pages were actually read, the model legitimately had source text, so
    # judge claims against that budget rather than the snippet one.
    if retrieval.get("pages_fetched"):
        limit = max(int(snippet_cap * 1.5), 400)
    else:
        limit = max(int(snippet_cap * 1.5), 400)
    checked = downgraded = invalid = 0
    details: list[dict[str, Any]] = []
    invalid_details: list[dict[str, Any]] = []

    for record in records:
        claim = record.content_access_status

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
