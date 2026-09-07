"""Provider abstraction.

The pipeline only ever talks to :class:`ResearchProvider`. Swapping Tencent for
another vendor means adding one subclass — no pipeline changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from .models import ResearchResponse


class ProviderError(RuntimeError):
    """Base class for provider failures the CLI knows how to explain."""

    hint: str = ""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        if hint:
            self.hint = hint


class AuthError(ProviderError):
    # Providers override this with their own variable names.
    hint = "Check your provider credentials in .env."


class RateLimitError(ProviderError):
    hint = ("Rate limited by the provider. Wait and re-run; a completed stage 1 "
            "on disk is reusable via --resume, so it is not lost.")


class TimeoutError_(ProviderError):
    hint = ("Increase the provider's timeout_seconds in config.yaml, or re-run the "
            "failed stage with --resume.")


class EmptyResponseError(ProviderError):
    hint = ("The API returned no text. Usually the model refused the prompt or the "
            "content filter blocked it. The raw payload was still saved.")


class MalformedResponseError(ProviderError):
    hint = "The API response did not match the documented shape. The raw payload was still saved for inspection."


class ResearchProvider(ABC):
    """Runs one research prompt and returns text plus any structured sources.

    Implementations MUST be stateless: every call carries its full context in
    ``prompt``. The pipeline never relies on server-side conversation memory.

    Two normalised shapes let the pipeline stay provider-agnostic. Providers
    translate their own payloads into these; the untouched original is always
    kept alongside (in ``ResearchResponse.raw`` and in each item's ``_raw``).

    A **citation** (``ResearchResponse.search_results``) is a dict with keys:
    ``title``, ``url``, ``site``, ``icon``, ``index``, ``content``,
    ``publication_date``. Missing values must be ``None`` — never invented.
    ``content`` is a search summary when the provider supplies one, and
    ``None`` when it only returns a link; the pipeline labels the record
    accordingly, so do not fill it with anything you did not receive.

    A **page** (``search()['pages']``) uses the same keys plus ``score`` and
    any provider extras.
    """

    name: str = "abstract"

    @abstractmethod
    def run_research(self, prompt: str, *, label: str = "") -> ResearchResponse:
        """Execute ``prompt`` as a single self-contained research request."""

    def search(
        self,
        query: str,
        *,
        count: int = 20,
        site: Optional[str] = None,
        industry: Optional[str] = None,
        freshness: Optional[str] = None,
        mode: int = 2,
    ) -> dict[str, Any]:
        """Optional structured web search. Providers without one return empty."""
        return {"query": query, "pages": [], "raw": None, "supported": False}

    @property
    def supports_search(self) -> bool:
        return False

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name}


def citation(
    *,
    title: Any = None,
    url: Any = None,
    site: Any = None,
    icon: Any = None,
    index: Any = None,
    content: Any = None,
    publication_date: Any = None,
    raw: Any = None,
) -> dict[str, Any]:
    """Build a normalised citation/page dict. See :class:`ResearchProvider`."""

    def clean(value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    return {
        "title": clean(title),
        "url": clean(url),
        "site": clean(site),
        "icon": clean(icon),
        "index": index,
        "content": clean(content),
        "publication_date": clean(publication_date),
        "_raw": raw,
    }


class MockProvider(ResearchProvider):
    """Offline provider for testing the pipeline without spending API calls.

    It emits output shaped like the real thing — including prompt-2 style
    ``SOURCE_ID:`` blocks — so persistence and parsing are exercised fully.
    """

    name = "mock"

    def __init__(self, model: str = "mock-model-v1") -> None:
        self.model = model
        self.calls: list[str] = []

    @property
    def supports_search(self) -> bool:
        return True

    def run_research(self, prompt: str, *, label: str = "") -> ResearchResponse:
        self.calls.append(label or "unlabelled")
        is_stage2 = "<STAGE_1_RESEARCH>" in prompt
        text = _MOCK_STAGE2 if is_stage2 else _MOCK_STAGE1
        raw = {
            "Response": {
                "Note": "synthetic payload from MockProvider",
                "Model": self.model,
                "PromptChars": len(prompt),
                "Choices": [{"Message": {"Role": "assistant", "Content": text}, "FinishReason": "stop"}],
                "SearchInfo": {"SearchResults": _MOCK_CITATIONS},
                "Usage": {"PromptTokens": len(prompt) // 4, "CompletionTokens": len(text) // 4,
                          "TotalTokens": (len(prompt) + len(text)) // 4},
                "RequestId": f"mock-{label or 'run'}-0001",
            }
        }
        return ResearchResponse(
            text=text,
            raw=raw,
            provider=self.name,
            model=self.model,
            search_results=[
                citation(
                    title=c["Title"], url=c["Url"], site=c["Text"],
                    icon=c["Icon"], index=c["Index"], raw=c,
                )
                for c in _MOCK_CITATIONS
            ],
            usage=raw["Response"]["Usage"],
            request_id=raw["Response"]["RequestId"],
            finish_reason="stop",
            warnings=["MockProvider output is synthetic — not real research data."],
        )

    def search(self, query, *, count=20, site=None, industry=None, freshness=None, mode=2):
        pages = [
            {
                **citation(
                    title=f"【模拟】{query} 相关文章 {i}",
                    url=f"https://mp.weixin.qq.com/s/MOCK_{abs(hash((query, i))) % 10**8}",
                    content=f"这是针对查询「{query}」的模拟摘要，用于测试保存流程。峰值功率12kW。",
                    site=site or "模拟站点",
                    publication_date="2026-01-15",
                    index=i,
                ),
                "score": 0.9 - i * 0.1,
            }
            for i in range(1, min(count, 3) + 1)
        ]
        return {
            "query": query,
            "pages": pages,
            "raw": {"Response": {"Query": query, "Pages": [], "Note": "mock", "RequestId": "mock-wsa"}},
            "supported": True,
            "site": site,
            "industry": industry,
        }

    def describe(self):
        return {
            "provider": self.name,
            "model": self.model,
            "endpoints": {"inference": "mock://chat", "search": "mock://search"},
            "note": "synthetic offline provider",
        }


_MOCK_CITATIONS = [
    {
        "Index": 1,
        "Title": "【模拟】公司官方公众号文章",
        "Url": "https://mp.weixin.qq.com/s/MOCK_STABLE_ID",
        "Text": "公司官方公众号",
        "Icon": "",
    },
    {
        "Index": 2,
        "Title": "【模拟】搜狗微信临时链接",
        "Url": "https://mp.weixin.qq.com/s?src=11&timestamp=1750000000&signature=mock",
        "Text": "搜狗微信",
        "Icon": "",
    },
]


_MOCK_STAGE1 = """## A. Canonical Company Identity

- 英文名称: MockCorp
- 中文正式名称: 模拟科技有限公司
- 说明: 这是 MockProvider 生成的模拟数据，仅用于测试管线。

## B. Discovered Entity Types

| Entity Type | Why It Matters | Example |
| --- | --- | --- |
| 法律实体 | 工商与招投标检索 | 模拟科技有限公司 |
| Robot Model | 产品线检索锚点 | MockBot A1 |

## C. Entity / Alias Dictionary

| Name / Alias | Entity Type | Canonical Entity | Relationship | Evidence | URL | Confidence |
| --- | --- | --- | --- | --- | --- | --- |
| 模拟科技 | 中文简称 | MockCorp | same entity | 官网 | https://example.com | High |
| MockBot A1 | Robot Model | MockCorp | product | 公众号 | https://mp.weixin.qq.com/s/MOCK | Medium |

## D. High-Value China Source Map

| Source | Source Type | Why Valuable | Information Available | Relevant Entity / Alias | URL |
| --- | --- | --- | --- | --- | --- |
| 模拟科技官方公众号 | WeChat Official Account | 一手产品发布 | 产品参数 | MockCorp | https://mp.weixin.qq.com/s/MOCK |

## E. Recommended Search Queries

1. 模拟科技 融资
2. 模拟科技有限公司 工商
3. MockBot A1 参数
4. 模拟科技 创始人 专访
5. MockCorp 论文
6. 模拟科技 量产 出货
7. 模拟科技 招聘 具身智能
8. 模拟科技 合作伙伴

## F. Ambiguous / Unresolved Entities

- 「模拟机器人」名称冲突，Unverified
"""

_MOCK_STAGE2 = """---

SOURCE_ID: SOURCE_001
TARGET_COMPANY: MockCorp
SOURCE_PLATFORM: WeChat Official Account
SOURCE_TYPE: 产品发布
TITLE: 【模拟】MockBot A1 正式发布
PUBLISHER / ACCOUNT: 模拟科技
AUTHOR: null
PUBLICATION_DATE: 2026-01-15
MATCHED_ENTITY: MockCorp
MATCHED_ALIAS: 模拟科技
DISCOVERY_QUERY: MockBot A1 参数
RETRIEVAL_URL: https://mp.weixin.qq.com/s?src=11&timestamp=1750000000&signature=mock
CANONICAL_URL: https://mp.weixin.qq.com/s/MOCK_STABLE_ID
URL_TYPE: STABLE_WECHAT_ARTICLE_URL
REACCESS_STATUS: NOT_TESTED
CONTENT_ACCESS_STATUS: VERBATIM_PARTIAL_TEXT

SOURCE_CONTENT:

2026年1月15日，模拟科技正式发布 MockBot A1。整机自由度 42，峰值功率12kW，
续航 4 小时。公司表示 2025年累计出货5100台。

---

SOURCE_ID: SOURCE_002
TARGET_COMPANY: MockCorp
SOURCE_PLATFORM: 视频号
SOURCE_TYPE: 高管访谈
TITLE: 【模拟】创始人谈具身智能路线
PUBLISHER / ACCOUNT: 模拟科技视频号
PUBLICATION_DATE: 2026-02-02
RETRIEVAL_URL: https://channels.weixin.qq.com/mock
CANONICAL_URL: null
URL_TYPE: STABLE_VIDEO_URL
REACCESS_STATUS: NOT_TESTED
CONTENT_ACCESS_STATUS: TRANSCRIPT_EXTRACTED
TRANSCRIPT_METHOD: auto-caption

SOURCE_CONTENT:

SPEAKER: 张模拟
TITLE: 创始人兼CEO
QUOTE: 「我们认为数据闭环是唯一的护城河。」

---

NEW_SEARCH_ANCHOR:
ENTITY_TYPE: Dataset
NAME: MockData-1M
SOURCE_ID: SOURCE_001

---

**Final Collection Summary**

TOTAL_SOURCES_DISCOVERED: 2
VERBATIM_FULL_TEXT: 0
VERBATIM_PARTIAL_TEXT: 1
TRANSCRIPT_EXTRACTED: 1
HIGH_FIDELITY_EXTRACTION: 0
SEARCH_SNIPPET_ONLY: 0
URL_ONLY: 0
STABLE_CANONICAL_URL_FOUND: 1
TEMPORARY_URL_ONLY: 0
NOT_REOPENABLE: 0
NEW_SEARCH_ANCHORS: 1
REMAINING_SOURCE_GAPS: 原始视频号无法取得完整 transcript
"""


def build_provider(name: str, config: Any) -> ResearchProvider:
    """Instantiate a provider by name. Imports are local to avoid cycles."""
    key = (name or "").strip().lower()

    if key == "serpapi":
        from .serpapi_client import SerpApiBaiduProvider

        return SerpApiBaiduProvider(config.serpapi)
    if key == "mock":
        return MockProvider()

    raise ProviderError(
        f"Unknown provider {name!r}.",
        hint="Supported providers: claude-cli, serpapi, mock.",
    )
