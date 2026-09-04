#!/usr/bin/env python3
"""China Local Research Collector — CLI entry point.

    python research.py
    python research.py --company "AgiBot"
    python research.py --company "AgiBot" --stage 1
    python research.py --resume ./research/agibot/2026-09-02_174500 --stage 2

This layer only handles argument parsing, terminal output and error reporting.
All research logic lives in ``src/pipeline.py``; all Tencent-specific code lives
in ``src/tencent_client.py``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import __version__
from src.config import load_config
from src.models import RunMetadata
from src.pipeline import Pipeline, extract_recommended_queries
from src.storage import LocalStorageBackend, STAGE1_JSON, STAGE1_MD, STAGE2_JSON
from src.provider import ProviderError, build_provider
from src.utils import (
    add_file_handler,
    get_logger,
    load_dotenv,
    run_timestamp,
    say,
    setup_logging,
    slugify,
    utc_now_iso,
)

class _SkipStage2(Exception):
    """Control flow only: --stage channels leaves stage 2 alone."""


BANNER = """========================================
 Deepdive — China Source Ingest
========================================"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research.py",
        description="Collect Chinese local research sources for a target company "
                    "via Tencent Hunyuan + 联网搜索API (Yuanbao search stack).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python research.py\n"
            '  python research.py --company "AgiBot"\n'
            '  python research.py --company "AgiBot" --stage 1\n'
            "  python research.py --resume ./research/agibot/2026-09-02_174500 --stage 2\n"
            "  python research.py --company \"AgiBot\" --provider mock   # offline test\n"
        ),
    )
    parser.add_argument("--company", help="target company name (prompted for if omitted)")
    parser.add_argument(
        "--stage",
        choices=["1", "2", "all", "channels"],
        default="all",
        help="which stage to run (default: all). 'channels' runs only the "
             "collection channels (newsroom, filings, patents, sweep) against a "
             "saved run, leaving stages 1-2 untouched.",
    )
    parser.add_argument(
        "--resume",
        metavar="RUN_DIR",
        help="reuse the stage 1 result saved in this run directory instead of re-running it",
    )
    parser.add_argument(
        "--provider",
        choices=["claude-cli", "zhipu", "tencent", "serpapi", "mock"],
        help="override config provider",
    )
    parser.add_argument("--config", help="path to a config file (default: ./config.yaml)")
    parser.add_argument(
        "--official-site",
        metavar="INDEX_URL",
        help="crawl this newsroom index for full-text official articles "
             "(e.g. https://www.agibot.com.cn/article/315)",
    )
    parser.add_argument(
        "--filings",
        metavar="LISTED_NAME",
        help="index exchange filings for this listed entity from 巨潮资讯网 "
             "(e.g. 上纬新材) — prompt 2 priority-1 sources",
    )
    parser.add_argument(
        "--patents",
        metavar="ASSIGNEE",
        help="index CNIPA patents for this assignee "
             "(e.g. 上海智元新创技术有限公司)",
    )
    parser.add_argument(
        "--no-reposts",
        action="store_true",
        help="skip looking for readable reposts of sources whose original is gated",
    )
    parser.add_argument(
        "--no-search-sweep",
        action="store_true",
        help="skip the structured search sweep over stage 1's recommended queries",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow overwriting a stage 2 result that already completed",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="print detailed logs to stderr")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _ask(label: str, hint: str = "") -> Optional[str]:
    """Read one optional value. Empty input means skip."""
    say()
    say(label)
    if hint:
        say(f"  {hint}")
    try:
        value = input("> ").strip()
    except EOFError:
        return None
    return value or None


def prompt_company() -> str:
    say(BANNER)
    say()
    say("조사할 회사명을 입력하세요.")
    say("  중국어 회사명이 가장 정확합니다. 예: 智元机器人")
    try:
        value = input("> ").strip()
    except EOFError:
        value = ""
    if not value:
        raise SystemExit("회사명이 없어 종료합니다.")
    return value


def prompt_channels() -> dict[str, Optional[str]]:
    """Ask for the three per-company values that unlock the extra channels.

    Without these, an interactive run silently skips the newsroom crawl, the
    exchange-filing index and the patent index — which is most of the corpus.
    Asking once beats leaving them off by default.
    """
    say()
    say("─" * 40)
    say("추가 수집 채널 (선택) — 모르면 Enter 로 건너뛰세요.")
    say("Stage 1 결과의 법인명에서 나중에 찾아 재실행할 수 있습니다.")

    return {
        "official_site": _ask(
            "1) 공식 뉴스룸 목록 URL",
            "회사 홈페이지의 뉴스/新闻资讯 목록 페이지. 예: https://www.agibot.com.cn/article/315",
        ),
        "filings": _ask(
            "2) 거래소 공시 조회용 상장사명",
            "이 회사가 지배하는 상장사가 있으면. 예: 上纬新材",
        ),
        "patents": _ask(
            "3) 특허 출원인 법인명",
            "중국어 정식 법인명. 예: 上海智元新创技术有限公司",
        ),
    }


def find_latest_run(root: Path, company: str) -> Optional[Path]:
    """Newest run directory for a company that has a usable stage 1 result."""
    base = root / slugify(company)
    if not base.is_dir():
        return None
    candidates = sorted((d for d in base.iterdir() if d.is_dir()), reverse=True)
    for directory in candidates:
        if (directory / STAGE1_JSON).is_file() or (directory / STAGE1_MD).is_file():
            return directory
    return None


def report_error(exc: Exception) -> None:
    say()
    say(f"ERROR: {exc}")
    hint = getattr(exc, "hint", "")
    if hint:
        say(f"  -> {hint}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    load_dotenv(PROJECT_ROOT / ".env")
    setup_logging(verbose=args.verbose)
    log = get_logger()

    try:
        config = load_config(args.config, project_root=PROJECT_ROOT)
    except (FileNotFoundError, ValueError) as exc:
        report_error(exc)
        return 2

    provider_name = args.provider or config.provider
    if args.no_search_sweep:
        config.search_sweep = {**config.search_sweep, "enabled": False}
    if args.no_reposts:
        config.repost_resolution = {**config.repost_resolution, "enabled": False}
    company = args.company.strip() if args.company else None
    storage = LocalStorageBackend(config.research_root)
    resume_dir: Optional[Path] = None

    # ---- resolve the run directory and the company name ----------------
    if args.resume:
        resume_dir = Path(args.resume).expanduser()
        if not resume_dir.is_absolute():
            resume_dir = (Path.cwd() / resume_dir).resolve()
        try:
            storage.attach_run(resume_dir)
        except FileNotFoundError as exc:
            report_error(exc)
            return 2
        existing = storage.load_metadata() or {}
        company = company or existing.get("target_company")
        if not company:
            report_error(
                ValueError(
                    f"{resume_dir} has no readable metadata.json, so the company name is unknown."
                )
            )
            say("  -> Re-run with --company \"<name>\" alongside --resume.")
            return 2

    interactive = False
    if not company:
        company = prompt_company()
        interactive = True
    else:
        say(BANNER)
        say()
        say(f"조사 대상: {company}")

    # Fully interactive run: ask for the values the extra channels need, unless
    # they were already supplied on the command line or in config.
    if interactive and not args.resume:
        answers = prompt_channels()
        args.official_site = args.official_site or answers["official_site"]
        args.filings = args.filings or answers["filings"]
        args.patents = args.patents or answers["patents"]

    stage1_only = args.stage == "1"
    stage2_only = args.stage == "2"
    channels_only = args.stage == "channels"

    if channels_only and resume_dir is None:
        latest = find_latest_run(config.research_root, company)
        if latest is None:
            report_error(
                FileNotFoundError(
                    f"--stage channels needs an existing run; none found under "
                    f"{config.research_root / slugify(company)}"
                )
            )
            return 2
        say()
        say(f"Using the most recent run: {latest}")
        storage.attach_run(latest)
        resume_dir = latest

    # Stage 2 alone with no --resume: fall back to the newest saved stage 1.
    if stage2_only and resume_dir is None:
        latest = find_latest_run(config.research_root, company)
        if latest is None:
            report_error(
                FileNotFoundError(
                    f"no previous run with a stage 1 result found under "
                    f"{config.research_root / slugify(company)}"
                )
            )
            say("  -> Run stage 1 first: python research.py --company "
                f"\"{company}\" --stage 1")
            return 2
        say()
        say(f"Reusing the most recent stage 1 result: {latest}")
        storage.attach_run(latest)
        resume_dir = latest

    official_index = args.official_site or config.official_site.get("index_url")
    filings_key = args.filings or config.registries.get("filings_search_key")
    patent_assignee = args.patents or config.registries.get("patent_assignee")
    if official_index:
        config.official_site = {
            **config.official_site, "enabled": True, "index_url": official_index
        }

    if interactive:
        planned = [
            name for name, on in (
                ("공식 뉴스룸", bool(official_index)),
                ("거래소 공시", bool(filings_key)),
                ("특허", bool(patent_assignee)),
            ) if on
        ]
        say()
        say("─" * 40)
        if planned:
            say(f"추가 수집: {', '.join(planned)}")
        else:
            say("추가 수집 없음 — Stage 1·2 와 검색 스윕만 실행합니다.")
            say("나중에 채널을 추가하려면:")
            say(f'  python research.py --company "{company}" \\')
            say('    --official-site "<뉴스룸 URL>" --filings "<상장사명>"')

    # ---- provider ------------------------------------------------------
    try:
        provider = build_provider(provider_name, config)
    except ProviderError as exc:
        report_error(exc)
        return 2

    # ---- metadata + run dir --------------------------------------------
    if resume_dir is None:
        run_dir = storage.create_run(company, timestamp=run_timestamp())
        metadata = RunMetadata(
            target_company=company,
            company_slug=slugify(company),
            run_dir=str(run_dir),
            started_at=utc_now_iso(),
            provider=provider.name,
            model=provider.describe().get("model"),
            tool_version=__version__,
        )
        prior_stage2_status = None
    else:
        run_dir = resume_dir
        existing = storage.load_metadata() or {}
        metadata = RunMetadata(
            target_company=company,
            company_slug=existing.get("company_slug") or slugify(company),
            run_dir=str(run_dir),
            started_at=existing.get("started_at") or utc_now_iso(),
            provider=provider.name,
            model=existing.get("model"),
            tool_version=__version__,
        )
        # Preserve what stage 1 already achieved — a stage 2 re-run must not
        # erase a successful stage 1.
        prior_stage2_status = existing.get("stage2_status")
        metadata.stage1_status = existing.get("stage1_status", "unknown")
        metadata.stage1_request_id = existing.get("stage1_request_id")
        metadata.stage1_usage = existing.get("stage1_usage")
        metadata.counts = dict(existing.get("counts") or {})
        metadata.notes = list(existing.get("notes") or [])
        metadata.notes.append(f"stage 2 resumed at {utc_now_iso()} from {run_dir}")

    add_file_handler(run_dir / "logs" / "run.log")
    log.info("provider=%s config=%s", provider.name, config.source_file)
    log.debug("provider details: %s", provider.describe())
    if provider.name == "mock":
        say()
        say("NOTE: running with the mock provider — output is synthetic, not real research.")

    storage.write_metadata(metadata)
    pipeline = Pipeline(config, provider, storage, metadata, progress=say)

    stage1_text: Optional[str] = None
    stage2_sources: list = []
    exit_code = 0

    # ---- stage 1 --------------------------------------------------------
    try:
        if channels_only:
            stage1_text, _payload = storage.load_stage1()
            # prior_stage2_status was captured before metadata was rewritten.
            metadata.stage2_status = prior_stage2_status or "unknown"
            say()
            say(f"Collection channels only — stages 1 and 2 left untouched "
                f"(stage 2: {metadata.stage2_status}).")
        elif stage2_only:
            say()
            say("[1/2] Loading saved entity discovery...")
            stage1_text, _payload = storage.load_stage1()
            metadata.stage1_status = "loaded_from_disk"
            metadata.notes.append(f"stage 1 loaded from disk ({len(stage1_text)} chars)")
            storage.write_metadata(metadata)
            say(f"✓ Loaded stage 1 result ({len(stage1_text):,} chars)")
        else:
            say()
            say("[1/2] Discovering company entities...")
            result = pipeline.run_stage1(company)
            stage1_text = result.response.text
            say("✓ Entity discovery complete")
            say("✓ Results saved")
    except (ProviderError, ValueError, OSError) as exc:
        # Covers provider errors and anything raised before the call (e.g. a
        # missing prompt file), so the status can never be left at "running".
        if metadata.stage1_status not in ("completed", "loaded_from_disk"):
            metadata.stage1_status = "failed"
        metadata.stage1_error = str(exc)
        metadata.completed_at = utc_now_iso()
        storage.write_metadata(metadata)
        report_error(exc)
        say()
        say(f"Partial results (if any) are in:\n{run_dir}/")
        return 1
    except KeyboardInterrupt:
        metadata.stage1_status = "failed"
        metadata.stage1_error = "interrupted by user"
        metadata.completed_at = utc_now_iso()
        storage.write_metadata(metadata)
        say("\nInterrupted.")
        return 130

    if stage1_only:
        metadata.stage2_status = "skipped"
        metadata.search_sweep_status = "skipped"
        metadata.completed_at = utc_now_iso()
        storage.write_metadata(metadata)
        say()
        say("Stage 1 only (--stage 1). Run stage 2 later with:")
        say(f'  python research.py --resume "{run_dir}" --stage 2')
        say()
        say(f"Research saved to:\n{run_dir}/")
        say()
        say("Done.")
        return 0

    if channels_only:
        stage2_sources = (storage.read_json(STAGE2_JSON) or {}).get("sources") or [] \
            if storage.exists(STAGE2_JSON) else []

    # ---- stage 2 --------------------------------------------------------
    # A completed stage 2 is expensive and easy to destroy by re-running with a
    # different config. Refuse unless the caller says so. (Learned the hard way:
    # a 26,252-char result with 24 sources was overwritten by a 2,519-char one.)
    # `prior_stage2_status` is read before metadata is rewritten — reading the
    # file here would always show the freshly written "pending".
    if (
        not channels_only
        and not args.force
        and prior_stage2_status == "completed"
        and storage.exists(STAGE2_JSON)
    ):
        if True:
            report_error(
                RuntimeError(
                    f"stage 2 already completed in {run_dir} and would be overwritten."
                )
            )
            say("  -> To add collection channels without touching it:")
            say(f'     python research.py --resume "{run_dir}" --stage channels')
            say("  -> To deliberately redo stage 2:  add --force")
            return 2

    # From here on, failures must never touch the stage 1 files on disk.
    try:
        if channels_only:
            raise _SkipStage2()
        say()
        say("[2/2] Collecting Chinese local sources...")
        stage2 = pipeline.run_stage2(company, stage1_text or "")
        stage2_sources = stage2.parsed.get("sources") or []
        say("✓ Source collection complete")
        say("✓ Results saved")
    except (ProviderError, ValueError, OSError) as exc:
        if metadata.stage2_status != "completed":
            metadata.stage2_status = "failed"
        metadata.stage2_error = str(exc)
        metadata.completed_at = utc_now_iso()
        storage.write_metadata(metadata)
        report_error(exc)
        say()
        say("Stage 1 results were NOT deleted and remain valid:")
        say(f"  {run_dir}/{STAGE1_MD}")
        say("Retry stage 2 only, without re-running stage 1:")
        say(f'  python research.py --resume "{run_dir}" --stage 2')
        exit_code = 1
    except _SkipStage2:
        pass
    except KeyboardInterrupt:
        metadata.stage2_status = "failed"
        metadata.stage2_error = "interrupted by user"
        metadata.completed_at = utc_now_iso()
        storage.write_metadata(metadata)
        say("\nInterrupted. Stage 1 results are intact.")
        return 130

    # ---- official newsroom crawl ---------------------------------------
    # Independent of the search API: the company's own site serves full text.
    if exit_code == 0 and official_index:
        try:
            say()
            pipeline.run_official_site(company, official_index)
        except Exception as exc:
            metadata.official_site_status = "failed"
            storage.write_metadata(metadata)
            log.warning("official site crawl failed: %s", exc)
            say(f"  official newsroom crawl failed ({exc}) — earlier results unaffected")

    # ---- primary-source registries -------------------------------------
    # Independent of every other stage: open registry endpoints, no account.
    for label, runner, arg in (
        ("exchange filings", pipeline.run_exchange_filings, filings_key),
        ("patents", pipeline.run_patents, patent_assignee),
    ):
        if exit_code != 0 or not arg:
            continue
        try:
            say()
            runner(company, arg)
        except Exception as exc:
            log.warning("%s stage failed: %s", label, exc)
            say(f"  {label} failed ({exc}) — earlier results unaffected")

    # ---- repost resolution ---------------------------------------------
    if exit_code == 0 and config.repost_resolution.get("enabled", True):
        try:
            result = pipeline.run_repost_resolution(company, stage2_sources)
            if "skipped" in result:
                log.info("repost resolution skipped: %s", result["skipped"])
        except Exception as exc:
            metadata.repost_status = "failed"
            storage.write_metadata(metadata)
            log.warning("repost resolution failed: %s", exc)
            say(f"  repost resolution failed ({exc}) — earlier results unaffected")

    # ---- search sweep ---------------------------------------------------
    if exit_code == 0 and config.search_sweep.get("enabled", True):
        try:
            queries = extract_recommended_queries(stage1_text or "")
            if queries:
                say()
                planned = min(len(queries), int(config.search_sweep.get("max_queries", 12)))
                say(f"[+] Sweeping structured search over {planned} recommended queries...")
                sweep = pipeline.run_search_sweep(company, queries)
                if "skipped" in sweep:
                    say(f"  skipped: {sweep['skipped']}")
                else:
                    say(f"✓ Collected {len(sweep['results'])} structured search results")
                    say("✓ Results saved")
        except ProviderError as exc:
            # A sweep failure is not fatal: stages 1 and 2 are already saved.
            metadata.search_sweep_status = "failed"
            metadata.search_sweep_error = str(exc)
            storage.write_metadata(metadata)
            log.warning("search sweep failed: %s", exc)
            say(f"  search sweep failed ({exc}) — stage 1 and 2 results are unaffected")
        except KeyboardInterrupt:
            metadata.search_sweep_status = "failed"
            metadata.search_sweep_error = "interrupted by user"
            storage.write_metadata(metadata)
            say("\nSearch sweep interrupted. Stage 1 and 2 results are intact.")

    metadata.completed_at = utc_now_iso()
    storage.write_metadata(metadata)

    say()
    say(f"Research saved to:\n{run_dir}/")
    say()
    say("Done." if exit_code == 0 else "Finished with errors.")
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:  # pragma: no cover
        say("\nInterrupted.")
        raise SystemExit(130)
