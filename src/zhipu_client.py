"""Z.ai (Zhipu / 智谱) API layer — the international-account provider.

Why this exists alongside the Tencent one: 联网搜索API is only sold on Tencent
Cloud's China site, whose real-name verification accepts a mainland Chinese ID
or a Chinese business licence and nothing else. Z.ai's international platform
takes an email registration and a Bearer API key, and exposes the two
capabilities this pipeline needs:

* ``POST /api/paas/v4/chat/completions`` with a built-in ``web_search`` tool.
  The response carries the generated text **and** a ``web_search`` array of
  results (title / content / link / media / publish_date) — structurally the
  same deal as Hunyuan's ``EnableEnhancement`` + ``SearchInfo``.
* ``POST /api/paas/v4/web_search`` for standalone structured search, with
  ``search_domain_filter`` standing in for SearchPro's ``Site`` parameter so
  queries can be aimed at ``mp.weixin.qq.com``.

Known limitation, stated plainly: the China-only platform (open.bigmodel.cn)
additionally offers a 搜狗 engine, which is WeChat's own search backend. The
international platform does not expose it, so WeChat-ecosystem coverage here is
whatever ``search-prime`` has indexed — better than a general western engine,
but not equal to Yuanbao. Measure it rather than assume it.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .config import ZhipuSettings
from .models import ResearchResponse
from .provider import (
    AuthError,
    RateLimitError,
    EmptyResponseError,
    MalformedResponseError,
    ProviderError,
    ResearchProvider,
    TimeoutError_,
    citation,
)
from .utils import get_logger

DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4"

# The free flash models are heavily contended and answer 429 with code 1305
# ("service temporarily overloaded"). That is transient, not a quota error, so
# it is worth retrying with backoff. Module-level so tests can zero it.
# 1305 = "service temporarily overloaded".
# 1113 = "insufficient balance" — normally fatal, but observed to be reported
# spuriously under contention on the free flash models and to clear on retry.
# Retried a bounded number of times so a genuine balance problem still fails.
# 1302 = per-account request rate limit. Transient like the others, but it
# needs a longer wait, so it gets its own, larger backoff base.
OVERLOADED_CODES = ("1305", "1113", "1302")
RATE_LIMIT_CODES = ("1302",)
# Measured: the free flash models return 1305/1113 non-deterministically under
# contention — identical requests succeed on retry, and request size is not the
# trigger (a 6,000-char prompt succeeded while a 512-token one failed). So retry
# generously; the failures are luck, not limits.
RETRY_BASE_SECONDS = 3.0
MAX_RETRIES = 7

# SearchPro-style freshness codes -> Z.ai's recency vocabulary, so one
# config value works across providers.
_RECENCY_MAP = {
    "d": "oneDay", "d1": "oneDay",
    "d7": "oneWeek", "w": "oneWeek", "w1": "oneWeek",
    "m": "oneMonth", "m1": "oneMonth", "m3": "oneMonth",
    "y": "oneYear", "y1": "oneYear", "y2": "oneYear",
}
_VALID_RECENCY = {"oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit"}


def _map_recency(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if value in _VALID_RECENCY:
        return value
    return _RECENCY_MAP.get(str(value).strip().lower())


class ZhipuClient:
    """Thin raw-JSON wrapper over the two Z.ai endpoints."""

    def __init__(self, settings: ZhipuSettings) -> None:
        if not settings.api_key:
            raise AuthError(
                "ZHIPU_API_KEY is not set.",
                hint="Get a key at https://z.ai/model-api, put it in .env, "
                     "or run with --provider mock to test offline.",
            )
        self.settings = settings
        self.log = get_logger()
        try:
            import requests  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise ProviderError(
                f"the requests library is not installed ({exc}).",
                hint="Run: pip install -r requirements.txt",
            ) from exc

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import requests

        url = f"{self.settings.base_url.rstrip('/')}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.log.debug("POST %s keys=%s", url, sorted(payload))

        last_error: Optional[ProviderError] = None
        self._backoff_base = RETRY_BASE_SECONDS
        for attempt in range(MAX_RETRIES):
            if attempt and RETRY_BASE_SECONDS:
                import time as _time

                wait = RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                self.log.info("retrying %s in %.0fs (attempt %d)", path, wait, attempt + 1)
                _time.sleep(wait)
            try:
                response = requests.post(
                    url,
                    headers=headers,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=self.settings.timeout_seconds,
                )
            except requests.exceptions.Timeout as exc:
                raise TimeoutError_(
                    f"{path} timed out after {self.settings.timeout_seconds}s"
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise ProviderError(f"{path} request failed: {exc}") from exc

            if response.status_code < 400:
                break
            error = self._http_error(response, path)
            if not self._is_transient(response):
                raise error
            # A rate limit needs real time to clear, unlike a load spike.
            try:
                code = str(((response.json() or {}).get("error") or {}).get("code") or "")
            except (ValueError, AttributeError):
                code = ""
            if code in RATE_LIMIT_CODES:
                self._backoff_base = max(self._backoff_base, 15.0)
            last_error = error
        else:
            raise RateLimitError(
                f"{path} still overloaded after {MAX_RETRIES} attempts: {last_error}",
                hint="The free flash models are heavily contended (codes 1305/1113). "
                     "If this persists, check your balance at "
                     "https://z.ai/manage-apikey/billing, or set zhipu.model to a paid "
                     "model such as glm-4.5-air in config.yaml.",
            )

        try:
            body = response.json()
        except ValueError as exc:
            snippet = response.text[:200]
            raise MalformedResponseError(
                f"{path} returned non-JSON content: {snippet!r}"
            ) from exc

        if not isinstance(body, dict):
            raise MalformedResponseError(f"{path} returned a {type(body).__name__}, expected an object")
        return body

    @staticmethod
    def _is_transient(response: Any) -> bool:
        """429 with an overload code is contention, not a quota breach."""
        if response.status_code not in (429, 500, 502, 503, 504):
            return False
        try:
            code = str(((response.json() or {}).get("error") or {}).get("code") or "")
        except (ValueError, AttributeError):
            return response.status_code >= 500
        return code in OVERLOADED_CODES or response.status_code >= 500

    @staticmethod
    def _http_error(response: Any, path: str) -> ProviderError:
        detail = response.text[:300]
        try:
            payload = response.json()
            error = payload.get("error") or {}
            detail = error.get("message") or payload.get("message") or detail
            code = error.get("code") or ""
        except (ValueError, AttributeError):
            code = ""

        message = f"{path} failed [HTTP {response.status_code}{f' {code}' if code else ''}]: {detail}"
        if response.status_code in (401, 403):
            return AuthError(
                message,
                hint="Check ZHIPU_API_KEY in .env. Create or rotate a key at "
                     "https://z.ai/model-api",
            )
        if response.status_code == 429:
            return RateLimitError(message)
        if response.status_code == 408 or response.status_code == 504:
            return TimeoutError_(message)
        return ProviderError(message)

    # -- chat completions with the built-in web_search tool ---------------
    def chat_completions(self, prompt: str) -> dict[str, Any]:
        s = self.settings
        payload_tools: list[dict[str, Any]] = []
        web_search: dict[str, Any] = {
            "enable": True,
            "search_engine": s.search_engine,
            # Return the raw search results next to the answer so evidence is
            # preserved, not just cited.
            "search_result": True,
            "content_size": s.content_size,
            "require_search": s.require_search,
            "count": s.search_count,
        }
        recency = _map_recency(s.search_recency)
        if recency:
            web_search["search_recency_filter"] = recency

        # The built-in web_search tool is a paid add-on ($0.01/use) and is
        # refused on a zero-balance account. Off by default: the pipeline
        # injects retrieval it controls (see research.retrieval_injection),
        # which is both free and auditable.
        if s.use_builtin_search:
            payload_tools.append({"type": "web_search", "web_search": web_search})

        payload: dict[str, Any] = {
            "model": s.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "temperature": s.temperature,
        }
        if payload_tools:
            payload["tools"] = payload_tools
        if s.max_tokens:
            payload["max_tokens"] = int(s.max_tokens)
        # Always explicit. glm-4.x flash models default to thinking, and a
        # modest max_tokens is then consumed entirely by reasoning_tokens,
        # returning empty content. Measured: 16/16 tokens, content ''.
        payload["thinking"] = {"type": "enabled" if s.thinking else "disabled"}
        return self._post("chat/completions", payload)

    # -- standalone structured search -------------------------------------
    def web_search(
        self,
        query: str,
        *,
        count: int = 20,
        domain_filter: Optional[str] = None,
        recency: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "search_engine": self.settings.search_api_engine,
            "search_query": query,
            "count": max(1, min(int(count), 50)),
            "content_size": self.settings.content_size,
        }
        if domain_filter:
            payload["search_domain_filter"] = domain_filter
        mapped = _map_recency(recency or self.settings.search_recency)
        if mapped:
            payload["search_recency_filter"] = mapped
        return self._post("web_search", payload)


def _to_citation(item: dict[str, Any], *, index: Optional[int] = None) -> dict[str, Any]:
    """Map one Z.ai search object onto the shared citation/page shape."""
    return citation(
        title=item.get("title"),
        url=item.get("link") or item.get("url"),
        site=item.get("media"),
        icon=item.get("icon"),
        index=item.get("refer") if item.get("refer") is not None else index,
        # `content` is Z.ai's summary of the page, not its body text. The
        # pipeline therefore labels these SEARCH_SNIPPET_ONLY, never full text.
        content=item.get("content"),
        publication_date=item.get("publish_date"),
        raw=item,
    )


def extract_search_items(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the search array out of either endpoint's response shape.

    Z.ai has used both ``web_search`` and ``search_result`` as the key across
    its endpoints and revisions, so accept either rather than silently
    returning nothing when the field is renamed.
    """
    for key in ("web_search", "search_result", "search_results", "results"):
        value = body.get(key)
        if isinstance(value, list) and value:
            return [v for v in value if isinstance(v, dict)]
    return []


class ZhipuProvider(ResearchProvider):
    name = "zhipu"

    def __init__(self, settings: ZhipuSettings) -> None:
        self.settings = settings
        self.client = ZhipuClient(settings)
        self.log = get_logger()

    @property
    def supports_search(self) -> bool:
        return True

    def run_research(self, prompt: str, *, label: str = "") -> ResearchResponse:
        raw = self.client.chat_completions(prompt)

        choices = raw.get("choices") or []
        if not isinstance(choices, list) or not choices:
            raise EmptyResponseError(
                f"Z.ai returned no choices for {label or 'request'} "
                f"(id {raw.get('id')})."
            )

        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") or {}
        text = (message.get("content") or "").strip()
        reasoning = (message.get("reasoning_content") or "").strip()
        finish_reason = first.get("finish_reason")

        if not text:
            usage = raw.get("usage") or {}
            detail = (usage.get("completion_tokens_details") or {})
            if reasoning or detail.get("reasoning_tokens"):
                raise EmptyResponseError(
                    f"Z.ai spent the whole completion budget on reasoning and returned no "
                    f"answer for {label or 'request'} "
                    f"(reasoning_tokens={detail.get('reasoning_tokens')}, "
                    f"max_tokens={self.settings.max_tokens}). "
                    "Raise zhipu.max_tokens, or set zhipu.thinking: false."
                )
            raise EmptyResponseError(
                f"Z.ai returned empty content for {label or 'request'} "
                f"(finish_reason={finish_reason!r}, id={raw.get('id')})."
            )

        items = extract_search_items(raw)
        results = [_to_citation(item, index=i) for i, item in enumerate(items, start=1)]

        warnings: list[str] = []
        if self.settings.use_builtin_search and not results:
            warnings.append(
                "The web_search tool was enabled but the response carried no search "
                "results — the model may have answered from its own knowledge. Treat "
                "unsourced claims in this output with care."
            )
        if finish_reason == "length":
            warnings.append(
                "finish_reason=length — the output was cut off. Raise zhipu.max_tokens."
            )
        elif finish_reason not in (None, "stop", "tool_calls"):
            warnings.append(f"finish_reason={finish_reason!r} — output may be incomplete.")
        if reasoning:
            warnings.append("reasoning_content was present and is preserved in the raw response.")

        return ResearchResponse(
            text=text,
            raw=raw,
            provider=self.name,
            model=raw.get("model") or self.settings.model,
            search_results=results,
            usage=raw.get("usage"),
            request_id=raw.get("id"),
            finish_reason=finish_reason,
            warnings=warnings,
        )

    def search(self, query, *, count=20, site=None, industry=None, freshness=None, mode=2):
        if industry:
            # No vertical-search equivalent; say so instead of pretending.
            self.log.debug("zhipu has no industry filter — ignoring industry=%r", industry)
        raw = self.client.web_search(
            query, count=count, domain_filter=site, recency=freshness
        )
        items = extract_search_items(raw)
        return {
            "query": query,
            "pages": [_to_citation(item, index=i) for i, item in enumerate(items, start=1)],
            "raw": raw,
            "supported": True,
            "site": site,
            "industry": None,
            "freshness": _map_recency(freshness or self.settings.search_recency),
            "request_id": raw.get("id"),
        }

    def describe(self):
        return {
            "provider": self.name,
            "model": self.settings.model,
            "endpoints": {
                "inference": f"{self.settings.base_url}/chat/completions",
                "search": f"{self.settings.base_url}/web_search",
            },
            "search_flags": {
                "chat_search_engine": self.settings.search_engine,
                "search_api_engine": self.settings.search_api_engine,
                "content_size": self.settings.content_size,
                "require_search": self.settings.require_search,
                "recency": _map_recency(self.settings.search_recency),
            },
            "limitation": (
                "International platform does not expose the 搜狗 (WeChat) engine; "
                "WeChat coverage is limited to what search-prime has indexed."
            ),
        }
