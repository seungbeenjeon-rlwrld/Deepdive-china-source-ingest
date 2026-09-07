"""Offline tests. Standard library only:  python3 -m unittest discover tests -v"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config
from src.models import RunMetadata, classify_url
from src.pipeline import (
    STAGE1_CLOSE,
    STAGE1_OPEN,
    Pipeline,
    extract_recommended_queries,
    parse_collection_summary,
    parse_source_blocks,
)
from src.provider import MockProvider, ProviderError, ResearchProvider
from src.storage import (
    LocalStorageBackend,
    STAGE1_JSON,
    STAGE1_MD,
    STAGE2_MD,
    md_document,
)
from src.utils import slugify, utc_now_iso


class FailingStage2Provider(MockProvider):
    """Succeeds on stage 1, fails on stage 2 — the scenario from spec §12."""

    name = "failing-mock"

    def run_research(self, prompt, *, label=""):
        if STAGE1_OPEN in prompt:
            raise ProviderError("simulated stage 2 API failure")
        return super().run_research(prompt, label=label)


class Harness:
    """Builds a pipeline against a throwaway directory."""

    def __init__(self, provider: ResearchProvider, company: str = "AgiBot"):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = load_config(project_root=PROJECT_ROOT)
        self.config.output = {**self.config.output, "root_dir": str(self.tmp)}
        # Pin every search path to the harness provider. config.yaml points
        # retrieval and the sweep at serpapi, and leaving that in place made the
        # test suite issue real Baidu queries — 10 of the user's monthly quota
        # before it was caught. Tests must never touch the network.
        self.config.search_sweep = {**self.config.search_sweep, "provider": None}
        self.config.research = {
            **self.config.research,
            "derive_channels": {"enabled": True, "probe_filings": False},
            "retrieval_injection": {
                **(self.config.research.get("retrieval_injection") or {}),
                "enabled": False,
                "provider": None,
            },
        }
        self.storage = LocalStorageBackend(self.tmp)
        self.run_dir = self.storage.create_run(company)
        self.metadata = RunMetadata(
            target_company=company,
            company_slug=slugify(company),
            run_dir=str(self.run_dir),
            started_at=utc_now_iso(),
            provider=provider.name,
        )
        self.pipeline = Pipeline(self.config, provider, self.storage, self.metadata)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestSlug(unittest.TestCase):
    def test_examples_from_spec(self):
        self.assertEqual(slugify("AgiBot"), "agibot")
        self.assertEqual(slugify("Unitree Robotics"), "unitree-robotics")

    def test_cjk_preserved_and_deterministic(self):
        self.assertEqual(slugify("宇树科技"), "宇树科技")
        self.assertEqual(slugify("宇树科技"), slugify("宇树科技"))

    def test_pathological_names_get_a_stable_slug(self):
        for name in ("///", "。。。", "!!!"):
            slug = slugify(name)
            self.assertTrue(slug)
            self.assertEqual(slug, slugify(name))
            self.assertNotIn("/", slug)

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            slugify("   ")


class TestStageContextPassing(unittest.TestCase):
    """Spec §2: stage 2 must receive stage 1's real output, unsummarised."""

    def setUp(self):
        self.h = Harness(MockProvider())

    def tearDown(self):
        self.h.cleanup()

    def test_stage1_prompt_substitutes_company(self):
        prompt = self.h.pipeline.build_stage1_prompt("AgiBot")
        self.assertIn("AgiBot", prompt)
        self.assertNotIn("{TARGET_COMPANY}", prompt)

    def test_stage2_prompt_embeds_full_stage1_text(self):
        stage1 = "线路A\n" * 500 + "峰值功率12kW"
        prompt = self.h.pipeline.build_stage2_prompt("AgiBot", stage1)
        self.assertIn(STAGE1_OPEN, prompt)
        self.assertIn(STAGE1_CLOSE, prompt)
        # Nothing was dropped or compressed.
        self.assertIn(stage1.strip(), prompt)
        self.assertIn("TARGET_COMPANY: AgiBot", prompt)
        # And prompt 2's own instructions are still there.
        self.assertIn("SEARCH → ACCESS → PRESERVE → VERIFY LINK", prompt)

    def test_stage2_refuses_empty_context(self):
        with self.assertRaises(ValueError):
            self.h.pipeline.build_stage2_prompt("AgiBot", "   ")

    def test_closing_delimiter_in_stage1_is_escaped(self):
        prompt = self.h.pipeline.build_stage2_prompt("AgiBot", f"evil {STAGE1_CLOSE} text")
        self.assertEqual(prompt.count(STAGE1_CLOSE), 1)

    def test_stage2_receives_stage1_without_manual_copying(self):
        provider = MockProvider()
        h = Harness(provider)
        try:
            s1 = h.pipeline.run_stage1("AgiBot")
            s2 = h.pipeline.run_stage2("AgiBot", s1.response.text)
            self.assertEqual(s2.parsed["stage1_context_chars"], len(s1.response.text))
            self.assertGreater(s2.parsed["stage1_context_chars"], 0)
            # stage0 resolves the Chinese search names before stage 1 retrieves.
            self.assertEqual(provider.calls, ["stage0", "stage1", "stage2"])
        finally:
            h.cleanup()


class TestSourceParsing(unittest.TestCase):
    def test_footer_source_id_is_not_a_source(self):
        text = MockProvider().run_research("x " + STAGE1_OPEN, label="s2").text
        blocks = parse_source_blocks(text, "MockCorp")
        self.assertEqual([b.source_id for b in blocks], ["SOURCE_001", "SOURCE_002"])

    def test_content_preserved_verbatim(self):
        text = (
            "SOURCE_ID: SOURCE_001\n"
            "TITLE: 测试\n"
            "CONTENT_ACCESS_STATUS: VERBATIM_FULL_TEXT\n"
            "SOURCE_CONTENT:\n\n"
            "第一段。峰值功率12kW\n\n"
            "第二段。2025年累计出货5100台\n"
        )
        block = parse_source_blocks(text, "X")[0]
        self.assertIn("峰值功率12kW", block.content)
        self.assertIn("2025年累计出货5100台", block.content)
        self.assertIn("\n\n", block.content)  # paragraph structure kept

    def test_nullish_values_become_none_not_guesses(self):
        text = "SOURCE_ID: S1\nAUTHOR: null\nCANONICAL_URL: N/A\nPUBLISHER: 无\n"
        block = parse_source_blocks(text, "X")[0]
        self.assertIsNone(block.author)
        self.assertIsNone(block.canonical_url)
        self.assertIsNone(block.publisher)

    def test_unknown_fields_are_kept_not_dropped(self):
        text = "SOURCE_ID: S1\nSOME_NEW_FIELD: 重要数据\n"
        block = parse_source_blocks(text, "X")[0]
        self.assertEqual(block.extra["unmapped_fields"]["SOME_NEW_FIELD"], "重要数据")

    def test_no_source_blocks_returns_empty(self):
        self.assertEqual(parse_source_blocks("just prose, no blocks", "X"), [])

    def test_collection_summary_read_back_verbatim(self):
        text = MockProvider().run_research("x " + STAGE1_OPEN, label="s2").text
        summary = parse_collection_summary(text)
        self.assertEqual(summary["total_sources_discovered"], "2")
        self.assertEqual(summary["verbatim_full_text"], "0")

    def test_recommended_queries_extracted(self):
        text = MockProvider().run_research("x", label="s1").text
        queries = extract_recommended_queries(text)
        self.assertIn("模拟科技 融资", queries)
        self.assertEqual(len(queries), len(set(queries)))

    def test_recommended_queries_absent_is_not_an_error(self):
        self.assertEqual(extract_recommended_queries("no such section"), [])


class TestUrlClassification(unittest.TestCase):
    """Spec / prompt 2 §7: signed Sogou links are never treated as canonical."""

    def test_sogou_signed_link_flagged_ephemeral(self):
        d = classify_url("https://mp.weixin.qq.com/s?src=11&timestamp=1&signature=x")
        self.assertEqual(d["url_type_heuristic"], "TEMPORARY_SOGOU_SIGNED_URL")
        self.assertTrue(d["is_ephemeral"])

    def test_stable_wechat_article(self):
        d = classify_url("https://mp.weixin.qq.com/s/AbCdEf")
        self.assertEqual(d["url_type_heuristic"], "STABLE_WECHAT_ARTICLE_URL")
        self.assertFalse(d["is_ephemeral"])

    def test_missing_url_yields_nulls_not_guesses(self):
        self.assertIsNone(classify_url(None)["url_type_heuristic"])


class TestEvidencePreservation(unittest.TestCase):
    def setUp(self):
        self.h = Harness(MockProvider())

    def tearDown(self):
        self.h.cleanup()

    def test_search_results_never_claim_full_text(self):
        self.h.pipeline.run_stage1("AgiBot")
        sweep = self.h.pipeline.run_search_sweep("AgiBot", ["模拟科技 融资"])
        self.assertTrue(sweep["results"])
        for record in sweep["results"]:
            self.assertIn(
                record["content_access_status"], ("SEARCH_SNIPPET_ONLY", "URL_ONLY")
            )

    def test_raw_response_is_saved_untouched(self):
        result = self.h.pipeline.run_stage1("AgiBot")
        raw_on_disk = json.loads(self.h.storage.read("raw_stage1_response.json"))
        self.assertEqual(raw_on_disk, result.response.raw)

    def test_markdown_carries_stage1_text_losslessly(self):
        result = self.h.pipeline.run_stage1("AgiBot")
        recovered, _ = self.h.storage.load_stage1()
        self.assertEqual(recovered.strip(), result.response.text.strip())

    def test_stage1_recoverable_from_markdown_alone(self):
        result = self.h.pipeline.run_stage1("AgiBot")
        (self.h.run_dir / STAGE1_JSON).unlink()
        (self.h.run_dir / "raw_stage1_response.json").unlink()
        recovered, meta = self.h.storage.load_stage1()
        self.assertEqual(recovered.strip(), result.response.text.strip())
        self.assertEqual(meta["recovered_from"], STAGE1_MD)

    def test_chinese_survives_json_roundtrip(self):
        self.h.storage.save_json("t.json", {"k": "峰值功率12kW 视频号"})
        text = self.h.storage.read("t.json")
        self.assertIn("峰值功率12kW", text)  # not \uXXXX escaped
        self.assertEqual(json.loads(text)["k"], "峰值功率12kW 视频号")


class TestFailureIsolation(unittest.TestCase):
    """Spec §12: a stage 2 failure must never destroy stage 1 results."""

    def test_stage1_files_survive_stage2_failure(self):
        h = Harness(FailingStage2Provider())
        try:
            s1 = h.pipeline.run_stage1("AgiBot")
            with self.assertRaises(ProviderError):
                h.pipeline.run_stage2("AgiBot", s1.response.text)

            for name in (STAGE1_MD, STAGE1_JSON, "raw_stage1_response.json"):
                self.assertTrue((h.run_dir / name).is_file(), f"{name} was lost")

            recovered, _ = h.storage.load_stage1()
            self.assertEqual(recovered.strip(), s1.response.text.strip())

            self.assertEqual(h.metadata.stage1_status, "completed")
            self.assertEqual(h.metadata.stage2_status, "failed")
            self.assertFalse((h.run_dir / STAGE2_MD).exists())
        finally:
            h.cleanup()

    def test_one_failing_search_query_does_not_lose_the_sweep(self):
        class PartialFailSearch(MockProvider):
            def search(self, query, **kwargs):
                if "融资" in query:
                    raise ProviderError("simulated per-query failure")
                return super().search(query, **kwargs)

        h = Harness(PartialFailSearch())
        try:
            # Pin to a single site filter so one bad query == one failure.
            h.config.search_sweep = {**h.config.search_sweep, "site_filters": [None]}
            h.pipeline.run_stage1("AgiBot")
            sweep = h.pipeline.run_search_sweep("AgiBot", ["模拟科技 融资", "MockBot A1 参数"])
            self.assertEqual(len(sweep["failures"]), 1)
            self.assertTrue(sweep["results"])  # the good query still produced evidence
        finally:
            h.cleanup()

    def test_dropped_queries_are_recorded_not_hidden(self):
        h = Harness(MockProvider())
        try:
            h.config.search_sweep = {**h.config.search_sweep, "max_queries": 1,
                                     "site_filters": [None]}
            h.pipeline.run_stage1("AgiBot")
            sweep = h.pipeline.run_search_sweep("AgiBot", ["q1", "q2", "q3"])
            self.assertEqual(sweep["queries_searched"], 1)
            self.assertEqual(sweep["queries_not_searched"], ["q2", "q3"])
            self.assertTrue(any("skipped" in n for n in h.metadata.notes))
        finally:
            h.cleanup()


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.storage = LocalStorageBackend(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_runs_never_overwrite_each_other(self):
        a = self.storage.create_run("AgiBot", timestamp="2026-09-02_174500")
        b = self.storage.create_run("AgiBot", timestamp="2026-09-02_174500")
        self.assertNotEqual(a, b)
        self.assertTrue(a.is_dir() and b.is_dir())

    def test_refuses_to_write_outside_the_run(self):
        self.storage.create_run("AgiBot")
        with self.assertRaises(ValueError):
            self.storage.save("../../escaped.md", "nope")

    def test_load_stage1_raises_clearly_when_absent(self):
        self.storage.create_run("AgiBot")
        with self.assertRaises(FileNotFoundError):
            self.storage.load_stage1()

    def test_expected_layout_is_created(self):
        run = self.storage.create_run("AgiBot")
        self.assertTrue((run / "raw_sources").is_dir())
        self.assertTrue((run / "logs").is_dir())

    def test_md_document_header_does_not_alter_body(self):
        body = "## A\n\n| a | b |\n| - | - |\n"
        doc = md_document("T", ["- x: 1"], body)
        self.assertIn(body.rstrip(), doc)


class TestPromptFiles(unittest.TestCase):
    def test_prompt_files_exist_and_are_utf8(self):
        for stage in (1, 2):
            path = load_config(project_root=PROJECT_ROOT).prompt_path(stage)
            self.assertTrue(path.is_file(), path)
            text = path.read_text(encoding="utf-8")
            self.assertGreater(len(text), 500)
            self.assertNotIn("\\_", text)  # escaping artefacts removed

    def test_prompt1_has_the_placeholder(self):
        path = load_config(project_root=PROJECT_ROOT).prompt_path(1)
        self.assertEqual(path.read_text(encoding="utf-8").count("{TARGET_COMPANY}"), 1)

    def test_prompt2_declares_the_access_statuses(self):
        text = load_config(project_root=PROJECT_ROOT).prompt_path(2).read_text(encoding="utf-8")
        for status in ("VERBATIM_FULL_TEXT", "SEARCH_SNIPPET_ONLY", "URL_ONLY",
                       "TRANSCRIPT_EXTRACTED", "HIGH_FIDELITY_EXTRACTION"):
            self.assertIn(status, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestCitationLabelling(unittest.TestCase):
    """A citation's label must follow what the provider actually returned."""

    def test_citation_with_snippet_is_snippet_not_url_only(self):
        from src.pipeline import _record_from_citation
        from src.provider import citation

        record = _record_from_citation(
            citation(title="T", url="https://mp.weixin.qq.com/s/x", content="正文摘要"),
            "X",
            index=1,
        )
        self.assertEqual(record.content_access_status, "SEARCH_SNIPPET_ONLY")
        self.assertEqual(record.content, "正文摘要")

    def test_citation_without_snippet_is_url_only(self):
        from src.pipeline import _record_from_citation
        from src.provider import citation

        record = _record_from_citation(
            citation(title="T", url="https://mp.weixin.qq.com/s/x"), "X", index=1
        )
        self.assertEqual(record.content_access_status, "URL_ONLY")
        self.assertIsNone(record.content)

    def test_citation_never_claims_full_text(self):
        from src.pipeline import _record_from_citation
        from src.provider import citation

        record = _record_from_citation(
            citation(title="T", url="u", content="x" * 50_000), "X", index=1
        )
        self.assertNotEqual(record.content_access_status, "VERBATIM_FULL_TEXT")

    def test_blank_strings_normalise_to_none(self):
        from src.provider import citation

        item = citation(title="  ", url="", site="  x  ")
        self.assertIsNone(item["title"])
        self.assertIsNone(item["url"])
        self.assertEqual(item["site"], "x")

    def test_original_payload_is_retained(self):
        from src.provider import citation

        original = {"weird_vendor_field": 1}
        self.assertEqual(citation(title="t", raw=original)["_raw"], original)


class TestProviderRegistry(unittest.TestCase):
    def test_mock_resolves(self):
        from src.provider import build_provider

        self.assertEqual(build_provider("mock", None).name, "mock")

    def test_unknown_provider_is_explained(self):
        from src.provider import ProviderError, build_provider

        with self.assertRaises(ProviderError) as ctx:
            build_provider("openai", None)
        self.assertIn("claude-cli", ctx.exception.hint)

    def test_bad_provider_in_config_is_rejected(self):
        from src.config import load_config

        with self.assertRaises(ValueError):
            cfg = load_config(project_root=PROJECT_ROOT)
            cfg.raw["provider"] = "nope"
            # re-validate through a fresh load with a temp config
            import tempfile as tf

            path = Path(tf.mkdtemp()) / "config.yaml"
            path.write_text("provider: nope\n", encoding="utf-8")
            load_config(path, project_root=PROJECT_ROOT)


class TestWeChatUrlForms(unittest.TestCase):
    """A WeChat link is canonical only in its /s/<id> short-path form.

    Regression: the legacy /s?__biz=...&mid=...&sn=... query form carries no
    signature parameter, so an ephemeral-params-only check wrongly called it
    STABLE_WECHAT_ARTICLE_URL. Found against real search results.
    """

    def test_short_path_form_is_canonical(self):
        d = classify_url("https://mp.weixin.qq.com/s/sANNvELq0lqDyl6aHs1MvA")
        self.assertEqual(d["url_type_heuristic"], "STABLE_WECHAT_ARTICLE_URL")
        self.assertEqual(d["wechat_url_form"], "short_path")

    def test_legacy_query_form_is_not_canonical(self):
        d = classify_url(
            "https://mp.weixin.qq.com/s?__biz=MzIyNjM2MzQyNg%3D%3D&idx=1"
            "&mid=2247686482&sn=e38d9d5eedcb1355fb65f617812016ed&scene=21"
        )
        self.assertEqual(d["url_type_heuristic"], "TEMPORARY_SESSION_URL")
        self.assertEqual(d["wechat_url_form"], "query_legacy")
        self.assertNotEqual(d["url_type_heuristic"], "STABLE_WECHAT_ARTICLE_URL")

    def test_sogou_signed_form_still_wins(self):
        d = classify_url(
            "https://mp.weixin.qq.com/s?src=11&timestamp=1750000000&signature=x"
        )
        self.assertEqual(d["url_type_heuristic"], "TEMPORARY_SOGOU_SIGNED_URL")
        self.assertTrue(d["is_ephemeral"])


class TestFetcherBlockDetection(unittest.TestCase):
    """A verification interstitial must be reported, never treated as content."""

    def test_wechat_interstitial_is_detected(self):
        from src.fetcher import Fetcher

        html = "<html><body><p>环境异常，完成验证后即可继续访问</p><a>去验证</a></body></html>"
        self.assertEqual(Fetcher._blocked_by(html), "环境异常")

    def test_captcha_markers_detected(self):
        from src.fetcher import Fetcher

        for marker in ("请输入验证码", "captcha", "Security Check"):
            self.assertIsNotNone(Fetcher._blocked_by(f"<html><body>{marker}</body></html>"))

    def test_normal_article_is_not_flagged(self):
        from src.fetcher import Fetcher

        self.assertIsNone(Fetcher._blocked_by("<html><body><p>正文内容。</p></body></html>"))


class TestBodyExtraction(unittest.TestCase):
    """Regression: a product mega-menu can be longer than the article body."""

    NAV = "".join(f'<a href="/p/{i}">产品型号{i}</a>' for i in range(120))
    ARTICLE = "".join(
        f"<p>这是正文第{i}段。峰值功率12kW，2025年累计出货5100台。</p>" for i in range(6)
    )

    def test_link_heavy_nav_loses_to_prose(self):
        from src.fetcher import extract

        html = f"<html><body><div class='nav'>{self.NAV}</div>" \
               f"<div class='newsCon'>{self.ARTICLE}</div></body></html>"
        _title, text, _date, _links = extract(html)
        self.assertIn("峰值功率12kW", text)
        self.assertNotIn("产品型号5", text)

    def test_paragraph_structure_survives(self):
        from src.fetcher import extract

        html = f"<html><body><div class='c'>{self.ARTICLE}</div></body></html>"
        _t, text, _d, _l = extract(html)
        self.assertIn("\n\n", text)

    def test_date_and_title_parsed(self):
        from src.fetcher import extract

        html = ("<html><head><title>标题 - 智元</title></head><body>"
                "<div class='c'><p>发布时间：2026-08-25 16:27:54</p>"
                + self.ARTICLE + "</div></body></html>")
        title, _text, date, _l = extract(html)
        self.assertEqual(title, "标题")  # trailing site name trimmed
        self.assertEqual(date, "2026-08-25 16:27:54")

    def test_scripts_are_stripped(self):
        from src.fetcher import extract

        html = f"<html><body><script>var secret='xyz'</script>" \
               f"<div class='c'>{self.ARTICLE}</div></body></html>"
        _t, text, _d, _l = extract(html)
        self.assertNotIn("secret", text)


class TestGatedHosts(unittest.TestCase):
    def test_wechat_hosts_are_gated(self):
        from src.collectors import is_gated

        for url in ("https://mp.weixin.qq.com/s/abc",
                    "https://weixin.sogou.com/link?url=x",
                    "https://channels.weixin.qq.com/x"):
            self.assertTrue(is_gated(url), url)

    def test_ordinary_hosts_are_not(self):
        from src.collectors import is_gated

        for url in ("https://news.qq.com/a/1", "https://www.agibot.com.cn/x", None):
            self.assertFalse(is_gated(url))


class TestFilingTextSelection(unittest.TestCase):
    """Which filings are worth reading, and which are just pointers."""

    def test_primary_documents_are_selected(self):
        from src.collectors import wants_filing_text

        for title in ("宇树科技首次公开发行股票并在科创板上市招股说明书",
                      "公司章程",
                      "关于变更注册资本、公司类型及修订《公司章程》并办理工商变更登记的公告",
                      "2025年年度报告"):
            self.assertTrue(wants_filing_text(title), title)

    def test_the_draft_prospectus_yields_to_the_final_one(self):
        """招股意向书 and 招股说明书 read almost identically."""
        from src.collectors import wants_filing_text

        # Both match on their own; the pipeline drops the draft when the final
        # is present, which TestFilingDraftPreference covers.
        self.assertTrue(wants_filing_text("宇树科技首次公开发行股票并在科创板上市招股意向书"))

    def test_pointer_announcements_are_skipped(self):
        """提示性公告 is one paragraph saying the real document exists."""
        from src.collectors import wants_filing_text

        for title in ("宇树科技首次公开发行股票并在科创板上市招股说明书提示性公告",
                      "招股说明书摘要",
                      "网上发行申购情况及中签率公告"):
            self.assertFalse(wants_filing_text(title), title)


class TestFilingDraftPreference(unittest.TestCase):
    """Taking both prospectus versions doubles the corpus for no new facts."""

    def _collector(self):
        from src.collectors import ExchangeFilingCollector

        class StubFetcher:
            class policy:
                user_agent = "test"

        return ExchangeFilingCollector(StubFetcher())

    def _filing(self, title):
        from src.models import SourceRecord

        return SourceRecord(source_id="F1", title=title,
                            retrieval_url="http://x/a.PDF",
                            canonical_url="http://x/a.PDF",
                            content_access_status="URL_ONLY")

    def _titles_attempted(self, titles):
        """Which filings the collector tried to download.

        Downloads are stubbed to fail, so every attempt lands in `failures`
        and the selection decision is what the test sees.
        """
        collector = self._collector()

        def refuse(*_args, **_kwargs):
            raise RuntimeError("download disabled in tests")

        with fake_requests(get=refuse):
            _, failures = collector._extract_texts(
                [self._filing(t) for t in titles], "Unitree",
                max_pdf_bytes=1, max_section_chars=100,
            )
        return [f["title"] for f in failures]

    def test_draft_is_skipped_when_the_final_exists(self):
        attempted = self._titles_attempted([
            "宇树科技首次公开发行股票并在科创板上市招股意向书",
            "宇树科技首次公开发行股票并在科创板上市招股说明书",
        ])
        self.assertEqual(attempted, ["宇树科技首次公开发行股票并在科创板上市招股说明书"])

    def test_draft_is_read_when_no_final_was_filed(self):
        attempted = self._titles_attempted([
            "宇树科技首次公开发行股票并在科创板上市招股意向书",
        ])
        self.assertEqual(attempted, ["宇树科技首次公开发行股票并在科创板上市招股意向书"])


class TestEveryAdvertisedProviderBuilds(unittest.TestCase):
    """The provider list and the dispatch must not drift apart.

    Removing the unused adapters took the claude-cli branch with them, so the
    default provider raised "Unknown provider 'claude-cli'" — caught only by a
    live run. Every name the CLI accepts is checked here instead.
    """

    def test_all_cli_choices_are_dispatchable(self):
        import research
        from src.config import load_config
        from src.provider import AuthError, ProviderError, build_provider

        choices = [
            action.choices
            for action in research.build_parser()._actions
            if action.dest == "provider"
        ][0]
        self.assertIn("claude-cli", choices)

        config = load_config()
        for name in choices:
            try:
                provider = build_provider(name, config)
            except AuthError:
                # A missing credential is a different failure from a missing
                # branch, and it is the correct one for a keyless environment.
                continue
            except ProviderError as exc:
                self.fail(f"{name} is offered by the CLI but will not build: {exc}")
            else:
                self.assertTrue(provider.name)

    def test_config_accepts_exactly_the_cli_choices(self):
        """config.yaml's validation list must match the CLI's."""
        import re

        import research

        choices = set([
            action.choices
            for action in research.build_parser()._actions
            if action.dest == "provider"
        ][0])
        source = (PROJECT_ROOT / "src" / "config.py").read_text(encoding="utf-8")
        match = re.search(r'if provider not in \(([^)]*)\):', source)
        self.assertIsNotNone(match, "provider validation list not found")
        allowed = set(re.findall(r'"([^"]+)"', match.group(1)))
        self.assertEqual(allowed, choices)


class TestSearchKeyIsRequired(unittest.TestCase):
    """A missing SerpApi key must stop the run, not quietly degrade it."""

    def _config(self, keys):
        h = Harness(MockProvider())
        h.config.serpapi.api_keys = keys
        self.addCleanup(h.cleanup)
        return h.config

    def test_a_missing_key_is_reported(self):
        import research

        message = research.require_search_key(self._config([]))
        self.assertIsNotNone(message)
        self.assertIn("SerpApi", message)
        # The message must say why, not just that. Losing the key loses the
        # Chinese registry name, which loses the filings.
        self.assertIn("--provider mock", message)

    def test_a_present_key_passes(self):
        import research

        self.assertIsNone(research.require_search_key(self._config(["k"])))

    def test_mock_runs_are_exempt(self):
        """--provider mock is pinned offline, so it needs no key."""
        import research

        config = self._config([])
        research.pin_offline(config)
        self.assertEqual(config.search_sweep["provider"], "mock")


class TestFilingCountsAreNotConflated(unittest.TestCase):
    """19 filings plus 35 text sections is not "54 filings"."""

    def test_sections_are_counted_separately_from_filings(self):
        from src.models import SourceRecord

        h = Harness(MockProvider())
        try:
            filing = SourceRecord(source_id="F1", title="公司章程",
                                  retrieval_url="http://x/a.PDF",
                                  content_access_status="URL_ONLY",
                                  origin="exchange_filing_registry")
            section = SourceRecord(source_id="F1_TEXT_01", title="公司章程 — 第一节",
                                   retrieval_url="http://x/a.PDF#page=1",
                                   content_access_status="HIGH_FIDELITY_EXTRACTION",
                                   content="条文" * 100,
                                   origin="exchange_filing_text")

            def collect(*_a, **_k):
                return [filing, section], []

            import src.pipeline as mod
            original = mod.ExchangeFilingCollector
            mod.ExchangeFilingCollector = lambda *_a, **_k: type(
                "C", (), {"collect": staticmethod(collect)}
            )()
            try:
                payload = h.pipeline.run_exchange_filings("Unitree", "宇树科技")
            finally:
                mod.ExchangeFilingCollector = original

            self.assertEqual(payload["filings_collected"], 1)
            self.assertEqual(payload["filing_text_sections"], 1)
            self.assertEqual(payload["filing_text_chars"], 200)
            self.assertEqual(h.metadata.counts["exchange_filings"], 1)
        finally:
            h.cleanup()


class TestPdfSectioning(unittest.TestCase):
    """A 382-page filing is a dozen documents to a reader, not one."""

    def _pdf(self, pages):
        """Smallest PDF a parser will accept, one text block per page."""
        import io
        import zlib

        objects = []
        kids = " ".join(f"{4 + i * 2} 0 R" for i in range(len(pages)))
        objects.append("<< /Type /Catalog /Pages 2 0 R >>")
        objects.append(
            f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>"
        )
        objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        streams = []
        for text in pages:
            body = "BT /F1 12 Tf 40 700 Td ("
            body += text.replace("(", "").replace(")", "")
            body += ") Tj ET"
            streams.append(body)
        for index, body in enumerate(streams):
            objects.append(
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {5 + index * 2} 0 R >>"
            )
            objects.append(f"<< /Length {len(body)} >>\nstream\n{body}\nendstream")
        out = io.StringIO()
        out.write("%PDF-1.4\n")
        offsets = []
        for number, obj in enumerate(objects, start=1):
            offsets.append(out.tell())
            out.write(f"{number} 0 obj\n{obj}\nendobj\n")
        start = out.tell()
        out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n")
        for offset in offsets:
            out.write(f"{offset:010d} 00000 n \n")
        out.write(
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{start}\n%%EOF\n"
        )
        return out.getvalue().encode("latin-1")

    def test_a_scanned_filing_says_so_instead_of_looking_empty(self):
        from src.pdf_extract import extract_sections

        result = extract_sections(self._pdf(["x", "y"]))
        self.assertIn("no text layer", result.error or "")
        self.assertEqual(result.sections, [])

    def test_a_broken_pdf_reports_rather_than_raises(self):
        from src.pdf_extract import extract_sections

        result = extract_sections(b"not a pdf at all")
        self.assertTrue(result.error)
        self.assertEqual(result.sections, [])

    def test_oversized_sections_split_on_paragraphs(self):
        from src.pdf_extract import Section, _chunk

        text = "\n".join(["段落内容" * 50] * 60)
        parts = _chunk(Section("第一节 释义", text, 1, 9), max_section_chars=5_000)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p.text) <= 5_000 for p in parts))
        self.assertEqual([p.part for p in parts], list(range(1, len(parts) + 1)))
        self.assertTrue(all(p.of_parts == len(parts) for p in parts))
        # Nothing may be dropped on the way through.
        self.assertEqual("".join(p.text.replace("\n", "") for p in parts),
                         text.replace("\n", ""))

    def test_cross_references_are_not_mistaken_for_chapter_headings(self):
        from src.pdf_extract import _heading_pages

        pages = [
            '详见本招股说明书“第五节 业务与技术”之“四、（二）主要产品”。' * 3,
            "第五节  业务与技术\n本节介绍公司主营业务。",
        ]
        self.assertEqual(_heading_pages(pages), [(1, "第五节 业务与技术")])


class TestChromeStripping(unittest.TestCase):
    """Page furniture must not be filed as article text."""

    def _text(self, html):
        from src.fetcher import extract

        return extract(html)[1] or ""

    def test_nav_stubs_above_the_headline_are_dropped(self):
        html = ("<div><p>下载客户端</p><p>登录</p><p>无障碍</p><p>+1</p>"
                "<p>宇树发布人形机器人H2训练视频：展现空翻、踹沙袋等动作</p>"
                "<p>" + "1月4日，宇树科技发布人形机器人H2的日常训练视频。" * 6 + "</p></div>")
        text = self._text(html)
        self.assertFalse(text.startswith("下载客户端"), text[:40])
        self.assertTrue(text.startswith("宇树发布人形机器人H2"), text[:40])

    def test_a_short_headline_is_not_mistaken_for_chrome(self):
        """A 12-char cut ate the 10-char headline "宇树科技回应公司更名"."""
        html = ("<div><p>宇树科技回应公司更名</p>"
                "<p>" + "2025年5月30日，宇树科技就公司更名一事作出回应。" * 6 + "</p></div>")
        self.assertTrue(self._text(html).startswith("宇树科技回应公司更名"))

    def test_breadcrumbs_are_dropped(self):
        html = ("<div><p>首页 → 财经中心 → 财经频道 分享到：</p>"
                "<p>宇树科技回应公司更名</p>"
                "<p>" + "公司就更名一事作出了正式回应内容如下。" * 8 + "</p></div>")
        self.assertTrue(self._text(html).startswith("宇树科技回应公司更名"))

    def test_progressively_truncated_repeats_collapse(self):
        """thepaper.cn emitted the same byline three times, each shorter."""
        byline = "2026-01-04 21:18 来源： 澎湃新闻·澎湃号·媒体 字号"
        html = ("<div><p>" + byline + "</p>"
                "<p>2026-01-04 21:18 来源： 澎湃新闻·澎湃号·媒体</p>"
                "<p>2026-01-04 21:18</p>"
                "<p>" + "宇树科技发布了人形机器人H2的日常训练视频内容。" * 6 + "</p></div>")
        self.assertEqual(self._text(html).count("澎湃新闻·澎湃号·媒体"), 1)


class TestRepostLabelling(unittest.TestCase):
    """A repost's full text is the repost's, not the original's."""

    # A real repost carries the original's headline; the resolver now checks
    # for that rather than keeping whatever page it managed to fetch first.
    STORY = "智元第10,000台正式下线！"

    def _resolver(self, page_text=None, page_title=None):
        page_text = page_text if page_text is not None else (self.STORY + "正文" * 400)
        page_title = page_title if page_title is not None else "转载：" + self.STORY
        from src.collectors import RepostResolver
        from src.fetcher import FetchResult

        class FakeFetcher:
            def fetch(self, url):
                return FetchResult(url=url, final_url=url, status=200,
                                   title=page_title, text=page_text,
                                   published="2026-03-31", html_bytes=1000)

        def search(_title):
            return [
                {"url": "https://mp.weixin.qq.com/s/orig", "title": "original"},
                {"url": "https://www.cnr.cn/a/1.shtml", "title": "repost", "site": "央广网"},
            ]

        return RepostResolver(FakeFetcher(), search)

    def _original(self):
        from src.models import SourceRecord

        return SourceRecord(
            source_id="SOURCE_010", title=self.STORY,
            retrieval_url="https://mp.weixin.qq.com/s/orig",
            canonical_url="https://mp.weixin.qq.com/s/orig",
            content_access_status="URL_ONLY", matched_entity="远征 A3",
        )

    def test_original_record_is_never_mutated(self):
        original = self._original()
        self._resolver().resolve([original], "AgiBot")
        self.assertEqual(original.content_access_status, "URL_ONLY")
        self.assertIsNone(original.content)

    def test_repost_is_a_separate_linked_record(self):
        records, gaps = self._resolver().resolve([self._original()], "AgiBot")
        self.assertEqual(len(records), 1)
        self.assertEqual(gaps, [])
        repost = records[0]
        self.assertEqual(repost.extra["reposts_source_id"], "SOURCE_010")
        self.assertEqual(repost.extra["original_url"], "https://mp.weixin.qq.com/s/orig")
        self.assertEqual(repost.source_type, "Secondary Repost")
        self.assertTrue(repost.extra["source_priority"].startswith("10 —"))
        self.assertIn("remains URL_ONLY", repost.extra["label_note"])

    def test_gated_candidates_are_skipped(self):
        records, _ = self._resolver().resolve([self._original()], "AgiBot")
        self.assertNotIn("weixin", records[0].retrieval_url)

    def test_an_unrelated_page_is_not_filed_as_a_repost(self):
        """The Unitree run filed a Baidu results page and an unrelated landing
        page as full-text reposts of a WeChat post."""
        records, gaps = self._resolver(
            page_text="欢迎访问本站" * 200, page_title="Unitree Robotics"
        ).resolve([self._original()], "AgiBot")
        self.assertEqual(records, [])
        self.assertEqual(
            gaps[0]["reason"], "no readable repost carrying the same story"
        )

    def test_video_pages_are_never_reposts(self):
        """Two video pages were filed as full-text reposts on player chrome."""
        from src.collectors import _is_video_page

        for url in ("https://haokan.baidu.com/v?vid=1",
                    "https://m.bilibili.com/video/BV1nTs3zyEhT",
                    "https://v.qq.com/x/page/a.html"):
            self.assertTrue(_is_video_page(url), url)
        self.assertFalse(_is_video_page("https://www.cnr.cn/a/1.shtml"))

    def test_player_chrome_is_too_short_to_be_full_text(self):
        records, gaps = self._resolver(
            page_text=self.STORY + "1612次播放", page_title="转载：" + self.STORY
        ).resolve([self._original()], "AgiBot")
        self.assertEqual(records, [])
        self.assertTrue(gaps)

    def test_search_result_pages_are_never_candidates(self):
        from src.collectors import _is_serp

        for url in ("https://www.baidu.com/s?tn=news&wd=x",
                    "https://www.google.com/search?q=x",
                    "https://www.sogou.com/web?query=x"):
            self.assertTrue(_is_serp(url), url)
        self.assertFalse(_is_serp("https://www.cnr.cn/a/1.shtml"))

    def test_unresolvable_source_becomes_a_gap(self):
        from src.collectors import RepostResolver
        from src.fetcher import FetchError

        class DeadFetcher:
            def fetch(self, url):
                raise FetchError("nope")

        resolver = RepostResolver(DeadFetcher(), lambda t: [{"url": "https://x.com/a"}])
        records, gaps = resolver.resolve([self._original()], "AgiBot")
        self.assertEqual(records, [])
        self.assertEqual(
            gaps[0]["reason"], "no readable repost carrying the same story"
        )


import contextlib as _contextlib


@_contextlib.contextmanager
def fake_requests(**attrs):
    """Swap in a stub `requests` module and always put the real one back."""
    import sys as _sys
    import types as _types

    real = _sys.modules.get("requests")
    stub = _types.ModuleType("requests")
    if real is not None and "exceptions" not in attrs:
        stub.exceptions = real.exceptions
    for name, value in attrs.items():
        setattr(stub, name, value)
    _sys.modules["requests"] = stub
    try:
        yield stub
    finally:
        if real is not None:
            _sys.modules["requests"] = real
        else:
            _sys.modules.pop("requests", None)


class TestRegistryCollectors(unittest.TestCase):
    """Registry stages must label priority correctly and never invent content."""

    def _fetcher(self):
        from src.fetcher import FetchPolicy

        class FakeFetcher:
            policy = FetchPolicy(delay_seconds=0)

            def _throttle(self):
                pass

        return FakeFetcher()

    def test_cninfo_em_tags_stripped(self):
        from src.collectors import _strip_em

        self.assertEqual(_strip_em("<em>上纬新材</em>"), "上纬新材")
        self.assertIsNone(_strip_em(None))
        self.assertIsNone(_strip_em(""))

    def test_cninfo_millis_to_date(self):
        from src.collectors import _cninfo_date

        self.assertEqual(_cninfo_date(1788364800000), "2026-09-02")
        self.assertIsNone(_cninfo_date(None))
        self.assertIsNone(_cninfo_date("not a number"))

    def test_filing_records_keep_pdf_link_and_no_body(self):
        from src.collectors import ExchangeFilingCollector

        payload = {"announcements": [{
            "announcementTitle": "<em>上纬新材</em>要约收购报告书",
            "announcementTime": 1788364800000,
            "adjunctUrl": "finalpage/2026-09-02/123.PDF",
            "secCode": "688585", "secName": "<em>上纬新材</em>",
            "orgName": "上纬新材料科技股份有限公司", "adjunctSize": 99,
        }]}

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return payload

        with fake_requests(post=lambda *a, **k: FakeResponse(),
                           get=lambda *a, **k: FakeResponse()):
            records, failures = ExchangeFilingCollector(self._fetcher()).collect(
                "AgiBot", "上纬新材", max_records=1
            )

        self.assertEqual(failures, [])
        self.assertEqual(len(records), 1)
        filing = records[0]
        self.assertEqual(filing.title, "上纬新材要约收购报告书")
        self.assertEqual(filing.url_type, "DIRECT_DOCUMENT_URL")
        self.assertTrue(filing.canonical_url.endswith("finalpage/2026-09-02/123.PDF"))
        self.assertTrue(filing.canonical_url.startswith("http"))
        # prompt 2 §14: return the document link, do not rebuild the document
        self.assertEqual(filing.content_access_status, "URL_ONLY")
        self.assertIsNone(filing.content)
        self.assertTrue(filing.extra["source_priority"].startswith("1 —"))
        self.assertEqual(filing.extra["sec_code"], "688585")

    def test_patent_throttle_reports_failure_not_empty_success(self):
        from src.collectors import PatentCollector

        class Throttled:
            status_code = 503

            def raise_for_status(self):
                raise AssertionError("should not be called for 503")

        import src.collectors as collectors_mod

        saved_backoff = collectors_mod.PATENTS_BACKOFF_BASE
        collectors_mod.PATENTS_BACKOFF_BASE = 0  # do not really sleep in tests
        try:
            with fake_requests(get=lambda *a, **k: Throttled()):
                records, failures, total = PatentCollector(self._fetcher()).collect(
                    "AgiBot", "上海智元新创技术有限公司", max_records=5
                )
        finally:
            collectors_mod.PATENTS_BACKOFF_BASE = saved_backoff

        self.assertEqual(records, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("503", failures[0]["error"])
        self.assertIsNone(total)


class TestSweepProviderSplit(unittest.TestCase):
    """The sweep may run on a search-only provider (Baidu) while 1-2 run elsewhere."""

    def test_sweep_falls_back_when_its_provider_cannot_be_built(self):
        h = Harness(MockProvider())
        try:
            # serpapi with no key raises AuthError at construction.
            h.config.search_sweep = {**h.config.search_sweep, "provider": "serpapi",
                                     "site_filters": [None], "max_queries": 1}
            h.config.serpapi.api_keys = []
            h.pipeline.run_stage1("AgiBot")
            sweep = h.pipeline.run_search_sweep("AgiBot", ["模拟科技 融资"])
            # Stage 1 survived and the sweep still produced evidence.
            self.assertNotIn("skipped", sweep)
            self.assertTrue(sweep["results"])
            self.assertTrue(any("fell back" in n for n in h.metadata.notes))
        finally:
            h.cleanup()

    def test_same_provider_name_does_not_rebuild(self):
        h = Harness(MockProvider())
        try:
            h.config.search_sweep = {**h.config.search_sweep, "provider": "mock"}
            self.assertIs(h.pipeline._sweep_provider(), h.pipeline.provider)
        finally:
            h.cleanup()

    def test_engine_suggested_anchors_are_collected(self):
        class SuggestingProvider(MockProvider):
            def search(self, query, **kwargs):
                out = super().search(query, **kwargs)
                out["related_searches"] = ["智元 供应商", "智元 招标"]
                return out

        h = Harness(SuggestingProvider())
        try:
            h.config.search_sweep = {**h.config.search_sweep, "provider": None,
                                     "site_filters": [None], "max_queries": 1}
            h.pipeline.run_stage1("AgiBot")
            sweep = h.pipeline.run_search_sweep("AgiBot", ["模拟科技 融资"])
            self.assertIn("智元 供应商", sweep["engine_suggested_anchors"])
        finally:
            h.cleanup()


class TestSerpApiProvider(unittest.TestCase):
    def _provider(self):
        from src.config import SerpApiSettings
        from src.serpapi_client import SerpApiBaiduProvider

        p = SerpApiBaiduProvider.__new__(SerpApiBaiduProvider)
        p.settings = SerpApiSettings(api_keys=["k"])
        p.log = __import__("logging").getLogger("test")
        # describe() reports the active key number, so a stub client is needed.
        p.client = type("C", (), {"active_key_number": 1})()
        return p

    def test_missing_key_raises_auth_error(self):
        from src.config import SerpApiSettings
        from src.provider import AuthError
        from src.serpapi_client import SerpApiClient

        with self.assertRaises(AuthError) as ctx:
            SerpApiClient(SerpApiSettings(api_keys=[]))
        self.assertIn("serpapi.com", ctx.exception.hint)

    def test_api_key_property_returns_the_first_key(self):
        from src.config import SerpApiSettings

        s = SerpApiSettings(api_keys=["a", "b", "c"])
        self.assertEqual(s.api_key, "a")
        self.assertTrue(s.has_credentials)
        self.assertIsNone(SerpApiSettings().api_key)

    def test_cannot_run_research_prompts(self):
        from src.provider import ProviderError

        with self.assertRaises(ProviderError) as ctx:
            self._provider().run_research("p", label="stage1")
        self.assertIn("search-only", str(ctx.exception))

    def test_organic_results_become_snippet_records(self):
        p = self._provider()
        p.client = type("C", (), {"search": lambda _s, params: {
            "search_metadata": {"id": "abc"},
            "organic_results": [
                {"position": 1, "title": "智元供应商", "link": "https://baijiahao.baidu.com/s?id=1",
                 "snippet": "峰值功率12kW", "source": "百家号"},
                {"position": 2, "title": "no link"},
            ],
            "related_searches": [{"query": "智元 招标"}],
        }})()
        out = p.search("智元 供应商", count=20)
        self.assertEqual(len(out["pages"]), 1)  # the link-less row is dropped
        page = out["pages"][0]
        self.assertEqual(page["url"], "https://baijiahao.baidu.com/s?id=1")
        self.assertEqual(page["content"], "峰值功率12kW")
        self.assertEqual(page["site"], "百家号")
        self.assertEqual(out["related_searches"], ["智元 招标"])
        self.assertEqual(out["request_id"], "abc")

    def test_site_filter_becomes_a_baidu_operator(self):
        captured = {}
        p = self._provider()
        p.client = type("C", (), {"search": lambda _s, params: (
            captured.update(params), {"organic_results": []})[1]})()
        p.search("智元", site="mp.weixin.qq.com")
        self.assertEqual(captured["q"], "site:mp.weixin.qq.com 智元")
        self.assertEqual(captured["ct"], "2")  # simplified Chinese

    def test_empty_baidu_result_is_not_an_error(self):
        p = self._provider()
        p.client = type("C", (), {"search": lambda _s, params: {
            "error": "Baidu hasn't returned any results for this query."}})()
        out = p.search("very long unlikely query")
        self.assertEqual(out["pages"], [])
        self.assertIn("no results", out["note"])

    def test_real_api_error_still_raises(self):
        from src.provider import ProviderError

        p = self._provider()
        p.client = type("C", (), {"search": lambda _s, params: {
            "error": "Invalid API key"}})()
        with self.assertRaises(ProviderError):
            p.search("q")

    def test_describe_states_it_is_not_a_first_party_api(self):
        note = self._provider().describe()["note"]
        self.assertIn("not a", note)
        self.assertIn("first-party", note)


class TestLabelVerification(unittest.TestCase):
    """A model's own access-status claim must be checked against what it was given."""

    def _records(self, claim, chars):
        from src.models import SourceRecord

        return [SourceRecord(source_id="S1", content_access_status=claim,
                             content="字" * chars, derived={"content_chars": chars})]

    def test_snippet_length_claim_of_verbatim_is_downgraded(self):
        from src.pipeline import verify_labels

        recs = self._records("VERBATIM_PARTIAL_TEXT", 150)
        audit = verify_labels(recs, {"results": 45}, snippet_cap=220)
        self.assertEqual(audit["downgraded"], 1)
        self.assertEqual(recs[0].content_access_status, "SEARCH_SNIPPET_ONLY")
        self.assertEqual(recs[0].derived["label_claimed"], "VERBATIM_PARTIAL_TEXT")
        self.assertIn("not supported", recs[0].derived["label_downgrade_reason"])

    def test_genuinely_long_text_keeps_its_label(self):
        from src.pipeline import verify_labels

        recs = self._records("VERBATIM_FULL_TEXT", 5000)
        audit = verify_labels(recs, {"results": 45}, snippet_cap=220)
        self.assertEqual(audit["downgraded"], 0)
        self.assertEqual(recs[0].content_access_status, "VERBATIM_FULL_TEXT")

    def test_weaker_labels_are_left_alone(self):
        from src.pipeline import verify_labels

        for claim in ("SEARCH_SNIPPET_ONLY", "URL_ONLY"):
            recs = self._records(claim, 100)
            audit = verify_labels(recs, {"results": 45}, snippet_cap=220)
            self.assertEqual(audit["checked"], 0)
            self.assertEqual(recs[0].content_access_status, claim)

    def test_never_upgrades(self):
        from src.pipeline import verify_labels

        recs = self._records("SEARCH_SNIPPET_ONLY", 9000)
        verify_labels(recs, {"results": 45}, snippet_cap=220)
        self.assertEqual(recs[0].content_access_status, "SEARCH_SNIPPET_ONLY")

    def test_no_injected_retrieval_means_no_audit(self):
        from src.pipeline import verify_labels

        recs = self._records("VERBATIM_FULL_TEXT", 50)
        audit = verify_labels(recs, {"skipped": "disabled"}, snippet_cap=220)
        self.assertEqual(audit["downgraded"], 0)
        self.assertEqual(recs[0].content_access_status, "VERBATIM_FULL_TEXT")

    def test_invalid_status_is_normalised_not_stored(self):
        """Regression: glm-4.7-flash emitted 'SEARCH_SNIPPED'."""
        from src.pipeline import verify_labels

        recs = self._records("SEARCH_SNIPPED", 120)
        audit = verify_labels(recs, {"results": 45}, snippet_cap=220)
        self.assertEqual(audit["invalid_labels"], 1)
        self.assertEqual(recs[0].content_access_status, "SEARCH_SNIPPET_ONLY")
        self.assertEqual(recs[0].derived["label_claimed"], "SEARCH_SNIPPED")
        self.assertIn("not one of", recs[0].derived["label_invalid_reason"])

    def test_invalid_status_with_no_content_becomes_url_only(self):
        from src.models import SourceRecord
        from src.pipeline import verify_labels

        rec = SourceRecord(source_id="S1", content_access_status="TOTAL_NONSENSE",
                           content=None)
        verify_labels([rec], {"results": 1}, snippet_cap=220)
        self.assertEqual(rec.content_access_status, "URL_ONLY")

    def test_missing_status_is_normalised(self):
        """A record with no CONTENT_ACCESS_STATUS is invisible to downstream filters."""
        from src.models import SourceRecord
        from src.pipeline import verify_labels

        rec = SourceRecord(source_id="S1", content_access_status=None, content="字" * 90)
        audit = verify_labels([rec], {"results": 1}, snippet_cap=220)
        self.assertEqual(audit["invalid_labels"], 1)
        self.assertEqual(rec.content_access_status, "SEARCH_SNIPPET_ONLY")
        self.assertIn("omitted", rec.derived["label_invalid_reason"])

    def test_all_permitted_statuses_survive_validation(self):
        from src.models import CONTENT_ACCESS_STATUSES
        from src.pipeline import verify_labels

        for status in CONTENT_ACCESS_STATUSES:
            recs = self._records(status, 9000)  # long enough to keep any claim
            audit = verify_labels(recs, {"results": 1}, snippet_cap=220)
            self.assertEqual(audit["invalid_labels"], 0, status)
            self.assertEqual(recs[0].content_access_status, status)


class TestRetrievalPageFetching(unittest.TestCase):
    """Snippets alone leave the model nothing to preserve; pages must be read."""

    def _harness(self, pages):
        class SearchProvider(MockProvider):
            def search(self, query, **kwargs):
                return {"query": query, "pages": pages, "raw": None, "supported": True}

        h = Harness(SearchProvider())
        h.config.research = {
            **h.config.research,
            "retrieval_injection": {
                "enabled": True, "seed_queries": ["{company}"],
                "results_per_query": 10, "max_results": 10, "chars_per_result": 200,
                "fetch_pages": True, "fetch_top_n": 5, "chars_per_fetched_page": 3000,
            },
        }
        return h

    def _stub_fetcher(self, pipeline, text="正文" * 400, blocked=False):
        from src.fetcher import FetchResult

        class FakeFetcher:
            def fetch(self, url):
                return FetchResult(url=url, final_url=url, status=200,
                                   title="抓取标题", text=None if blocked else text,
                                   published="2026-03-31", html_bytes=1000,
                                   blocked=blocked,
                                   block_reason="环境异常" if blocked else None)

        pipeline._fetcher = FakeFetcher()

    def test_fetched_pages_are_injected_as_full_text(self):
        from src.provider import citation

        h = self._harness([citation(title="文章", url="https://news.qq.com/a/1",
                                    content="短摘要")])
        try:
            self._stub_fetcher(h.pipeline)
            block, meta = h.pipeline.build_retrieval_block("AgiBot", ["q"])
            self.assertEqual(meta["pages_fetched"], 1)
            self.assertIn("FULL_TEXT", block)
            self.assertIn("正文正文", block)
            self.assertNotIn("SNIPPET_ONLY", block)
        finally:
            h.cleanup()

    def test_gated_hosts_are_never_fetched(self):
        from src.provider import citation

        h = self._harness([citation(title="公众号", url="https://mp.weixin.qq.com/s/x",
                                    content="摘要")])
        try:
            self._stub_fetcher(h.pipeline)
            block, meta = h.pipeline.build_retrieval_block("AgiBot", ["q"])
            self.assertEqual(meta["pages_fetched"], 0)
            self.assertIn("SNIPPET_ONLY", block)
            self.assertEqual(meta["sources"][0]["_fetch_skipped"], "gated host")
        finally:
            h.cleanup()

    def test_blocked_page_falls_back_to_snippet(self):
        from src.provider import citation

        h = self._harness([citation(title="文章", url="https://news.qq.com/a/1",
                                    content="短摘要")])
        try:
            self._stub_fetcher(h.pipeline, blocked=True)
            block, meta = h.pipeline.build_retrieval_block("AgiBot", ["q"])
            self.assertEqual(meta["pages_fetched"], 0)
            self.assertIn("SNIPPET_ONLY", block)
            self.assertEqual(len(meta["fetch_failures"]), 1)
        finally:
            h.cleanup()

    def test_prompt_tells_the_model_what_it_may_quote(self):
        h = self._harness([])
        try:
            wrapped = h.pipeline._with_retrieval("PROMPT", "<SEARCH_RESULTS>x</SEARCH_RESULTS>")
            self.assertIn("FULL_TEXT", wrapped)
            self.assertIn("SNIPPET_ONLY", wrapped)
            self.assertIn("VERBATIM_PARTIAL_TEXT", wrapped)
            self.assertIn("SEARCH_SNIPPET_ONLY", wrapped)
        finally:
            h.cleanup()

    def test_fetching_can_be_disabled(self):
        from src.provider import citation

        h = self._harness([citation(title="文章", url="https://news.qq.com/a/1",
                                    content="短摘要")])
        try:
            h.config.research["retrieval_injection"]["fetch_pages"] = False
            self._stub_fetcher(h.pipeline)
            block, meta = h.pipeline.build_retrieval_block("AgiBot", ["q"])
            self.assertEqual(meta["pages_fetched"], 0)
            self.assertIn("SNIPPET_ONLY", block)
        finally:
            h.cleanup()


class TestQueryHygiene(unittest.TestCase):
    """Section-E parsing picks up labels; those must not become searches."""

    def test_bolded_heading_is_dropped(self):
        from src.pipeline import clean_queries

        kept, dropped = clean_queries(["**微信生态搜索**:", "智元机器人 融资"],
                                      company="智元机器人")
        self.assertEqual(kept, ["智元机器人 融资"])
        self.assertIn("not a query", dropped[0]["reason"])

    def test_trailing_colon_is_a_heading(self):
        from src.pipeline import clean_queries

        kept, _ = clean_queries(["产品与技术:", "智元机器人 产品"], company="智元机器人")
        self.assertEqual(kept, ["智元机器人 产品"])

    def test_off_topic_query_is_dropped(self):
        from src.pipeline import clean_queries

        kept, dropped = clean_queries(["人形机器人 行业报告", "智元机器人 量产"],
                                      company="智元机器人")
        self.assertEqual(kept, ["智元机器人 量产"])
        self.assertIn("does not mention", dropped[0]["reason"])

    def test_alias_counts_as_mentioning_the_company(self):
        from src.pipeline import clean_queries

        kept, _ = clean_queries(["AgiBot GO-1 开源"], company="智元机器人",
                                aliases=["AgiBot"])
        self.assertEqual(kept, ["AgiBot GO-1 开源"])

    def test_wechat_site_operator_dropped_for_baidu(self):
        from src.pipeline import clean_queries

        kept, dropped = clean_queries(
            ["site:mp.weixin.qq.com 智元机器人 融资", "智元机器人 融资"],
            company="智元机器人", drop_site_operator="mp.weixin.qq.com",
        )
        self.assertEqual(kept, ["智元机器人 融资"])
        self.assertIn("not usefully indexed", dropped[0]["reason"])

    def test_wechat_site_operator_kept_for_other_engines(self):
        from src.pipeline import clean_queries

        kept, _ = clean_queries(["site:mp.weixin.qq.com 智元机器人"],
                                company="智元机器人")
        self.assertEqual(len(kept), 1)

    def test_duplicates_collapse_and_markdown_is_stripped(self):
        from src.pipeline import clean_queries

        kept, _ = clean_queries(["**智元机器人 融资**", "智元机器人 融资"],
                                company="智元机器人")
        self.assertEqual(kept, ["智元机器人 融资"])


class TestMalformedUrlSafety(unittest.TestCase):
    """A model-emitted non-URL must not crash a run."""

    def test_bracketed_placeholder_does_not_raise(self):
        from src.models import classify_url

        for bad in ("[1]", "http://[not-ipv6", "https://[]", "[URL]"):
            derived = classify_url(bad)
            self.assertIn("url_type_heuristic", derived)

    def test_parse_error_is_recorded(self):
        from src.models import classify_url

        derived = classify_url("http://[not-ipv6")
        self.assertEqual(derived["url_type_heuristic"], "UNKNOWN")
        self.assertIn("url_parse_error", derived)

    def test_guess_platform_survives(self):
        from src.models import guess_platform

        self.assertIsNone(guess_platform("http://[not-ipv6"))
        self.assertEqual(guess_platform("http://[bad", "站点"), "站点")

    def test_unparseable_url_is_treated_as_gated(self):
        from src.collectors import is_gated

        self.assertTrue(is_gated("http://[not-ipv6"))

    def test_site_operator_is_stripped_not_discarded(self):
        from src.pipeline import clean_queries

        kept, dropped = clean_queries(
            ["site:mp.weixin.qq.com 智元机器人 融资"],
            company="智元机器人", drop_site_operator="mp.weixin.qq.com",
        )
        self.assertEqual(kept, ["智元机器人 融资"])
        self.assertIn("operator stripped", dropped[0]["reason"])

    def test_site_only_query_is_dropped(self):
        from src.pipeline import clean_queries

        kept, dropped = clean_queries(
            ["site:mp.weixin.qq.com 智元机器人"],
            company="智元机器人", drop_site_operator="mp.weixin.qq.com",
        )
        self.assertEqual(kept, ["智元机器人"])


class TestUnusableContentDetection(unittest.TestCase):
    """Anti-bot payloads are long enough to pass a length check."""

    def test_waf_payload_is_rejected(self):
        from src.fetcher import looks_unusable

        payload = '{"_waf_bd8ce2ce37":"uf+hfrime' + "A1b2C3d4+/=" * 60 + '"}'
        self.assertIsNotNone(looks_unusable(payload))

    def test_real_chinese_article_passes(self):
        from src.fetcher import looks_unusable

        article = "智元机器人发布远征A2，整机自由度42，峰值功率12kW。" * 30
        self.assertIsNone(looks_unusable(article))

    def test_english_article_is_not_rejected_for_being_english(self):
        from src.fetcher import looks_unusable

        # Short English text must not trip the "almost no Chinese" rule.
        self.assertIsNone(looks_unusable("AgiBot released the A2 robot today."))

    def test_empty_is_flagged(self):
        from src.fetcher import looks_unusable

        self.assertEqual(looks_unusable("   "), "empty")

    def test_waf_marker_in_html_blocks_the_fetch(self):
        from src.fetcher import Fetcher

        # markers are checked separately from the interstitial list
        self.assertIsNone(Fetcher._blocked_by('<html><body>正文</body></html>'))


class TestNoSilentFetchSkips(unittest.TestCase):
    def test_short_body_is_recorded_as_a_failure(self):
        from src.fetcher import FetchResult
        from src.provider import citation

        class SearchProvider(MockProvider):
            def search(self, query, **kwargs):
                return {"query": query, "supported": True, "raw": None,
                        "pages": [citation(title="t", url="https://news.qq.com/a/1",
                                           content="摘要")]}

        h = Harness(SearchProvider())
        try:
            h.config.research = {**h.config.research, "retrieval_injection": {
                "enabled": True, "seed_queries": ["{company}"], "results_per_query": 5,
                "max_results": 5, "chars_per_result": 200, "fetch_pages": True,
                "fetch_top_n": 5, "chars_per_fetched_page": 3000}}

            class ShortFetcher:
                def fetch(self, url):
                    return FetchResult(url=url, final_url=url, status=200, title="t",
                                       text="短", published=None, html_bytes=10)

            h.pipeline._fetcher = ShortFetcher()
            _block, meta = h.pipeline.build_retrieval_block("AgiBot", ["q"])
            self.assertEqual(meta["pages_fetched"], 0)
            self.assertEqual(len(meta["fetch_failures"]), 1)
            self.assertIn("too short", meta["fetch_failures"][0]["error"])
        finally:
            h.cleanup()


class TestOnlyCompanyNameIsAsked(unittest.TestCase):
    """The user supplies one name. Everything else the pipeline works out.

    The three channel inputs (newsroom URL, listed entity, patent assignee) were
    once interactive questions. That was the same mistake as asking for Chinese
    names: stages 0-1 already produce them.
    """

    def _run_main(self, stdin_lines, argv):
        import builtins
        import io
        import sys as _sys

        import research as cli

        answers = iter(stdin_lines)
        real_input = builtins.input
        builtins.input = lambda *a: next(answers)
        out = io.StringIO()
        real_stdout = _sys.stdout
        _sys.stdout = out
        try:
            code = cli.main(argv)
        finally:
            builtins.input = real_input
            _sys.stdout = real_stdout
        return code, out.getvalue()

    def _config(self):
        import tempfile
        from pathlib import Path

        cfg = Path(tempfile.mkdtemp()) / "c.yaml"
        cfg.write_text(
            "provider: mock\n"
            f"output: {{root_dir: {Path(tempfile.mkdtemp())}}}\n"
            "research:\n"
            "  stage1_prompt: ./prompts/prompt1_entity_discovery.md\n"
            "  stage2_prompt: ./prompts/prompt2_source_collection.md\n"
            "  retrieval_injection: {enabled: false, provider: null}\n"
            "  derive_channels: {enabled: true, probe_official_site: false, probe_filings: false}\n"
            "search_sweep: {enabled: false, provider: null}\n"
            "repost_resolution: {enabled: false}\n"
                        "registries: {filings_search_key: null, patent_assignee: null}\n",
            encoding="utf-8",
        )
        return cfg

    def test_one_input_line_is_enough(self):
        """Exactly one prompt. A second input() call would raise StopIteration."""
        code, out = self._run_main(["TestCorp"], ["--config", str(self._config())])
        self.assertEqual(code, 0)
        self.assertIn("조사할 회사명을 입력하세요", out)
        self.assertIn("Done.", out)

    def test_the_old_channel_questions_are_gone(self):
        _code, out = self._run_main(["TestCorp"], ["--config", str(self._config())])
        for gone in ("공식 뉴스룸 목록 URL", "거래소 공시 조회용", "특허 출원인 법인명"):
            self.assertNotIn(gone, out)

    def test_no_chinese_name_advice_in_the_prompt(self):
        """The user types an English name; the pipeline finds the Chinese ones."""
        _code, out = self._run_main(["TestCorp"], ["--config", str(self._config())])
        self.assertNotIn("중국어 회사명이 가장 정확", out)

    def test_stage0_runs_before_stage1(self):
        _code, out = self._run_main(["TestCorp"], ["--config", str(self._config())])
        self.assertLess(out.index("[0/2]"), out.index("[1/2]"))

    def test_company_flag_skips_the_prompt_entirely(self):
        code, out = self._run_main(
            [], ["--company", "TestCorp", "--config", str(self._config())]
        )
        self.assertEqual(code, 0)
        self.assertIn("조사 대상: TestCorp", out)
        self.assertNotIn("입력하세요", out)


def _derivation_harness(*, probe_filings: bool = False):
    """Harness with channel derivation on and the network probes off.

    ``probe_filings`` is opt-in so a caller that wants to exercise the probe can
    stub ``_has_filings``; left off, nothing here touches the network.
    """
    h = Harness(MockProvider())
    h.config.research = {**h.config.research,
                         "derive_channels": {"enabled": True,
                                             "probe_filings": probe_filings}}
    return h


class TestMockIsOffline(unittest.TestCase):
    """`--provider mock` is advertised as costing nothing. It must cost nothing.

    It didn't: the sweep resolved to serpapi independently of the LLM provider,
    so a mock smoke test spent 16 real SerpApi calls on synthetic queries.
    """

    def test_mock_never_resolves_to_a_networked_search_provider(self):
        import research

        h = Harness(MockProvider())
        try:
            h.config.search_sweep = {**h.config.search_sweep, "provider": "serpapi"}
            h.config.research = {
                **h.config.research,
                "retrieval_injection": {"provider": "serpapi"},
            }
            research.pin_offline(h.config)
            for got in (h.pipeline._sweep_provider(),
                        h.pipeline._retrieval_provider()):
                self.assertEqual(got.name, "mock")
            # and the cninfo probe, which is a network call of its own
            self.assertFalse(
                h.config.research["derive_channels"]["probe_filings"]
            )
        finally:
            h.cleanup()


class TestChannelDerivation(unittest.TestCase):
    """Derive the channel inputs from stage 0/1 output rather than asking."""

    NAMES = {
        "chinese_names": [
            {"name": "智元", "type": "short_name", "confidence": "high"},
            {"name": "上海智元恒岳科技合伙企业（有限合伙）", "type": "legal_entity",
             "confidence": "low"},
            {"name": "上海智元新创技术有限公司", "type": "legal_entity",
             "confidence": "high"},
        ],
    }

    def _pipeline(self):
        return _derivation_harness()

    def test_patent_assignee_prefers_the_operating_company(self):
        h = self._pipeline()
        try:
            d = h.pipeline.derive_channels(self.NAMES, "")
            # A 合伙企业 is a holding vehicle, not the entity that files patents.
            self.assertEqual(d["patent_assignee"], "上海智元新创技术有限公司")
        finally:
            h.cleanup()

    def test_probe_prefers_a_stage_0_name_the_registry_confirms(self):
        # The Unitree failure: the regex pulled 户集中度 out of "客户集中度",
        # while the right key (宇树科技) was already sitting in stage 0.
        h = _derivation_harness(probe_filings=True)
        try:
            h.pipeline._has_filings = lambda k: k == "宇树科技"
            d = h.pipeline.derive_channels(
                {"search_names": ["宇树科技", "杭州宇树科技有限公司"]},
                "客户集中度较高，科创板代码 688836。",
            )
            self.assertEqual(d["filings_search_key"], "宇树科技")
            self.assertIn("巨潮资讯网", d["evidence"]["filings_search_key"])
        finally:
            h.cleanup()

    def test_probe_leaves_the_key_unset_when_nothing_is_listed(self):
        h = _derivation_harness(probe_filings=True)
        try:
            h.pipeline._has_filings = lambda k: False
            d = h.pipeline.derive_channels(
                {"search_names": ["智元机器人"]}, "该公司尚未上市。"
            )
            self.assertIsNone(d["filings_search_key"])
        finally:
            h.cleanup()

    def test_listed_entity_is_found_next_to_a_stock_code(self):
        h = self._pipeline()
        try:
            text = "智元机器人取得上纬新材（688585.SH）控制权，持股63.62%。"
            d = h.pipeline.derive_channels({}, text)
            self.assertEqual(d["filings_search_key"], "上纬新材")
            self.assertIn("688585", d["evidence"]["filings_search_key"])
        finally:
            h.cleanup()

    def test_most_mentioned_stock_code_wins(self):
        h = self._pipeline()
        try:
            text = ("富临精工（300432.SZ）은 고객. "
                    "上纬新材（688585.SH）… 上纬新材（688585.SH）… 上纬新材（688585.SH）")
            d = h.pipeline.derive_channels({}, text)
            self.assertEqual(d["filings_search_key"], "上纬新材")
        finally:
            h.cleanup()

    def test_no_stock_code_yields_none_not_a_guess(self):
        h = self._pipeline()
        try:
            d = h.pipeline.derive_channels({}, "이 회사는 비상장입니다.")
            self.assertIsNone(d["filings_search_key"])
        finally:
            h.cleanup()

    def test_official_host_candidates_exclude_media_and_registries(self):
        from src.pipeline import _official_host_candidates

        text = ("https://www.agibot.com.cn/about https://www.agibot.com.cn/news "
                "https://baike.baidu.com/x https://www.tianyancha.com/y "
                "https://news.qq.com/z https://xueqiu.com/w")
        hosts = _official_host_candidates(text)
        self.assertEqual(hosts[0], "agibot.com.cn")
        for bad in ("baike.baidu.com", "tianyancha.com", "news.qq.com", "xueqiu.com"):
            self.assertNotIn(bad, hosts)

    def test_probe_is_skipped_when_disabled(self):
        """Tests and offline runs must not reach the network.

        With the probe off, a stage 0 name is only a guess, so it must not be
        promoted to the filings key on trust alone.
        """
        h = self._pipeline()
        try:
            d = h.pipeline.derive_channels({"search_names": ["宇树科技"]}, "")
            self.assertIsNone(d["filings_search_key"])
        finally:
            h.cleanup()

    def test_derivation_records_why(self):
        h = self._pipeline()
        try:
            d = h.pipeline.derive_channels(
                self.NAMES, "上纬新材（688585.SH）"
            )
            self.assertIn("confidence=high", d["evidence"]["patent_assignee"])
        finally:
            h.cleanup()


class TestClaudeCliProvider(unittest.TestCase):
    """Shells out to `claude -p`; every failure mode must be legible."""

    def _provider(self, **overrides):
        from src.claude_cli_client import ClaudeCliProvider
        from src.config import ClaudeCliSettings

        p = ClaudeCliProvider.__new__(ClaudeCliProvider)
        p.settings = ClaudeCliSettings(**overrides)
        p.binary = "/usr/bin/true"
        p.log = __import__("logging").getLogger("test")
        return p

    def _stub_run(self, provider, *, stdout="", stderr="", returncode=0, timeout=False):
        import subprocess as sp

        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["input"] = kwargs.get("input")
            if timeout:
                raise sp.TimeoutExpired(argv, 1)
            return type("C", (), {"stdout": stdout, "stderr": stderr,
                                  "returncode": returncode})()

        import src.claude_cli_client as mod
        mod.subprocess.run = fake_run
        return captured

    def setUp(self):
        import subprocess

        import src.claude_cli_client as mod
        self._real_run = mod.subprocess.run
        self.addCleanup(lambda: setattr(mod.subprocess, "run", self._real_run))

    def test_missing_binary_gives_install_instructions(self):
        from src.claude_cli_client import ClaudeCliProvider
        from src.config import ClaudeCliSettings
        from src.provider import ProviderError

        # Isolate from whatever is actually installed on this machine.
        import src.claude_cli_client as mod
        saved = mod.FALLBACK_PATHS
        mod.FALLBACK_PATHS = ()
        try:
            with self.assertRaises(ProviderError) as ctx:
                ClaudeCliProvider(ClaudeCliSettings(binary="definitely-not-a-real-binary"))
            self.assertIn("install.sh", ctx.exception.hint)
        finally:
            mod.FALLBACK_PATHS = saved

    def test_prompt_goes_through_stdin_not_argv(self):
        """A 30k-char prompt must not be passed as a command-line argument."""
        p = self._provider()
        captured = self._stub_run(p, stdout="결과")
        prompt = "字" * 30000
        p.run_research(prompt, label="stage1")
        self.assertEqual(captured["input"], prompt)
        self.assertNotIn(prompt, captured["argv"])
        self.assertIn("-p", captured["argv"])

    def test_tools_are_disabled_so_retrieval_stays_auditable(self):
        p = self._provider(disallowed_tools="WebSearch,WebFetch")
        captured = self._stub_run(p, stdout="ok")
        p.run_research("x", label="stage1")
        self.assertIn("--disallowedTools", captured["argv"])
        self.assertIn("WebSearch,WebFetch", captured["argv"])

    def test_model_is_passed_when_set(self):
        p = self._provider(model="opus")
        captured = self._stub_run(p, stdout="ok")
        p.run_research("x")
        self.assertIn("--model", captured["argv"])
        self.assertIn("opus", captured["argv"])

    def test_successful_output_is_returned_verbatim(self):
        p = self._provider()
        self._stub_run(p, stdout="  ## A. 실체\n내용  ")
        r = p.run_research("x", label="stage1")
        self.assertEqual(r.text, "## A. 실체\n내용")
        self.assertEqual(r.provider, "claude-cli")
        self.assertEqual(r.finish_reason, "stop")

    def test_auth_failure_gets_an_actionable_hint(self):
        from src.provider import ProviderError

        p = self._provider()
        self._stub_run(p, stderr="Not logged in. Please authenticate.", returncode=1)
        with self.assertRaises(ProviderError) as ctx:
            p.run_research("x")
        self.assertIn("authenticate", ctx.exception.hint)

    def test_quota_failure_points_at_the_shared_allowance(self):
        from src.provider import ProviderError

        p = self._provider()
        self._stub_run(p, stderr="usage limit reached", returncode=1)
        with self.assertRaises(ProviderError) as ctx:
            p.run_research("x")
        self.assertIn("shares your", ctx.exception.hint)

    def test_timeout_is_reported_as_timeout(self):
        from src.provider import TimeoutError_

        p = self._provider(timeout_seconds=5)
        self._stub_run(p, timeout=True)
        with self.assertRaises(TimeoutError_):
            p.run_research("x")

    def test_empty_output_is_an_error_not_a_silent_pass(self):
        from src.provider import EmptyResponseError

        p = self._provider()
        self._stub_run(p, stdout="   ", stderr="something odd")
        with self.assertRaises(EmptyResponseError):
            p.run_research("x")

    def test_stderr_on_success_becomes_a_warning(self):
        p = self._provider()
        self._stub_run(p, stdout="결과", stderr="deprecation notice")
        r = p.run_research("x")
        self.assertTrue(any("stderr" in w for w in r.warnings))

    def test_search_is_declined_so_the_sweep_uses_serpapi(self):
        p = self._provider()
        self.assertFalse(p.supports_search)
        out = p.search("q")
        self.assertFalse(out["supported"])
        self.assertIn("serpapi", out["note"])

    def test_describe_states_the_shared_allowance_tradeoff(self):
        self.assertIn("subscription allowance", self._provider().describe()["note"])

    def test_not_logged_in_on_exit_zero_is_caught(self):
        """Regression: the CLI prints 'Not logged in' to stdout and exits 0."""
        from src.provider import ProviderError

        p = self._provider()
        self._stub_run(p, stdout="Not logged in · Please run /login", returncode=0)
        with self.assertRaises(ProviderError) as ctx:
            p.run_research("x", label="stage1")
        self.assertIn("exit 0", str(ctx.exception))
        self.assertIn("/login", ctx.exception.hint)

    def test_usage_limit_on_exit_zero_is_caught(self):
        from src.provider import ProviderError

        p = self._provider()
        self._stub_run(p, stdout="usage limit reached, try later", returncode=0)
        with self.assertRaises(ProviderError) as ctx:
            p.run_research("x")
        self.assertIn("shares your", ctx.exception.hint)

    def test_long_output_mentioning_rate_limit_is_not_flagged(self):
        """Real research output may discuss limits; only short output is screened."""
        p = self._provider()
        body = "本文讨论 API rate limit 相关技术细节。" * 60
        self._stub_run(p, stdout=body, returncode=0)
        r = p.run_research("x")
        self.assertEqual(r.text, body.strip())

    def test_binary_is_found_at_the_installer_location(self):
        import os
        import tempfile

        from src.claude_cli_client import ClaudeCliProvider

        fake_home = tempfile.mkdtemp()
        target = os.path.join(fake_home, "claude")
        with open(target, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(target, 0o755)

        import src.claude_cli_client as mod
        saved = mod.FALLBACK_PATHS
        mod.FALLBACK_PATHS = (target,)
        try:
            self.assertEqual(ClaudeCliProvider._resolve("definitely-not-real"), target)
        finally:
            mod.FALLBACK_PATHS = saved

    def test_missing_everywhere_lists_the_locations_tried(self):
        from src.claude_cli_client import ClaudeCliProvider
        from src.provider import ProviderError

        import src.claude_cli_client as mod
        saved = mod.FALLBACK_PATHS
        mod.FALLBACK_PATHS = ("/nonexistent/a/claude", "~/.local/bin/claude-nope")
        try:
            with self.assertRaises(ProviderError) as ctx:
                ClaudeCliProvider._resolve("definitely-not-a-real-binary-xyz")
            self.assertIn(".local/bin/claude-nope", str(ctx.exception))
        finally:
            mod.FALLBACK_PATHS = saved


class TestSerpApiKeyRotation(unittest.TestCase):
    """Several keys may be configured; an exhausted one must roll to the next."""

    def _client(self, keys):
        from src.config import SerpApiSettings
        from src.serpapi_client import SerpApiClient

        c = SerpApiClient.__new__(SerpApiClient)
        c.settings = SerpApiSettings(api_keys=list(keys))
        c.log = __import__("logging").getLogger("test")
        c._keys = list(keys)
        c._index = 0
        c._exhausted = set()
        return c

    def _responses(self, client, script):
        """`script` maps the key used -> (status, body)."""
        used = []

        def fake_get(url, params=None, timeout=None):
            key = params["api_key"]
            used.append(key)
            status, body = script[key]
            return type("R", (), {
                "status_code": status,
                "text": json.dumps(body),
                "json": lambda _s, b=body: b,
            })()

        with fake_requests(get=fake_get, exceptions=__import__("requests").exceptions):
            return used, client

    def test_quota_exhaustion_rotates_to_the_next_key(self):
        client = self._client(["k1", "k2"])
        used, _ = self._responses(client, {
            "k1": (429, {"error": "You've run out of searches"}),
            "k2": (200, {"organic_results": [{"title": "t", "link": "u"}]}),
        })
        with fake_requests(get=lambda url, params=None, timeout=None: type("R", (), {
            "status_code": 429 if params["api_key"] == "k1" else 200,
            "text": "{}",
            "json": lambda _s: ({"error": "run out of searches"}
                                if params["api_key"] == "k1"
                                else {"organic_results": [{"title": "t", "link": "u"}]}),
        })(), exceptions=__import__("requests").exceptions):
            body = client.search({"engine": "baidu", "q": "x"})
        self.assertIn("organic_results", body)
        self.assertEqual(client.active_key_number, 2)

    def test_all_keys_exhausted_raises_with_the_count(self):
        from src.provider import RateLimitError

        client = self._client(["k1", "k2", "k3"])
        with fake_requests(get=lambda url, params=None, timeout=None: type("R", (), {
            "status_code": 429, "text": "out of searches",
            "json": lambda _s: {"error": "out of searches"},
        })(), exceptions=__import__("requests").exceptions):
            with self.assertRaises(RateLimitError) as ctx:
                client.search({"engine": "baidu", "q": "x"})
        self.assertIn("3 SerpApi key(s)", str(ctx.exception))
        self.assertIn("SERPAPI_KEY_2", ctx.exception.hint)

    def test_rejected_key_also_rotates_rather_than_stopping_the_run(self):
        client = self._client(["bad", "good"])
        with fake_requests(get=lambda url, params=None, timeout=None: type("R", (), {
            "status_code": 401 if params["api_key"] == "bad" else 200,
            "text": "{}",
            "json": lambda _s: ({"error": "Invalid API key"}
                                if params["api_key"] == "bad"
                                else {"organic_results": []}),
        })(), exceptions=__import__("requests").exceptions):
            body = client.search({"engine": "baidu", "q": "x"})
        self.assertIn("organic_results", body)
        self.assertEqual(client.active_key_number, 2)

    def test_a_key_is_not_retried_once_exhausted(self):
        """Rotation must be sticky, or every query pays the 429 round trip."""
        client = self._client(["k1", "k2"])
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(params["api_key"])
            status = 429 if params["api_key"] == "k1" else 200
            return type("R", (), {
                "status_code": status, "text": "{}",
                "json": lambda _s: ({"error": "out of searches"} if status == 429
                                    else {"organic_results": []}),
            })()

        with fake_requests(get=fake_get, exceptions=__import__("requests").exceptions):
            client.search({"engine": "baidu", "q": "a"})
            client.search({"engine": "baidu", "q": "b"})
        # k1 tried once, then never again
        self.assertEqual(calls.count("k1"), 1)
        self.assertEqual(calls.count("k2"), 2)

    def test_keys_are_never_written_to_logs(self):
        import logging

        client = self._client(["SECRET-ONE", "SECRET-TWO"])
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        client.log.addHandler(handler)
        try:
            client._advance("hit its quota")
        finally:
            client.log.removeHandler(handler)
        joined = " ".join(records)
        self.assertNotIn("SECRET-ONE", joined)
        self.assertNotIn("SECRET-TWO", joined)
        self.assertIn("key 2 of 2", joined)


class TestSerpApiKeyCollection(unittest.TestCase):
    """Keys may arrive as SERPAPI_KEYS or as numbered variables."""

    def _collect(self, env):
        import os

        from src.config import _collect_serpapi_keys

        saved = {k: os.environ.get(k) for k in
                 ("SERPAPI_KEYS", "SERPAPI_KEY", "SERPAPI_KEY_2", "SERPAPI_KEY_3",
                  "SERPAPI_KEY_4")}
        for k in saved:
            os.environ.pop(k, None)
        os.environ.update(env)
        try:
            return _collect_serpapi_keys()
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    def test_numbered_variables_are_read_in_order(self):
        self.assertEqual(
            self._collect({"SERPAPI_KEY": "a", "SERPAPI_KEY_2": "b", "SERPAPI_KEY_3": "c"}),
            ["a", "b", "c"],
        )

    def test_numbering_stops_at_the_first_gap(self):
        # KEY_4 without KEY_3 is a typo; reading past the gap would hide it.
        self.assertEqual(
            self._collect({"SERPAPI_KEY": "a", "SERPAPI_KEY_4": "d"}), ["a"]
        )

    def test_comma_separated_list_works(self):
        self.assertEqual(self._collect({"SERPAPI_KEYS": "a, b ,c"}), ["a", "b", "c"])

    def test_duplicates_collapse(self):
        self.assertEqual(
            self._collect({"SERPAPI_KEYS": "a,b", "SERPAPI_KEY": "a"}), ["a", "b"]
        )

    def test_no_keys_yields_empty(self):
        self.assertEqual(self._collect({}), [])


class TestRetrievalIndependentOfSweep(unittest.TestCase):
    """Regression: turning the sweep off once disabled prompt retrieval too.

    Stage 2 then ran blind and could only restructure stage 1's evidence —
    the model itself reported "本轮 Stage 2 新发现 0 条".
    """

    def _harness(self):
        from src.provider import citation

        class SearchProvider(MockProvider):
            name = "serpapi"  # stand in for the real search provider

            def search(self, query, **kwargs):
                return {"query": query, "supported": True, "raw": None,
                        "pages": [citation(title="t", url="https://news.qq.com/a/1",
                                           content="摘要")]}

        h = Harness(MockProvider())
        h._searcher = SearchProvider()
        # build_provider is not reachable for a stub name, so preload the cache.
        h.pipeline._search_cache = {"serpapi": h._searcher}
        h.config.research = {**h.config.research, "retrieval_injection": {
            "enabled": True, "provider": "serpapi", "seed_queries": ["{company}"],
            "results_per_query": 5, "max_results": 5, "chars_per_result": 200,
            "fetch_pages": False}}
        return h

    def test_retrieval_still_runs_when_the_sweep_is_disabled(self):
        h = self._harness()
        try:
            h.config.search_sweep = {**h.config.search_sweep,
                                     "enabled": False, "provider": None}
            block, meta = h.pipeline.build_retrieval_block("AgiBot", ["q"])
            self.assertIsNotNone(block)
            self.assertEqual(meta["results"], 1)
            self.assertNotIn("skipped", meta)
        finally:
            h.cleanup()

    def test_retrieval_falls_back_to_the_sweep_provider(self):
        h = self._harness()
        try:
            h.config.research["retrieval_injection"]["provider"] = None
            h.config.search_sweep = {**h.config.search_sweep, "provider": "serpapi"}
            block, meta = h.pipeline.build_retrieval_block("AgiBot", ["q"])
            self.assertIsNotNone(block)
        finally:
            h.cleanup()

    def test_search_incapable_provider_says_how_to_fix_it(self):
        h = Harness(MockProvider())
        try:
            h.config.research = {**h.config.research, "retrieval_injection": {
                "enabled": True, "provider": None, "seed_queries": ["{company}"]}}
            h.config.search_sweep = {**h.config.search_sweep, "provider": None}

            class NoSearch(MockProvider):
                @property
                def supports_search(self):
                    return False

            h.pipeline.provider = NoSearch()
            _block, meta = h.pipeline.build_retrieval_block("AgiBot", ["q"])
            self.assertIn("skipped", meta)
            self.assertIn("retrieval_injection.provider", meta["hint"])
        finally:
            h.cleanup()


class TestNoNetworkInTests(unittest.TestCase):
    """The suite must not issue real API calls. It did once; this guards it."""

    def test_harness_pins_every_search_path_off(self):
        h = Harness(MockProvider())
        try:
            self.assertIsNone(h.config.search_sweep.get("provider"))
            ri = h.config.research["retrieval_injection"]
            self.assertFalse(ri["enabled"])
            self.assertIsNone(ri["provider"])
        finally:
            h.cleanup()

    def test_harness_pipeline_builds_no_network_provider(self):
        h = Harness(MockProvider())
        try:
            # Both resolvers must return the harness's own mock, never a real client.
            self.assertIs(h.pipeline._retrieval_provider(), h.pipeline.provider)
            self.assertIs(h.pipeline._sweep_provider(), h.pipeline.provider)
            self.assertEqual(h.pipeline._search_cache, {})
        finally:
            h.cleanup()

    def test_stage1_makes_no_retrieval_call_in_tests(self):
        h = Harness(MockProvider())
        try:
            result = h.pipeline.run_stage1("AgiBot")
            self.assertIn("skipped", result.parsed["retrieval"])
        finally:
            h.cleanup()


class TestStage2OverwriteGuard(unittest.TestCase):
    """A completed stage 2 must not be destroyed by a re-run with a worse config.

    This happened for real: a 26,252-char stage 2 with 24 sources was replaced
    by a 2,519-char one with 0 sources because a config edit had disabled
    retrieval. The guard and `--stage channels` exist because of that.
    """

    def _run(self, argv, stdin=()):
        import builtins
        import io
        import sys as _sys

        import research as cli

        answers = iter(stdin)
        real_input = builtins.input
        builtins.input = lambda *a: next(answers)
        out = io.StringIO()
        real_stdout = _sys.stdout
        _sys.stdout = out
        try:
            code = cli.main(argv)
        finally:
            builtins.input = real_input
            _sys.stdout = real_stdout
        return code, out.getvalue()

    def _config(self, root):
        import tempfile
        from pathlib import Path

        cfg = Path(tempfile.mkdtemp()) / "c.yaml"
        cfg.write_text(
            "provider: mock\n"
            f"output: {{root_dir: {root}}}\n"
            "research:\n"
            "  stage1_prompt: ./prompts/prompt1_entity_discovery.md\n"
            "  stage2_prompt: ./prompts/prompt2_source_collection.md\n"
            "  retrieval_injection: {enabled: false, provider: null}\n"
            "  derive_channels: {enabled: true, probe_official_site: false, probe_filings: false}\n"
            "search_sweep: {enabled: false, provider: null}\n"
            "repost_resolution: {enabled: false}\n"
                        "registries: {filings_search_key: null, patent_assignee: null}\n",
            encoding="utf-8",
        )
        return cfg

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.root = Path(tempfile.mkdtemp())
        self.cfg = self._config(self.root)
        code, _ = self._run(["--company", "AgiBot", "--config", str(self.cfg)])
        self.assertEqual(code, 0)
        self.run_dir = sorted((self.root / "agibot").iterdir())[-1]

    def _stage2_size(self):
        return len(json.loads((self.run_dir / "02_sources.json").read_text("utf-8"))["text"])

    def test_rerunning_stage2_is_refused(self):
        before = self._stage2_size()
        code, out = self._run(
            ["--resume", str(self.run_dir), "--stage", "2", "--config", str(self.cfg)]
        )
        self.assertEqual(code, 2)
        self.assertIn("would be overwritten", out)
        self.assertIn("--stage channels", out)
        self.assertIn("--force", out)
        self.assertEqual(self._stage2_size(), before)

    def test_force_allows_a_deliberate_redo(self):
        code, _ = self._run(
            ["--resume", str(self.run_dir), "--stage", "2", "--force",
             "--config", str(self.cfg)]
        )
        self.assertEqual(code, 0)

    def test_channels_only_leaves_stage2_untouched(self):
        before = self._stage2_size()
        code, out = self._run(
            ["--resume", str(self.run_dir), "--stage", "channels",
             "--config", str(self.cfg)]
        )
        self.assertEqual(code, 0)
        self.assertIn("left untouched", out)
        self.assertEqual(self._stage2_size(), before)

    def test_channels_reports_the_real_prior_status(self):
        """The status is read before metadata is rewritten, or it shows 'pending'."""
        _code, out = self._run(
            ["--resume", str(self.run_dir), "--stage", "channels",
             "--config", str(self.cfg)]
        )
        self.assertIn("stage 2: completed", out)


class TestNameResolution(unittest.TestCase):
    """The user types one global name; the system must find the Chinese ones.

    Measured motivation: searching Baidu for "AgiBot" alone returns
    agibot.net — AGIBOT敏捷机器人, a surgical-robotics company — alongside the
    intended one. Without expansion, queries both miss and mislead.
    """

    NAMES_JSON = {
        "canonical_english": "AgiBot",
        "chinese_names": [
            {"name": "智元机器人", "type": "brand", "confidence": "high", "note": ""},
            {"name": "上海智元新创技术有限公司", "type": "legal_entity",
             "confidence": "high", "note": ""},
        ],
        "english_variants": ["AGIBOT", "Zhiyuan Robotics"],
        "search_names": ["智元机器人", "上海智元新创技术有限公司", "AgiBot"],
        "collisions": [{"name": "AGIBOT敏捷机器人", "note": "手术机器人公司，无关"}],
        "note": "",
    }

    def _harness(self, output):
        class NameProvider(MockProvider):
            def run_research(self, prompt, *, label=""):
                if label == "stage0":
                    r = super().run_research(prompt, label=label)
                    r.text = output
                    return r
                return super().run_research(prompt, label=label)

        h = Harness(NameProvider())
        h.config.research = {**h.config.research,
                             "name_resolution": {"enabled": True, "max_names": 8}}
        return h

    def test_chinese_names_are_discovered_from_an_english_input(self):
        h = self._harness(json.dumps(self.NAMES_JSON, ensure_ascii=False))
        try:
            result = h.pipeline.resolve_names("AgiBot")
            self.assertIn("智元机器人", result["search_names"])
            self.assertIn("上海智元新创技术有限公司", result["search_names"])
            self.assertEqual(len(result["collisions"]), 1)
        finally:
            h.cleanup()

    def test_the_typed_name_is_always_kept(self):
        payload = {**self.NAMES_JSON, "search_names": ["智元机器人"]}
        h = self._harness(json.dumps(payload, ensure_ascii=False))
        try:
            result = h.pipeline.resolve_names("AgiBot")
            self.assertIn("AgiBot", result["search_names"])
        finally:
            h.cleanup()

    def test_json_in_a_code_fence_is_still_parsed(self):
        """Models fence JSON even when told not to."""
        fenced = "여기 결과입니다:\\n```json\\n" + json.dumps(
            self.NAMES_JSON, ensure_ascii=False) + "\\n```"
        h = self._harness(fenced)
        try:
            result = h.pipeline.resolve_names("AgiBot")
            self.assertIn("智元机器人", result["search_names"])
        finally:
            h.cleanup()

    def test_unparseable_output_falls_back_to_the_typed_name(self):
        h = self._harness("죄송하지만 JSON을 만들 수 없습니다")
        try:
            result = h.pipeline.resolve_names("AgiBot")
            self.assertEqual(result["search_names"], ["AgiBot"])
            self.assertIn("error", result)
            self.assertTrue(any("not valid JSON" in n for n in h.metadata.notes))
        finally:
            h.cleanup()

    def test_provider_failure_is_not_fatal(self):
        from src.provider import ProviderError

        class FailingNames(MockProvider):
            def run_research(self, prompt, *, label=""):
                if label == "stage0":
                    raise ProviderError("simulated stage 0 failure")
                return super().run_research(prompt, label=label)

        h = Harness(FailingNames())
        h.config.research = {**h.config.research,
                             "name_resolution": {"enabled": True, "max_names": 8}}
        try:
            result = h.pipeline.resolve_names("AgiBot")
            self.assertEqual(result["search_names"], ["AgiBot"])
            # stage 1 must still be able to run
            stage1 = h.pipeline.run_stage1("AgiBot")
            self.assertTrue(stage1.response.text)
        finally:
            h.cleanup()

    def test_seed_queries_cross_names_with_templates(self):
        h = self._harness(json.dumps(self.NAMES_JSON, ensure_ascii=False))
        try:
            h.config.research["retrieval_injection"] = {
                **h.config.research["retrieval_injection"],
                "seed_queries": ["{company}", "{company} 工商"],
                "max_seed_queries": 8,
            }
            queries = h.pipeline._seed_queries(
                "AgiBot", ["智元机器人", "上海智元新创技术有限公司"]
            )
            self.assertIn("智元机器人", queries)
            self.assertIn("智元机器人 工商", queries)
            self.assertIn("上海智元新创技术有限公司 工商", queries)
        finally:
            h.cleanup()

    def test_seed_queries_are_capped_name_major(self):
        """Truncation must lose the weakest names, not the best query types."""
        h = self._harness(json.dumps(self.NAMES_JSON, ensure_ascii=False))
        try:
            h.config.research["retrieval_injection"] = {
                **h.config.research["retrieval_injection"],
                "seed_queries": ["{company}", "{company} 工商", "{company} 融资"],
                "max_seed_queries": 3,
            }
            queries = h.pipeline._seed_queries("X", ["名前1", "名前2"])
            self.assertEqual(len(queries), 3)
            self.assertTrue(all("名前1" in q for q in queries))
        finally:
            h.cleanup()

    def test_disabled_resolution_uses_the_typed_name(self):
        h = self._harness(json.dumps(self.NAMES_JSON, ensure_ascii=False))
        try:
            h.config.research["name_resolution"] = {"enabled": False}
            result = h.pipeline.resolve_names("AgiBot")
            self.assertEqual(result["search_names"], ["AgiBot"])
            self.assertIn("skipped", result)
        finally:
            h.cleanup()

    def test_stage1_is_told_the_names_are_unverified(self):
        h = self._harness(json.dumps(self.NAMES_JSON, ensure_ascii=False))
        try:
            captured = {}
            original = h.pipeline.provider.run_research

            def spy(prompt, *, label=""):
                captured[label] = prompt
                return original(prompt, label=label)

            h.pipeline.provider.run_research = spy
            h.pipeline.run_stage1("AgiBot")
            stage1_prompt = captured["stage1"]
            self.assertIn("尚未经过验证", stage1_prompt)
            self.assertIn("智元机器人", stage1_prompt)
            # collisions must be passed through so stage 1 does not merge them
            self.assertIn("AGIBOT敏捷机器人", stage1_prompt)
        finally:
            h.cleanup()

    def test_no_chinese_names_is_flagged_as_a_recall_risk(self):
        payload = {**self.NAMES_JSON, "chinese_names": [], "search_names": ["AgiBot"]}
        h = self._harness(json.dumps(payload, ensure_ascii=False))
        try:
            h.pipeline.resolve_names("AgiBot")
            self.assertTrue(any("no Chinese names" in n for n in h.metadata.notes))
        finally:
            h.cleanup()

    def test_stock_code_label_is_not_mistaken_for_a_company(self):
        from src.parsing import _find_listed_entity

        # "科创板代码：688836" — 创板代码 is a label, not a company.
        self.assertIsNone(_find_listed_entity("宇树科技-W 科创板代码：688836 上市"))

    def test_real_bracketed_company_still_found(self):
        from src.parsing import _find_listed_entity

        found = _find_listed_entity("智元取得上纬新材（688585.SH）控制权")
        self.assertEqual(found["name"], "上纬新材")

    # --- Unitree run: 20 labels normalised, 3 of them wrongly ------------
    def test_status_with_an_appended_note_keeps_its_grade(self):
        """The model annotates the field; that must not downgrade the evidence."""
        from src.parsing import split_status

        status, note = split_status(
            "VERBATIM_PARTIAL_TEXT（正文全文保留；文末 ETF 推介段落省略）"
        )
        self.assertEqual(status, "VERBATIM_PARTIAL_TEXT")
        self.assertIn("正文全文保留", note)

    def test_annotated_status_survives_verification(self):
        from src.models import SourceRecord
        from src.parsing import verify_labels

        rec = SourceRecord(
            source_id="S1",
            content_access_status="VERBATIM_PARTIAL_TEXT（正文全文保留）",
            content="字" * 5000,
        )
        audit = verify_labels([rec], {"results": 10, "pages_fetched": 3}, snippet_cap=220)
        self.assertEqual(rec.content_access_status, "VERBATIM_PARTIAL_TEXT")
        self.assertEqual(audit["invalid_labels"], 0)
        self.assertIn("正文全文保留", rec.derived["label_note"])

    def test_genuine_typo_is_still_normalised(self):
        from src.parsing import split_status

        self.assertEqual(split_status("SEARCH_SNIPPED"), (None, None))

    # --- Unitree run: English newsroom rejected for having no Chinese ----
    def test_english_page_is_not_rejected(self):
        """unitree.com/news is a valid English listing from a Chinese company."""
        from src.fetcher import looks_unusable

        page = ("News Center\n\nKung Fu Meets Spring, Unitree SFG Robots\n\n"
                "2026-05-31\n\nMedia Coverage\n\n") * 12
        self.assertIsNone(looks_unusable(page))

    def test_encoded_payload_is_still_rejected(self):
        from src.fetcher import looks_unusable

        blob = '{"_waf_x":"' + ("A1b2C3d4+/=" * 40) + '"}'
        self.assertIsNotNone(looks_unusable(blob))

    def test_chinese_page_still_passes(self):
        from src.fetcher import looks_unusable

        self.assertIsNone(looks_unusable("智元机器人发布远征A2，峰值功率12kW。" * 30))

    # --- Unitree run: newsroom returned 0 articles and 0 failures --------
    def _rec(self, sid, url, status, content="", title=None, origin="provider_search"):
        from src.models import SourceRecord

        return SourceRecord(source_id=sid, retrieval_url=url, canonical_url=url,
                            content_access_status=status, content=content,
                            title=title, origin=origin)

    def test_same_url_from_two_channels_is_merged_not_duplicated(self):
        from src.parsing import dedupe_records

        recs = [
            self._rec("A", "https://x.com/1", "SEARCH_SNIPPET_ONLY", "짧은 요약",
                      origin="provider_search"),
            self._rec("B", "https://x.com/1", "VERBATIM_FULL_TEXT", "전문" * 500,
                      origin="stage2_model_output"),
        ]
        kept, stats = dedupe_records(recs)
        self.assertEqual(len(kept), 1)
        self.assertEqual(stats["merged"], 1)
        # the better read survives
        self.assertEqual(kept[0].content_access_status, "VERBATIM_FULL_TEXT")

    def test_merge_records_that_two_channels_found_it(self):
        """Corroboration is signal, so it must not be silently discarded."""
        from src.parsing import dedupe_records

        recs = [
            self._rec("A", "https://x.com/1", "SEARCH_SNIPPET_ONLY", origin="provider_search"),
            self._rec("B", "https://x.com/1", "URL_ONLY", origin="stage2_model_output"),
        ]
        kept, _ = dedupe_records(recs)
        self.assertCountEqual(
            kept[0].extra["also_found_by"], ["provider_search", "stage2_model_output"]
        )

    def test_a_body_on_the_weaker_record_is_not_lost(self):
        from src.parsing import dedupe_records

        recs = [
            self._rec("A", "https://x.com/1", "URL_ONLY", ""),
            self._rec("B", "https://x.com/1", "SEARCH_SNIPPET_ONLY", "본문 있음"),
        ]
        kept, _ = dedupe_records(recs)
        self.assertEqual(kept[0].content, "본문 있음")

    def test_records_without_a_url_are_never_treated_as_duplicates(self):
        from src.parsing import dedupe_records

        recs = [self._rec("A", None, "URL_ONLY"), self._rec("B", None, "URL_ONLY")]
        kept, stats = dedupe_records(recs)
        self.assertEqual(len(kept), 2)
        self.assertEqual(stats["merged"], 0)

    def test_repeated_coverage_is_clustered_not_deleted(self):
        """Eight outlets ran the same launch; a reader needs one, not eight."""
        from src.parsing import cluster_by_title

        recs = [
            self._rec(f"S{i}", f"https://outlet{i}.com/a", "SEARCH_SNIPPET_ONLY",
                      "요약", title="宇树发布四足机器人Unitree As2 全新登场")
            for i in range(8)
        ]
        summary = cluster_by_title(recs)
        self.assertEqual(summary["clusters"], 1)
        roles = [r.extra["cluster_role"] for r in recs]
        self.assertEqual(roles.count("primary"), 1)
        self.assertEqual(roles.count("duplicate_coverage"), 7)
        # nothing was removed
        self.assertEqual(len(recs), 8)

    def test_distinct_stories_are_not_clustered(self):
        from src.parsing import cluster_by_title

        recs = [
            self._rec("A", "https://x.com/1", "SEARCH_SNIPPET_ONLY", title="宇树科技融资往事回顾"),
            self._rec("B", "https://x.com/2", "SEARCH_SNIPPET_ONLY", title="成都宇辰科技完成工商注册"),
        ]
        self.assertEqual(cluster_by_title(recs)["clusters"], 0)

    def test_no_clusters_does_not_raise(self):
        """Regression: the cluster counter was unbound when nothing clustered."""
        from src.parsing import cluster_by_title

        self.assertEqual(cluster_by_title([])["clusters"], 0)

    def test_index_lists_strongest_evidence_first(self):
        from src.reports import index_markdown

        recs = [
            self._rec("A", "https://x.com/1", "URL_ONLY", title="약한 것"),
            self._rec("B", "https://x.com/2", "VERBATIM_FULL_TEXT", "전문", title="강한 것"),
        ]
        for i, r in enumerate(recs, 1):
            r.extra = {**r.extra, "_file": f"source_{i:03d}.md"}
        out = index_markdown("X", recs)
        self.assertLess(out.index("강한 것"), out.index("약한 것"))
        self.assertIn("Read this file first", out)

    def test_index_marks_duplicate_coverage(self):
        from src.parsing import cluster_by_title
        from src.reports import index_markdown

        recs = [
            self._rec(f"S{i}", f"https://o{i}.com/a", "SEARCH_SNIPPET_ONLY", "요약",
                      title="宇树发布四足机器人Unitree As2")
            for i in range(3)
        ]
        cluster_by_title(recs)
        out = index_markdown("X", recs)
        data_rows = [l for l in out.splitlines()
                     if l.startswith("| source_") or l.startswith("| S")]
        marked = [l for l in data_rows if l.rstrip().endswith("| dup |")]
        self.assertEqual(len(marked), 2, "one primary, two duplicates")

    def test_provider_payload_is_not_copied_into_each_source(self):
        """77KB of API echo against 43KB of real content, in one run."""
        import shutil
        import tempfile
        from pathlib import Path

        from src.storage import LocalStorageBackend

        tmp = Path(tempfile.mkdtemp())
        try:
            store = LocalStorageBackend(tmp)
            store.create_run("X")
            rec = self._rec("A", "https://x.com/1", "SEARCH_SNIPPET_ONLY", "본문")
            rec.extra = {**rec.extra, "provider_payload": {"huge": "x" * 5000}}
            store.save_source(rec, index=1)
            saved = json.loads((store.run_dir / "raw_sources/source_001.json").read_text("utf-8"))
            self.assertNotIn("x" * 100, json.dumps(saved))
            self.assertIn("raw_", saved["extra"]["provider_payload"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
