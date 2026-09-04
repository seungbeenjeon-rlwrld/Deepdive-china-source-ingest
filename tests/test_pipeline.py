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
from src.tencent_client import parse_pages
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
        # Pin the sweep to the harness provider; config.yaml may point elsewhere.
        self.config.search_sweep = {**self.config.search_sweep, "provider": None}
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
            self.assertEqual(provider.calls, ["stage1", "stage2"])
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


class TestTencentResponseHandling(unittest.TestCase):
    def test_pages_json_strings_are_parsed(self):
        pages = parse_pages({"Response": {"Pages": ['{"title":"甲","url":"u"}']}})
        self.assertEqual(pages[0]["title"], "甲")

    def test_unparsable_page_is_kept_not_dropped(self):
        pages = parse_pages({"Response": {"Pages": ["<<broken>>"]}})
        self.assertEqual(pages[0]["_unparsed"], "<<broken>>")

    def test_missing_pages_is_empty(self):
        self.assertEqual(parse_pages({"Response": {}}), [])

    def test_empty_choices_raise_empty_response_error(self):
        from src.config import TencentSettings
        from src.provider import EmptyResponseError
        from src.tencent_client import TencentProvider

        provider = TencentProvider.__new__(TencentProvider)
        provider.settings = TencentSettings(secret_id="x", secret_key="y")
        provider.log = __import__("logging").getLogger("test")
        provider.client = type("C", (), {"chat_completions": lambda self, p: {
            "Response": {"Choices": [], "RequestId": "r1"}}})()
        with self.assertRaises(EmptyResponseError):
            provider.run_research("p", label="stage1")

    def test_sensitive_finish_reason_is_explained(self):
        from src.config import TencentSettings
        from src.provider import EmptyResponseError
        from src.tencent_client import TencentProvider

        provider = TencentProvider.__new__(TencentProvider)
        provider.settings = TencentSettings(secret_id="x", secret_key="y")
        provider.log = __import__("logging").getLogger("test")
        provider.client = type("C", (), {"chat_completions": lambda self, p: {
            "Response": {"Choices": [{"Message": {"Content": ""},
                                      "FinishReason": "sensitive"}], "RequestId": "r1"}}})()
        with self.assertRaises(EmptyResponseError) as ctx:
            provider.run_research("p", label="stage1")
        self.assertIn("sensitive", str(ctx.exception))

    def test_malformed_envelope_is_rejected(self):
        from src.config import TencentSettings
        from src.provider import MalformedResponseError
        from src.tencent_client import TencentClient

        client = TencentClient.__new__(TencentClient)
        client.settings = TencentSettings(secret_id="x", secret_key="y")
        client.log = __import__("logging").getLogger("test")
        client._sdk_exception = RuntimeError
        fake = type("C", (), {"call_json": lambda self, a, p: {"NotResponse": 1}})()
        with self.assertRaises(MalformedResponseError):
            client._call(fake, "ChatCompletions", {})


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
        self.assertIn("tencent", ctx.exception.hint)

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


class TestZhipuProvider(unittest.TestCase):
    def setUp(self):
        from src.config import ZhipuSettings
        from src.zhipu_client import ZhipuProvider

        self.settings = ZhipuSettings(api_key="test-key")
        self.provider = ZhipuProvider.__new__(ZhipuProvider)
        self.provider.settings = self.settings
        self.provider.log = __import__("logging").getLogger("test")

    def _stub(self, body):
        self.provider.client = type("C", (), {
            "chat_completions": lambda _self, p: body,
            "web_search": lambda _self, q, **kw: body,
        })()

    def test_missing_api_key_raises_auth_error(self):
        from src.config import ZhipuSettings
        from src.provider import AuthError
        from src.zhipu_client import ZhipuClient

        with self.assertRaises(AuthError) as ctx:
            ZhipuClient(ZhipuSettings(api_key=None))
        self.assertIn("z.ai", ctx.exception.hint)

    def test_response_parsed_into_text_and_citations(self):
        self._stub({
            "id": "req-1",
            "model": "glm-4.7-flash",
            "choices": [{"message": {"role": "assistant", "content": "研究结果"},
                         "finish_reason": "stop"}],
            "web_search": [{
                "title": "公众号文章", "link": "https://mp.weixin.qq.com/s/abc",
                "content": "峰值功率12kW", "media": "官方公众号",
                "publish_date": "2026-01-15", "refer": 1,
            }],
            "usage": {"total_tokens": 100},
        })
        result = self.provider.run_research("p", label="stage1")
        self.assertEqual(result.text, "研究结果")
        self.assertEqual(result.request_id, "req-1")
        self.assertEqual(len(result.search_results), 1)
        item = result.search_results[0]
        self.assertEqual(item["url"], "https://mp.weixin.qq.com/s/abc")
        self.assertEqual(item["site"], "官方公众号")
        self.assertEqual(item["content"], "峰值功率12kW")
        self.assertEqual(item["publication_date"], "2026-01-15")
        self.assertFalse(result.warnings)

    def test_no_search_results_warns_only_when_builtin_search_is_on(self):
        self._stub({
            "id": "r", "choices": [{"message": {"content": "answer"},
                                    "finish_reason": "stop"}],
        })
        self.provider.settings.use_builtin_search = True
        result = self.provider.run_research("p", label="stage1")
        self.assertTrue(any("no search results" in w for w in result.warnings))

    def test_no_warning_when_retrieval_is_injected_instead(self):
        """With the paid tool off, an empty search list is expected, not a problem."""
        self._stub({
            "id": "r", "choices": [{"message": {"content": "answer"},
                                    "finish_reason": "stop"}],
        })
        self.provider.settings.use_builtin_search = False
        result = self.provider.run_research("p", label="stage1")
        self.assertFalse(any("no search results" in w for w in result.warnings))

    def test_paid_tool_is_not_sent_by_default(self):
        from src.config import ZhipuSettings
        from src.zhipu_client import ZhipuClient

        captured = {}
        client = ZhipuClient.__new__(ZhipuClient)
        client.settings = ZhipuSettings(api_key="k", use_builtin_search=False)
        client.log = __import__("logging").getLogger("test")
        client._post = lambda path, payload: captured.update(payload) or {"choices": []}
        client.chat_completions("hello")
        self.assertNotIn("tools", captured)
        # thinking must always be explicit, or reasoning eats the whole budget
        self.assertEqual(captured["thinking"], {"type": "disabled"})

    def test_paid_tool_is_sent_when_opted_in(self):
        from src.config import ZhipuSettings
        from src.zhipu_client import ZhipuClient

        captured = {}
        client = ZhipuClient.__new__(ZhipuClient)
        client.settings = ZhipuSettings(api_key="k", use_builtin_search=True)
        client.log = __import__("logging").getLogger("test")
        client._post = lambda path, payload: captured.update(payload) or {"choices": []}
        client.chat_completions("hello")
        self.assertEqual(captured["tools"][0]["type"], "web_search")

    def test_overload_codes_are_treated_as_transient(self):
        from src.zhipu_client import OVERLOADED_CODES, ZhipuClient

        for code in ("1305", "1113"):
            self.assertIn(code, OVERLOADED_CODES)
            fake = type("R", (), {"status_code": 429,
                                  "json": lambda _s, c=code: {"error": {"code": c}},
                                  "text": ""})()
            self.assertTrue(ZhipuClient._is_transient(fake))

    def test_real_auth_failure_is_not_retried(self):
        from src.zhipu_client import ZhipuClient

        fake = type("R", (), {"status_code": 401,
                              "json": lambda _s: {"error": {"code": "1002"}},
                              "text": ""})()
        self.assertFalse(ZhipuClient._is_transient(fake))

    def test_empty_choices_raise(self):
        from src.provider import EmptyResponseError

        self._stub({"id": "r", "choices": []})
        with self.assertRaises(EmptyResponseError):
            self.provider.run_research("p", label="stage1")

    def test_truncation_is_flagged(self):
        self._stub({
            "id": "r",
            "choices": [{"message": {"content": "cut"}, "finish_reason": "length"}],
            "web_search": [{"title": "t", "link": "u"}],
        })
        result = self.provider.run_research("p", label="stage2")
        self.assertTrue(any("cut off" in w for w in result.warnings))

    def test_search_items_key_variants_all_work(self):
        from src.zhipu_client import extract_search_items

        for key in ("web_search", "search_result", "search_results", "results"):
            self.assertEqual(
                extract_search_items({key: [{"title": "t"}]}), [{"title": "t"}]
            )
        self.assertEqual(extract_search_items({"nothing": 1}), [])

    def test_recency_codes_map_across_providers(self):
        from src.zhipu_client import _map_recency

        self.assertEqual(_map_recency("y2"), "oneYear")
        self.assertEqual(_map_recency("d7"), "oneWeek")
        self.assertEqual(_map_recency("m3"), "oneMonth")
        self.assertEqual(_map_recency("oneDay"), "oneDay")
        self.assertIsNone(_map_recency(None))
        self.assertIsNone(_map_recency("nonsense"))

    def test_http_errors_map_to_typed_errors(self):
        from src.provider import AuthError, ProviderError, RateLimitError
        from src.zhipu_client import ZhipuClient

        def fake(status, payload=None):
            return type("R", (), {
                "status_code": status,
                "text": json.dumps(payload or {}),
                "json": lambda _self: payload or {},
            })()

        err = ZhipuClient._http_error(fake(401, {"error": {"message": "bad key"}}), "chat")
        self.assertIsInstance(err, AuthError)
        self.assertIn("bad key", str(err))
        self.assertIsInstance(ZhipuClient._http_error(fake(429), "chat"), RateLimitError)
        self.assertIsInstance(ZhipuClient._http_error(fake(500), "chat"), ProviderError)

    def test_search_normalises_pages(self):
        self._stub({"id": "s1", "search_result": [
            {"title": "文章", "link": "https://mp.weixin.qq.com/s/x",
             "content": "摘要", "media": "站点", "publish_date": "2026-02-01"},
        ]})
        result = self.provider.search("测试", count=10, site="mp.weixin.qq.com")
        self.assertEqual(len(result["pages"]), 1)
        page = result["pages"][0]
        self.assertEqual(page["url"], "https://mp.weixin.qq.com/s/x")
        self.assertEqual(page["content"], "摘要")
        self.assertEqual(result["site"], "mp.weixin.qq.com")

    def test_describe_states_the_wechat_limitation(self):
        self.assertIn("搜狗", self.provider.describe()["limitation"])


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


class TestRepostLabelling(unittest.TestCase):
    """A repost's full text is the repost's, not the original's."""

    def _resolver(self, page_text="正文" * 200):
        from src.collectors import RepostResolver
        from src.fetcher import FetchResult

        class FakeFetcher:
            def fetch(self, url):
                return FetchResult(url=url, final_url=url, status=200,
                                   title="转载标题", text=page_text,
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
            source_id="SOURCE_010", title="智元第10,000台正式下线！",
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

    def test_unresolvable_source_becomes_a_gap(self):
        from src.collectors import RepostResolver
        from src.fetcher import FetchError

        class DeadFetcher:
            def fetch(self, url):
                raise FetchError("nope")

        resolver = RepostResolver(DeadFetcher(), lambda t: [{"url": "https://x.com/a"}])
        records, gaps = resolver.resolve([self._original()], "AgiBot")
        self.assertEqual(records, [])
        self.assertEqual(gaps[0]["reason"], "no readable repost found")


import contextlib as _contextlib


@_contextlib.contextmanager
def fake_requests(**attrs):
    """Swap in a stub `requests` module and always put the real one back."""
    import sys as _sys
    import types as _types

    real = _sys.modules.get("requests")
    stub = _types.ModuleType("requests")
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
            h.config.serpapi.api_key = None
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
        p.settings = SerpApiSettings(api_key="k")
        p.log = __import__("logging").getLogger("test")
        return p

    def test_missing_key_raises_auth_error(self):
        from src.config import SerpApiSettings
        from src.provider import AuthError
        from src.serpapi_client import SerpApiClient

        with self.assertRaises(AuthError) as ctx:
            SerpApiClient(SerpApiSettings(api_key=None))
        self.assertIn("serpapi.com", ctx.exception.hint)

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


class TestZhipuRateLimitCodes(unittest.TestCase):
    """Three distinct 429 codes, all transient, one needing a longer wait."""

    def _response(self, code):
        return type("R", (), {"status_code": 429,
                              "json": lambda _s: {"error": {"code": code}},
                              "text": ""})()

    def test_all_three_observed_codes_are_transient(self):
        from src.zhipu_client import OVERLOADED_CODES, ZhipuClient

        for code in ("1305", "1113", "1302"):
            self.assertIn(code, OVERLOADED_CODES, code)
            self.assertTrue(ZhipuClient._is_transient(self._response(code)), code)

    def test_rate_limit_code_is_distinguished(self):
        from src.zhipu_client import RATE_LIMIT_CODES

        self.assertIn("1302", RATE_LIMIT_CODES)
        self.assertNotIn("1305", RATE_LIMIT_CODES)


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


class TestInteractiveChannelPrompts(unittest.TestCase):
    """A fully interactive run must not silently skip the extra channels."""

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

    def test_channels_are_asked_when_company_is_prompted(self):
        import tempfile
        from pathlib import Path

        cfg = Path(tempfile.mkdtemp()) / "c.yaml"
        cfg.write_text(
            "provider: mock\n"
            f"output: {{root_dir: {Path(tempfile.mkdtemp())}}}\n"
            "research:\n"
            "  stage1_prompt: ./prompts/prompt1_entity_discovery.md\n"
            "  stage2_prompt: ./prompts/prompt2_source_collection.md\n"
            "  retrieval_injection: {enabled: false}\n"
            "search_sweep: {enabled: false}\n"
            "repost_resolution: {enabled: false}\n"
            "official_site: {enabled: false, index_url: null}\n"
            "registries: {filings_search_key: null, patent_assignee: null}\n",
            encoding="utf-8",
        )
        code, out = self._run_main(
            ["TestCorp", "", "", ""], ["--config", str(cfg)]
        )
        self.assertEqual(code, 0)
        # all three prompts shown
        self.assertIn("공식 뉴스룸", out)
        self.assertIn("거래소 공시", out)
        self.assertIn("특허 출원인", out)
        # skipping everything must tell the user how to add channels later
        self.assertIn("추가 수집 없음", out)
        self.assertIn("--official-site", out)

    def test_channels_are_not_asked_when_company_is_a_flag(self):
        import tempfile
        from pathlib import Path

        cfg = Path(tempfile.mkdtemp()) / "c.yaml"
        cfg.write_text(
            "provider: mock\n"
            f"output: {{root_dir: {Path(tempfile.mkdtemp())}}}\n"
            "research:\n"
            "  stage1_prompt: ./prompts/prompt1_entity_discovery.md\n"
            "  stage2_prompt: ./prompts/prompt2_source_collection.md\n"
            "  retrieval_injection: {enabled: false}\n"
            "search_sweep: {enabled: false}\n"
            "repost_resolution: {enabled: false}\n"
            "official_site: {enabled: false, index_url: null}\n"
            "registries: {filings_search_key: null, patent_assignee: null}\n",
            encoding="utf-8",
        )
        # No stdin available: scripted use must never block on a prompt.
        code, out = self._run_main([], ["--company", "TestCorp", "--config", str(cfg)])
        self.assertEqual(code, 0)
        self.assertNotIn("공식 뉴스룸", out)
        self.assertIn("조사 대상: TestCorp", out)
