"""Tencent Cloud API layer — the only Tencent-specific module.

Two endpoints are used:

* ``hunyuan.tencentcloudapi.com`` / ``ChatCompletions`` (Version 2023-09-01)
  runs the research prompts. ``EnableEnhancement`` + ``ForceSearchEnhancement``
  switch on Tencent's AI-search plugin; ``Citation`` and ``SearchInfo`` make the
  response carry a structured ``SearchInfo.SearchResults`` citation list next to
  the generated text. The endpoint is **stateless** — it has no conversation
  memory, which is exactly why the pipeline passes stage 1 forward explicitly.

* ``wsa.tencentcloudapi.com`` / ``SearchPro`` (Version 2025-05-08) is 联网搜索API,
  documented by Tencent as built on the Yuanbao (元宝) App search stack. It
  returns structured Chinese web results and its ``Site`` filter reaches
  ``mp.weixin.qq.com`` (WeChat Official Account articles).

Transport is the official SDK's own ``AbstractClient.call_json``, which does
TC3-HMAC-SHA256 signing and retries but hands back **raw JSON dicts** — nothing
is normalised away before we persist it.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .config import TencentSettings
from .models import ResearchResponse
from .provider import (
    AuthError,
    citation,
    EmptyResponseError,
    MalformedResponseError,
    ProviderError,
    RateLimitError,
    ResearchProvider,
    TimeoutError_,
)
from .utils import get_logger

HUNYUAN_ENDPOINT = "hunyuan.tencentcloudapi.com"
HUNYUAN_SERVICE = "hunyuan"
HUNYUAN_VERSION = "2023-09-01"

WSA_ENDPOINT = "wsa.tencentcloudapi.com"
WSA_SERVICE = "wsa"
WSA_VERSION = "2025-05-08"

_AUTH_CODES = ("AuthFailure", "UnauthorizedOperation", "InvalidCredential")
_RATE_CODES = ("RequestLimitExceeded", "LimitExceeded", "ResourceUnavailable", "TooManyRequests")
_TIMEOUT_MARKERS = ("timeout", "timed out", "ClientNetworkError", "Read timed out")


def _import_sdk():
    try:
        from tencentcloud.common import credential
        from tencentcloud.common.abstract_client import AbstractClient
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
            TencentCloudSDKException,
        )
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile
    except ImportError as exc:  # pragma: no cover - environment problem
        raise ProviderError(
            f"Tencent Cloud SDK is not installed ({exc}).",
            hint="Run: pip install -r requirements.txt",
        ) from exc
    return credential, AbstractClient, TencentCloudSDKException, ClientProfile, HttpProfile


class TencentClient:
    """Thin raw-JSON wrapper over the two Tencent endpoints."""

    def __init__(self, settings: TencentSettings) -> None:
        if not settings.has_credentials:
            raise AuthError(
                "TENCENT_SECRET_ID / TENCENT_SECRET_KEY are not set.",
                hint="cp .env.example .env and fill in your Tencent Cloud keys, "
                     "or run with --provider mock to test the pipeline offline.",
            )
        self.settings = settings
        self.log = get_logger()

        cred_mod, abstract_client, sdk_exc, client_profile, http_profile = _import_sdk()
        self._sdk_exception = sdk_exc

        http = http_profile()
        http.reqTimeout = settings.timeout_seconds
        profile = client_profile()
        profile.httpProfile = http
        profile.signMethod = "TC3-HMAC-SHA256"

        cred = cred_mod.Credential(settings.secret_id, settings.secret_key)

        class _Generic(abstract_client):
            _apiVersion = ""
            _endpoint = ""
            _service = ""

        def _make(service: str, endpoint: str, version: str):
            client = _Generic(cred, settings.region, profile)
            client._service = service
            client._endpoint = endpoint
            client._apiVersion = version
            return client

        self._hunyuan = _make(HUNYUAN_SERVICE, HUNYUAN_ENDPOINT, HUNYUAN_VERSION)
        self._wsa = _make(WSA_SERVICE, WSA_ENDPOINT, WSA_VERSION)

    # -- error translation ------------------------------------------------
    def _translate(self, exc: Exception, action: str) -> ProviderError:
        code = getattr(exc, "code", "") or ""
        message = getattr(exc, "message", "") or str(exc)
        request_id = getattr(exc, "requestId", None)
        detail = f"{action} failed [{code or type(exc).__name__}]: {message}"
        if request_id:
            detail += f" (RequestId {request_id})"

        if any(c in code for c in _AUTH_CODES):
            return AuthError(
                detail,
                hint="Check TENCENT_SECRET_ID / TENCENT_SECRET_KEY in .env. Also confirm "
                     "Hunyuan and 联网搜索API are activated in the console, and that the "
                     "key has not been auto-disabled after 90 days unused.",
            )
        if any(c in code for c in _RATE_CODES):
            return RateLimitError(detail)
        if any(m.lower() in (code + " " + message).lower() for m in _TIMEOUT_MARKERS):
            return TimeoutError_(detail)
        return ProviderError(detail)

    def _call(self, client, action: str, params: dict[str, Any]) -> dict[str, Any]:
        self.log.debug("calling %s with keys=%s", action, sorted(params))
        try:
            payload = client.call_json(action, params)
        except self._sdk_exception as exc:
            raise self._translate(exc, action) from exc
        except json.JSONDecodeError as exc:
            raise MalformedResponseError(f"{action} returned non-JSON content: {exc}") from exc
        except Exception as exc:  # network layer, SSL, DNS, ...
            raise self._translate(exc, action) from exc

        if not isinstance(payload, dict) or "Response" not in payload:
            raise MalformedResponseError(
                f"{action} response missing the documented 'Response' envelope."
            )
        self.log.debug("%s ok (RequestId=%s)", action, payload["Response"].get("RequestId"))
        return payload

    # -- Hunyuan ChatCompletions -----------------------------------------
    def chat_completions(self, prompt: str) -> dict[str, Any]:
        s = self.settings
        params: dict[str, Any] = {
            "Model": s.model,
            "Messages": [{"Role": "user", "Content": prompt}],
            "Stream": False,
            "Temperature": s.temperature,
            "EnableEnhancement": s.enable_enhancement,
            "ForceSearchEnhancement": s.force_search_enhancement,
            "Citation": s.citation,
            "SearchInfo": s.search_info,
            "EnableMultimedia": s.enable_multimedia,
        }
        if s.enable_speed_search:
            params["EnableSpeedSearch"] = True
        return self._call(self._hunyuan, "ChatCompletions", params)

    # -- WSA SearchPro ----------------------------------------------------
    def search_pro(
        self,
        query: str,
        *,
        count: int = 20,
        site: Optional[str] = None,
        industry: Optional[str] = None,
        freshness: Optional[str] = None,
        mode: int = 2,
        deeplinks: bool = False,
    ) -> dict[str, Any]:
        # Cnt only accepts 10/20/30/40/50.
        allowed = (10, 20, 30, 40, 50)
        cnt = min(allowed, key=lambda v: abs(v - int(count)))
        params: dict[str, Any] = {"Query": query, "Mode": int(mode), "Cnt": cnt}
        if site:
            params["Site"] = site
        if industry:
            params["Industry"] = industry
        if freshness:
            params["Freshness"] = freshness
        if deeplinks:
            params["Deeplinks"] = True
        return self._call(self._wsa, "SearchPro", params)


def parse_pages(response: dict[str, Any]) -> list[dict[str, Any]]:
    """``SearchPro`` returns ``Pages`` as an array of JSON *strings*.

    Anything that fails to parse is kept verbatim under ``_unparsed`` rather
    than dropped — losing evidence is worse than storing an odd record.
    """
    pages_raw = (response.get("Response") or {}).get("Pages") or []
    out: list[dict[str, Any]] = []
    for item in pages_raw:
        if isinstance(item, dict):
            out.append(item)
            continue
        try:
            parsed = json.loads(item)
            out.append(parsed if isinstance(parsed, dict) else {"_unparsed": item})
        except (TypeError, ValueError):
            out.append({"_unparsed": item})
    return out


def _normalise_page(page: dict[str, Any]) -> dict[str, Any]:
    """Map a ``SearchPro`` page onto the shared page shape.

    ``passage`` is the standard summary and ``content`` the dynamic summary
    (premium tiers only). Both are summaries, so they become ``content`` on the
    normalised record and the pipeline never labels them as full text.
    """
    passage = page.get("passage") or None
    dynamic = page.get("content") or None
    parts = []
    if passage:
        parts.append(f"[passage]\n{passage}")
    if dynamic and dynamic != passage:
        parts.append(f"[content]\n{dynamic}")

    return {
        **citation(
            title=page.get("title"),
            url=page.get("url"),
            site=page.get("site"),
            icon=page.get("favicon"),
            content="\n\n".join(parts) or None,
            publication_date=str(page.get("date")) if page.get("date") else None,
            raw=page,
        ),
        "score": page.get("score"),
        "authority_level": page.get("authority_level"),
        "images": page.get("pics"),
        "deeplinks": page.get("deeplinks"),
    }


class TencentProvider(ResearchProvider):
    """:class:`ResearchProvider` implementation backed by Tencent Cloud."""

    name = "tencent"

    def __init__(self, settings: TencentSettings) -> None:
        self.settings = settings
        self.client = TencentClient(settings)
        self.log = get_logger()

    @property
    def supports_search(self) -> bool:
        return True

    def run_research(self, prompt: str, *, label: str = "") -> ResearchResponse:
        raw = self.client.chat_completions(prompt)
        body = raw.get("Response", {})

        choices = body.get("Choices") or []
        if not isinstance(choices, list) or not choices:
            raise EmptyResponseError(
                f"Hunyuan returned no Choices for {label or 'request'} "
                f"(RequestId {body.get('RequestId')})."
            )

        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("Message") or {}
        text = (message.get("Content") or "").strip()
        reasoning = (message.get("ReasoningContent") or "").strip()
        finish_reason = first.get("FinishReason")

        if not text:
            if finish_reason == "sensitive":
                raise EmptyResponseError(
                    f"Hunyuan returned empty content with FinishReason=sensitive for "
                    f"{label or 'request'} — the content safety filter blocked the answer."
                )
            raise EmptyResponseError(
                f"Hunyuan returned empty content for {label or 'request'} "
                f"(FinishReason={finish_reason!r}, RequestId={body.get('RequestId')})."
            )

        search_info = body.get("SearchInfo") or {}
        results = search_info.get("SearchResults") or []
        if not isinstance(results, list):
            results = []
        # Hunyuan citations are title/url/site only — never body text — so
        # `content` stays None and the pipeline labels them URL_ONLY.
        results = [
            citation(
                title=c.get("Title"),
                url=c.get("Url"),
                site=c.get("Text"),
                icon=c.get("Icon"),
                index=c.get("Index"),
                raw=c,
            )
            for c in results
            if isinstance(c, dict)
        ]

        warnings: list[str] = []
        if self.settings.search_info and not results:
            warnings.append(
                "SearchInfo was requested but the response carried no SearchResults — "
                "search may not have been triggered for this prompt."
            )
        if finish_reason not in (None, "stop"):
            warnings.append(f"FinishReason={finish_reason!r} — output may be incomplete.")
        if reasoning:
            warnings.append("ReasoningContent was present and is preserved in the raw response.")

        return ResearchResponse(
            text=text,
            raw=raw,
            provider=self.name,
            model=self.settings.model,
            search_results=results,
            usage=body.get("Usage"),
            request_id=body.get("RequestId"),
            finish_reason=finish_reason,
            warnings=warnings,
        )

    def search(self, query, *, count=20, site=None, industry=None, freshness=None, mode=2):
        raw = self.client.search_pro(
            query, count=count, site=site, industry=industry, freshness=freshness, mode=mode
        )
        return {
            "query": query,
            "pages": [_normalise_page(p) for p in parse_pages(raw)],
            "raw": raw,
            "supported": True,
            "site": site,
            "industry": industry,
            "freshness": freshness,
            "request_id": raw.get("Response", {}).get("RequestId"),
            "api_version_tier": raw.get("Response", {}).get("Version"),
        }

    def describe(self):
        return {
            "provider": self.name,
            "model": self.settings.model,
            "region": self.settings.region,
            "endpoints": {
                "inference": f"{HUNYUAN_ENDPOINT}/ChatCompletions@{HUNYUAN_VERSION}",
                "search": f"{WSA_ENDPOINT}/SearchPro@{WSA_VERSION}",
            },
            "search_flags": {
                "EnableEnhancement": self.settings.enable_enhancement,
                "ForceSearchEnhancement": self.settings.force_search_enhancement,
                "Citation": self.settings.citation,
                "SearchInfo": self.settings.search_info,
                "EnableMultimedia": self.settings.enable_multimedia,
                "EnableSpeedSearch": self.settings.enable_speed_search,
            },
        }
