"""Find the Chinese domains whose text we can actually read.

The local-domain channel was first configured by guessing: 天眼查, 企查查,
爱企查 and ccgp.gov.cn, the sites a web search keeps pointing at. Three of the
four turned out to be login-walled and the fourth blocks automated fetching in
robots.txt, so every record from that channel was a search snippet and not one
of them carried a document.

This finds candidates the other way round. Baidu has already told us which
domains it returns for these companies — the saved raw responses hold them —
so the question is only which of those serve their body text to a normal
request. That is answerable without spending any SerpApi quota: take one saved
URL per domain and fetch it.

Usage:
    python tools/audit_domains.py <run-dir-or-glob> [...]

Writes a ranked report to stdout. Nothing here modifies a corpus.
"""

from __future__ import annotations

import glob as globlib
import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.collectors import is_gated  # noqa: E402
from src.fetcher import FetchBlocked, Fetcher, FetchError, FetchPolicy  # noqa: E402

# Infrastructure and Q&A hosts that carry no company record. Matched on the
# HOST, not as a substring of the URL: "baidu.com/s" as a substring also
# excludes baijiahao.baidu.com/s?id=..., which is Baidu's content platform and
# the second-largest host in these responses at 130 hits. That mistake kept it
# out of the first audit entirely.
SKIP_HOSTS = (
    "bdstatic.com", "browser.qq.com", "nourl.ubs.baidu.com",
    "zhidao.baidu.com", "wenku.baidu.com", "google.com", "bing.com",
)
# (host, path prefix) for search-results pages, which are fetchable and useless.
SKIP_PATHS = (("baidu.com", "/s"), ("baidu.com", "/link"))


def _skip(url: str, host: str) -> bool:
    from urllib.parse import urlparse

    if any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
        return True
    path = urlparse(url).path or "/"
    return any(host == h and path.startswith(pre) for h, pre in SKIP_PATHS)

MIN_BODY = 400          # below this a "body" is navigation chrome
SAMPLE_PER_HOST = 2     # try a second URL before writing a host off


def collect_urls(paths: list[str]) -> dict[str, list[str]]:
    """Every URL Baidu returned, grouped by host."""
    by_host: dict[str, list[str]] = {}

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("link", "url") and isinstance(value, str) \
                        and value.startswith("http"):
                    host = (urlparse(value).netloc or "").lower()
                    host = host.removeprefix("www.")
                    if not host or _skip(value, host):
                        continue
                    urls = by_host.setdefault(host, [])
                    if value not in urls:
                        urls.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for pattern in paths:
        # glob.glob, not Path.glob: the caller passes absolute patterns and
        # pathlib refuses those.
        matches = [Path(m) for m in sorted(globlib.glob(pattern))]
        for path in matches or [Path(pattern)]:
            if path.is_dir():
                files = sorted(path.rglob("raw_*responses.json"))
            else:
                files = [path]
            for file in files:
                try:
                    walk(json.loads(file.read_text(encoding="utf-8")))
                except Exception as exc:  # noqa: BLE001 - report and continue
                    print(f"  skipped {file}: {exc}", file=sys.stderr)
    return by_host


def probe(fetcher: Fetcher, urls: list[str]) -> dict:
    """Fetch up to SAMPLE_PER_HOST urls; keep the best outcome."""
    best = {"chars": 0, "verdict": "no sample", "url": None, "detail": ""}
    for url in urls[:SAMPLE_PER_HOST]:
        if is_gated(url):
            return {"chars": 0, "verdict": "gated", "url": url,
                    "detail": "listed in GATED_HOSTS; never fetched"}
        try:
            page = fetcher.fetch(url)
        except FetchBlocked as exc:
            outcome = {"chars": 0, "verdict": "blocked", "url": url,
                       "detail": str(exc)[:90]}
        except FetchError as exc:
            outcome = {"chars": 0, "verdict": "error", "url": url,
                       "detail": str(exc)[:90]}
        except Exception as exc:  # noqa: BLE001
            outcome = {"chars": 0, "verdict": "error", "url": url,
                       "detail": f"{type(exc).__name__}: {exc}"[:90]}
        else:
            text = page.text or ""
            if page.blocked:
                outcome = {"chars": len(text), "verdict": "blocked", "url": url,
                           "detail": "fetcher flagged a challenge page"}
            elif len(text) >= MIN_BODY:
                outcome = {"chars": len(text), "verdict": "readable", "url": url,
                           "detail": text[:70].replace("\n", " ")}
            else:
                outcome = {"chars": len(text), "verdict": "thin", "url": url,
                           "detail": "body under the chrome threshold"}
        if outcome["chars"] > best["chars"] or best["verdict"] == "no sample":
            best = outcome
        if best["verdict"] == "readable":
            break
    return best


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    by_host = collect_urls(sys.argv[1:])
    if not by_host:
        print("No URLs found. Point this at run directories holding "
              "raw_*responses.json files.")
        return 1

    fetcher = Fetcher(FetchPolicy())
    rows = []
    for index, (host, urls) in enumerate(
            sorted(by_host.items(), key=lambda kv: -len(kv[1])), start=1):
        result = probe(fetcher, urls)
        rows.append({"host": host, "hits": len(urls), **result})
        print(f"  [{index}/{len(by_host)}] {host} -> {result['verdict']} "
              f"({result['chars']} chars)", file=sys.stderr)

    rows.sort(key=lambda r: (r["verdict"] != "readable", -r["chars"]))
    verdicts = Counter(r["verdict"] for r in rows)

    print(f"\n{len(rows)} hosts probed: "
          + ", ".join(f"{v} {k}" for k, v in verdicts.most_common()))
    print("\nREADABLE — candidates for local_sources.domains")
    print(f"{'host':34s} {'hits':>5s} {'chars':>7s}  sample")
    for row in rows:
        if row["verdict"] != "readable":
            continue
        print(f"{row['host']:34s} {row['hits']:>5d} {row['chars']:>7d}  "
              f"{row['detail']}")

    print("\nREJECTED")
    for row in rows:
        if row["verdict"] == "readable":
            continue
        print(f"{row['host']:34s} {row['hits']:>5d} {row['verdict']:>9s}  "
              f"{row['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
