"""SerpApi provider — programmatic access to the Baidu index.

Why this exists: measured on AgiBot, 4 Baidu queries returned 49 domains of
which **39 (79%) never appeared** in a Claude-web-search corpus for the same
company. The exclusive layer is not marginal — it includes 百家号
(baijiahao.baidu.com, Baidu's own content ecosystem), 中国政府采购网
(ccgp.gov.cn, legally-mandated procurement disclosure), 爱企查/天眼查 company
registries, Baidu B2B supply-chain listings, investor forums (雪球, 股吧) and
regional government sites. Western indexes reach the tier-1 media layer
(新浪, 澎湃, 财联社, 界面) and largely stop there.

Note on what this is: Baidu retired its official search API, so there is no
first-party endpoint. SerpApi runs the query against Baidu and parses the
result page. That is a vendor relationship, not an official API — see README.

Search only. This provider has no chat model, so it cannot run the research
prompts; pair it with another provider for stages 1-2 and use it for the
structured search sweep.
"""

from __future__ import annotations

from typing import Any, Optional

from .config import SerpApiSettings
from .models import ResearchResponse
from .provider import (
    AuthError,
    MalformedResponseError,
    ProviderError,
    RateLimitError,
    ResearchProvider,
    TimeoutError_,
    citation,
)
from .utils import get_logger

SERPAPI_ENDPOINT = "https://serpapi.com/search"

# Baidu's `ct` parameter: 1 = all languages, 2 = Simplified, 3 = Traditional.
CT_SIMPLIFIED = "2"

# SearchPro-style freshness codes -> approximate day windows for Baidu's `gpc`.
_FRESHNESS_DAYS = {
    "d": 1, "d1": 1, "d7": 7, "w": 7, "w1": 7,
    "m": 30, "m1": 30, "m3": 90, "y": 365, "y1": 365, "y2": 730,
}


# SerpApi answers an exhausted monthly quota with 429. Distinguishing that
# from a bad key matters: one means "rotate", the other means "stop".
_QUOTA_MARKERS = ("run out of searches", "exceeded", "quota", "plan limit")


class SerpApiClient:
    """Rotates through the configured keys as each one's quota runs out."""

    def __init__(self, settings: SerpApiSettings) -> None:
        if not settings.has_credentials:
            raise AuthError(
                "no SerpApi key configured.",
                hint="Set SERPAPI_KEY in .env (get one at "
                     "https://serpapi.com/manage-api-key), or run with "
                     "--provider mock to test offline.",
            )
        self.settings = settings
        self.log = get_logger()
        self._keys = list(settings.api_keys)
        self._index = 0
        self._exhausted: set[int] = set()

    @property
    def active_key_number(self) -> int:
        """1-based, for logs. Never log the key itself."""
        return self._index + 1

    def _advance(self, reason: str) -> bool:
        """Mark the current key done and move to the next. False if none left."""
        self._exhausted.add(self._index)
        for candidate in range(len(self._keys)):
            if candidate not in self._exhausted:
                previous = self.active_key_number
                self._index = candidate
                self.log.warning(
                    "SerpApi key %d %s — switching to key %d of %d",
                    previous, reason, self.active_key_number, len(self._keys),
                )
                return True
        return False

    def search(self, params: dict[str, Any]) -> dict[str, Any]:
        import requests

        self.log.debug("serpapi search: %s", {k: v for k, v in params.items()})

        # One attempt per remaining key: a quota error rotates, anything else stops.
        for _ in range(len(self._keys)):
            payload = {**params, "api_key": self._keys[self._index]}
            try:
                response = requests.get(
                    SERPAPI_ENDPOINT, params=payload,
                    timeout=self.settings.timeout_seconds,
                )
            except requests.exceptions.Timeout as exc:
                raise TimeoutError_(
                    f"SerpApi timed out after {self.settings.timeout_seconds}s. Baidu "
                    "queries routinely take 15-30s; raise serpapi.timeout_seconds."
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise ProviderError(f"SerpApi request failed: {exc}") from exc

            if response.status_code == 401:
                # A rejected key is a configuration error, not exhaustion. Try
                # the next one rather than stopping the whole run on a typo.
                if self._advance("was rejected (HTTP 401)"):
                    continue
                raise AuthError(
                    f"all {len(self._keys)} SerpApi key(s) were rejected (HTTP 401).",
                    hint="Check the keys in .env against "
                         "https://serpapi.com/manage-api-key",
                )

            if response.status_code == 429:
                detail = response.text[:200]
                if self._advance("hit its quota (HTTP 429)"):
                    continue
                raise RateLimitError(
                    f"all {len(self._keys)} SerpApi key(s) are out of searches "
                    f"(HTTP 429): {detail}",
                    hint="Check plans at https://serpapi.com/dashboard, or add another "
                         "key as SERPAPI_KEY_2 in .env. The free tier is 250 "
                         "searches/month per account.",
                )

            if response.status_code >= 400:
                raise ProviderError(
                    f"SerpApi returned HTTP {response.status_code}: {response.text[:200]}"
                )

            try:
                return response.json()
            except ValueError as exc:
                raise MalformedResponseError(
                    f"SerpApi returned non-JSON: {response.text[:200]}"
                ) from exc

        raise RateLimitError(
            f"all {len(self._keys)} SerpApi key(s) are unusable.",
            hint="Check https://serpapi.com/dashboard",
        )


def _to_citation(item: dict[str, Any], *, index: int) -> dict[str, Any]:
    """Map one Baidu organic result onto the shared citation/page shape."""
    return citation(
        title=item.get("title"),
        url=item.get("link"),
        site=item.get("source") or item.get("displayed_link"),
        index=item.get("position") or index,
        # Baidu's snippet is a search summary, never the article body, so the
        # pipeline labels these SEARCH_SNIPPET_ONLY.
        content=item.get("snippet"),
        publication_date=item.get("date"),
        raw=item,
    )


class SerpApiBaiduProvider(ResearchProvider):
    """Structured Baidu search. Search-only — cannot run the research prompts."""

    name = "serpapi"

    def __init__(self, settings: SerpApiSettings) -> None:
        self.settings = settings
        self.client = SerpApiClient(settings)
        self.log = get_logger()

    @property
    def supports_search(self) -> bool:
        return True

    def run_research(self, prompt: str, *, label: str = "") -> ResearchResponse:
        raise ProviderError(
            f"provider 'serpapi' is search-only and cannot run stage {label or 'prompts'}.",
            hint="Set provider to tencent, zhipu or mock for stages 1-2, and use "
                 "search_sweep.provider: serpapi for the Baidu sweep.",
        )

    def search(self, query, *, count=20, site=None, industry=None, freshness=None, mode=2):
        params: dict[str, Any] = {
            "engine": "baidu",
            "q": f"site:{site} {query}" if site else query,
            "ct": CT_SIMPLIFIED if self.settings.simplified_chinese_only else "1",
            "rn": str(max(1, min(int(count), 50))),
        }
        if industry:
            self.log.debug("baidu has no industry filter — ignoring industry=%r", industry)

        raw = self.client.search(params)

        # SerpApi reports an empty Baidu result set as an `error` string rather
        # than an HTTP error. That is a legitimate no-hit, not a failure.
        error = raw.get("error")
        if error and "hasn't returned any results" in str(error):
            self.log.info("baidu returned no results for %r", query)
            return {
                "query": query, "pages": [], "raw": raw, "supported": True,
                "site": site, "industry": None, "freshness": freshness,
                "note": "Baidu returned no results. Long multi-keyword queries often "
                        "return nothing on Baidu; shorter queries work better.",
            }
        if error:
            raise ProviderError(f"SerpApi/Baidu error for {query!r}: {error}")

        organic = raw.get("organic_results") or []
        pages = [_to_citation(o, index=i) for i, o in enumerate(organic, start=1) if o.get("link")]

        return {
            "query": query,
            "pages": pages,
            "raw": raw,
            "supported": True,
            "site": site,
            "industry": None,
            "freshness": freshness,
            "request_id": (raw.get("search_metadata") or {}).get("id"),
            # Baidu's own related/suggested queries are new search anchors for free.
            "related_searches": [
                r.get("query") for r in (raw.get("related_searches") or []) if r.get("query")
            ],
            "people_also_search_for": [
                r.get("query") for r in (raw.get("people_also_search_for") or []) if r.get("query")
            ],
        }

    def describe(self):
        return {
            "provider": self.name,
            "model": None,
            "keys_configured": len(self.settings.api_keys),
            "active_key_number": self.client.active_key_number,
            "endpoints": {"inference": None, "search": f"{SERPAPI_ENDPOINT}?engine=baidu"},
            "search_flags": {
                "engine": "baidu",
                "ct": CT_SIMPLIFIED if self.settings.simplified_chinese_only else "1",
            },
            "note": (
                "Baidu index via SerpApi. Baidu retired its official search API, so this "
                "is a vendor running the query and parsing the result page — not a "
                "first-party API. Search-only: cannot run the research prompts."
            ),
        }
