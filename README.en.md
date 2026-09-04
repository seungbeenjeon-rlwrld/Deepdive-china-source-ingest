# Deepdive — China Source Ingest

Collects Chinese-language source evidence for a target company and writes it to
a structured local folder that a downstream Claude research process can read
alongside ordinary web search.

It reaches sources a Western search index largely misses. Measured on
智元机器人 (AgiBot): of 49 domains returned by 4 Baidu queries, **39 (79%) never
appeared** in a Claude-web-search corpus for the same company — including
中国政府采购网 (procurement), 爱企查/天眼查 (company registries), Baidu B2B
supply-chain listings, 雪球/股吧 (investor forums) and regional government sites.

This repository is **only the ingestion layer**:

```
Tencent / Yuanbao
      ↓
China Local Source Ingestion   ← this repository
      ↓
Structured Local Research Corpus
      ↓
Claude Deep Research           ← separate, not built here
```

It deliberately does **not** analyse, compare or summarise. Its job is
`SEARCH → ACCESS → PRESERVE → VERIFY LINK`.

---

## Installation

```bash
git clone <your-remote> china-research
cd china-research
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Python 3.10 or newer. Two dependencies: the official Tencent Cloud SDK and PyYAML.

---

## Choosing a provider

Two providers are implemented. **The choice is decided by what account you can
get, not by preference.**

Three providers are implemented. `tencent` and `zhipu` can run the research
prompts; `serpapi` is **search-only** and is meant for the sweep, set via
`search_sweep.provider` while stages 1-2 run on a chat provider.

| | **`zhipu`** (Z.ai / 智谱) | **`tencent`** |
| --- | --- | --- |
| Account | plain **email** registration | China-site Tencent Cloud |
| Verification | none beyond email | mainland Chinese ID, HK/Macau/Taiwan permit, or Chinese business licence — **a foreign passport is not accepted** |
| Chinese web search | ✅ `search-prime` | ✅ 联网搜索API (Yuanbao search stack) |
| WeChat 公众号 coverage | whatever `search-prime` indexed | best available commercially |
| Search cost | ~$0.01/call | ¥46/1000 calls (标准版) |
| Model cost | `glm-4.7-flash` is free | 1M free tokens, then metered |

**If you do not have a Chinese ID or a Chinese company entity, use `zhipu`.**
Passport holders are routed to Tencent Cloud's international site, which does
not sell 联网搜索API at all (`/document/product/1806` 404s there), and whose
TokenHub gateway has no web-search capability whatsoever — running this
pipeline on a search-less model would produce unsourced claims, which is the
opposite of the point.

### `serpapi` — the Baidu index

| Variable | Required | What it is |
| --- | --- | --- |
| `SERPAPI_KEY` | yes | Key from [serpapi.com/manage-api-key](https://serpapi.com/manage-api-key) — email signup, **100 free searches/month**, no card |

```yaml
provider: zhipu              # stages 1-2
search_sweep:
  provider: serpapi          # the sweep runs on Baidu
```

`site:` filters become Baidu operators, so `site_filters: [mp.weixin.qq.com]`
works here too. `ct=2` restricts results to Simplified Chinese. Baidu's own
`related_searches` are harvested into `engine_suggested_anchors` — free new
search anchors the model never had to think of.

Two honest caveats. **Baidu retired its official search API**, so this is a
vendor running the query and parsing Baidu's result page, not a first-party
API — a different posture from Tencent's WSA, and your call whether that fits
your policy. And Baidu returns nothing for long multi-keyword queries; the
provider treats an empty result as a legitimate no-hit rather than an error,
but keep sweep queries short.

If the sweep provider cannot be built (missing key), the run **falls back to the
main provider and records the fallback in `metadata.notes`** rather than losing
a run whose stages 1-2 already succeeded.

### An honest note on WeChat coverage

Nothing sold commercially reproduces the Yuanbao app's WeChat access. Worth
knowing before you invest in an account:

- Z.ai's international platform exposes only the `search-prime` engine. The
  China-only platform (`open.bigmodel.cn`, which needs a Chinese phone number)
  adds `search_pro_sogou`, but its own documentation describes that engine as
  covering *腾讯生态（新闻/企鹅号）和知乎* — **it does not claim 微信公众号
  coverage.** So chasing a Chinese Zhipu account buys less than it appears to.
- The Yuanbao app works without a Chinese ID because consumer apps verify
  identity through the phone number or WeChat account you log in with, while
  cloud APIs must tie resources to a government-verified legal person. That
  app-side WeChat access is not sold as an API, and this tool does not automate
  the app — prompt 2 itself forbids circumventing WeChat access controls.

Treat WeChat yield as an empirical question. Point the sweep at
`mp.weixin.qq.com` and count what comes back before planning around it.

---

## API configuration

```bash
cp .env.example .env
```

Fill in **one** provider's keys.

### Option A — Z.ai (`provider: zhipu`)

| Variable | Required | What it is |
| --- | --- | --- |
| `ZHIPU_API_KEY` | yes | Key from [z.ai/manage-apikey/apikey-list](https://z.ai/manage-apikey/apikey-list) — email signup, no card |
| `ZHIPU_MODEL` | no | Overrides `zhipu.model`. `glm-4.7-flash` / `glm-4.5-flash` are free; `glm-5.3` is strongest |

Three things measured the hard way on the free tier:

- **The built-in `web_search` tool is a paid add-on** ($0.01/use) and returns
  `429 / 1113 insufficient balance` at zero balance — while the *model* itself
  works fine. `zhipu.use_builtin_search` is therefore `false` by default and
  retrieval is injected instead.
- **These models think by default.** Omitting `thinking` let a 16-token budget
  be consumed entirely by `reasoning_tokens`, returning empty content. The
  provider now always sends `thinking` explicitly.
- **The free models are contended, not capped.** Codes `1305` and `1113` arrive
  non-deterministically and clear on retry — a 6,000-char prompt succeeded while
  a 512-token one failed. The client retries up to 7 times with backoff.
| `ZHIPU_BASE_URL` | no | Defaults to `https://api.z.ai/api/paas/v4` |

Endpoints used:

| Purpose | Endpoint |
| --- | --- |
| Run the research prompts, with search | `POST /api/paas/v4/chat/completions` with a built-in `web_search` tool (`search_result: true`, `require_search: true`). The response carries the generated text **and** a `web_search` array of results — the same deal as Hunyuan's `SearchInfo`. |
| Structured search sweep | `POST /api/paas/v4/web_search`, with `search_domain_filter` standing in for SearchPro's `Site` so queries can target `mp.weixin.qq.com` |

### Option B — Tencent Cloud (`provider: tencent`)

| Variable | Required | What it is |
| --- | --- | --- |
| `TENCENT_SECRET_ID` | yes | From [console.cloud.tencent.com/cam/capi](https://console.cloud.tencent.com/cam/capi) |
| `TENCENT_SECRET_KEY` | yes | The matching secret key |
| `TENCENT_REGION` | no | Region for Hunyuan, default `ap-guangzhou`. 联网搜索API ignores it. |
| `TENCENT_MODEL` | no | Overrides `tencent.model` — **not** `hunyuan-lite`, which has no search capability |

Both products must be **activated in the console before the first call**:

- 腾讯混元大模型 (Hunyuan) — <https://console.cloud.tencent.com/hunyuan/settings>
- 联网搜索API (Web Search) — <https://console.cloud.tencent.com/wsapi/index>
  (click 开通服务 and choose a tier — 标准版 is the sensible default;
  尊享版/旗舰版 are enterprise-only, and only they return the longer
  `content` field and `authority_level`)

Keys must be created on the **master account**. For a shared team tool, prefer a
company-entity account with a scoped 子用户 rather than an individual's ID, so
the pipeline does not die when one person leaves.

⚠️ **Tencent Cloud auto-disables AccessKeys unused for 90 days.** This tool runs
intermittently, so a sudden `AuthFailure` after a quiet period usually means the
key was disabled, not that it is wrong.

| Purpose | Endpoint |
| --- | --- |
| Run the research prompts, with search | `hunyuan.tencentcloudapi.com` → `ChatCompletions` (`2023-09-01`). `EnableEnhancement` + `ForceSearchEnhancement` turn on Tencent's AI-search plugin; `Citation` + `SearchInfo` return a structured citation list; `EnableMultimedia` surfaces 视频号 results. |
| Structured search sweep | `wsa.tencentcloudapi.com` → `SearchPro` (`2025-05-08`) — 联网搜索API, which Tencent documents as built on the **Yuanbao (元宝) App** search stack. `Site` targets `mp.weixin.qq.com`; `Industry` gives `gov / news / acad / finance`. |
| Transport & auth | `tencentcloud-sdk-python-common` → `AbstractClient.call_json`: official TC3-HMAC-SHA256 signing and retries, returning **raw JSON dicts** so nothing is normalised away before it is persisted. |

There is no public API for the Yuanbao consumer app itself; 联网搜索API is its
productised search layer.

### Common to both

**Neither provider has server-side conversation memory**, which is why the
pipeline carries stage 1 → stage 2 state explicitly (see below).

Credentials are never logged; anything that looks like a secret is redacted
before it can reach the terminal or `logs/`.

---

## Run

Interactive:

```bash
python research.py
```

```
========================================
 China Local Research Collector
========================================

Company name:
> AgiBot

[1/2] Discovering company entities...
✓ Entity discovery complete
✓ Results saved

[2/2] Collecting Chinese local sources...
✓ Source collection complete
✓ Results saved

[+] Sweeping structured search over 12 recommended queries...
✓ Collected 143 structured search results
✓ Results saved

Research saved to:
./research/agibot/2026-09-02_174500/

Done.
```

Non-interactive:

```bash
python research.py --company "AgiBot"
```

Everything, including the primary-source registries:

```bash
python research.py --company "AgiBot" \
  --official-site "https://www.agibot.com.cn/article/315" \
  --filings "上纬新材" \
  --patents "上海智元新创技术有限公司"
```

A fully automated run measured end to end on 智元机器人 (`glm-4.7-flash` for
stages 1-2, Baidu retrieval injected, no human in the loop):

| | |
| --- | --- |
| Preserved sources | **176** |
| Invalid or missing access labels | **0** |
| Stage 1 → 2 context passed automatically | 8,268 chars |
| Recommended queries produced by stage 1 | 50 |
| Exchange filings indexed | 20 (of 652 available) |
| Official articles preserved in full text | 8 |
| Engine-suggested new anchors harvested | 38 |
| Cost | **$0** (free model + 12 of 250 monthly free searches) |

Offline smoke test with synthetic data, no API calls and no credentials needed:

```bash
python research.py --company "AgiBot" --provider mock
```

### All options

| Flag | Meaning |
| --- | --- |
| `--company NAME` | Target company. Prompted for if omitted. |
| `--stage {1,2,all}` | Which stage to run. Default `all`. |
| `--resume RUN_DIR` | Reuse the stage 1 result already saved in that run directory. |
| `--provider {tencent,zhipu,serpapi,mock}` | Override `provider` in `config.yaml`. |
| `--config PATH` | Alternate config file. Default `./config.yaml`. |
| `--official-site URL` | Crawl this newsroom index for full-text official articles (e.g. `https://www.agibot.com.cn/article/315`). |
| `--filings LISTED_NAME` | Index exchange filings for a listed entity from 巨潮资讯网 (e.g. `上纬新材`). Priority-1 sources. |
| `--patents ASSIGNEE` | Index CNIPA patents by assignee (e.g. `上海智元新创技术有限公司`). |
| `--no-reposts` | Skip looking for readable copies of gated sources. |
| `--no-search-sweep` | Skip the structured search sweep. |
| `--verbose` / `-v` | Print detailed logs to stderr as well as to `logs/run.log`. |

---

## How the two stages are wired

Stage 2 receives stage 1's **actual output, programmatically**. Nothing is
copied by hand, and nothing relies on hidden server-side session state:

```python
stage1_result = run_stage1(company)          # prompt 1 with {TARGET_COMPANY} filled in
stage2_result = run_stage2(
    company=company,
    stage1_context=stage1_result,            # the complete, unmodified text
)
```

Stage 1's text is injected into prompt 2 inside a delimited block:

```
<STAGE_1_RESEARCH>
TARGET_COMPANY: AgiBot

... the entire stage 1 result, verbatim ...
</STAGE_1_RESEARCH>
```

It is **never summarised, truncated or reordered** first. If the combined prompt
exceeds `research.max_context_chars`, the tool logs a loud warning and still
sends everything — dropping evidence would defeat the purpose. Raise the limit
or pick a larger-context model if the API pushes back.

The prompts themselves live in `prompts/` and are loaded from disk at runtime,
not hardcoded. They are the source of truth: the pipeline never imposes its own
taxonomy on top of what they produce.

---

## Output

```
research/
└── agibot/
    └── 2026-09-02_174500/
        ├── metadata.json                     run status, IDs, token usage, counts
        ├── 01_entity_discovery.md            stage 1, for humans and Claude
        ├── 01_entity_discovery.json          stage 1, for programs
        ├── 02_sources.md                     stage 2, full model output verbatim
        ├── 02_sources.json                   stage 2, with SOURCE_ID blocks indexed
        ├── 03_search_sweep.md                structured search results, readable
        ├── 03_search_sweep.json              structured search results, machine-readable
        ├── 04_official_site.md               official newsroom articles, full text
        ├── 04_official_site.json
        ├── 05_reposts.md                     readable copies of gated sources
        ├── 05_reposts.json
        ├── 06_exchange_filings.md            regulatory filings + direct PDF links
        ├── 06_exchange_filings.json
        ├── 07_patents.md                     CNIPA patents by assignee
        ├── 07_patents.json
        ├── raw_stage1_response.json          untouched API response
        ├── raw_stage2_response.json          untouched API response
        ├── raw_search_sweep_responses.json   untouched API responses
        ├── raw_sources/
        │   ├── source_001.md                 YAML front matter + preserved content
        │   ├── source_001.json
        │   └── ...
        └── logs/run.log
```

Every run gets its own timestamped directory. **Nothing is ever overwritten** —
if two runs land in the same second the second gets a `_2` suffix.

Company names become deterministic, filesystem-safe slugs:

```
AgiBot            → agibot
Unitree Robotics  → unitree-robotics
宇树科技           → 宇树科技        (CJK is path-safe and stays readable)
```

Names that cannot survive normalisation get a short SHA-1 suffix so two
different companies can never collide on one directory.

### What is preserved, and how honestly

`raw_sources/*.md` files start with YAML front matter and then the preserved
content:

```yaml
---
source_id: "SOURCE_001"
title: "..."
publisher: "..."
publication_date: "2026-01-15"
source_platform: "WeChat Official Account"
retrieval_url: "https://mp.weixin.qq.com/s?src=11&timestamp=...&signature=..."
canonical_url: "https://mp.weixin.qq.com/s/AbCdEf"
url_type: "STABLE_WECHAT_ARTICLE_URL"
content_access_status: "VERBATIM_PARTIAL_TEXT"
origin: "stage2_model_output"
derived:
  url_type_heuristic: "TEMPORARY_SOGOU_SIGNED_URL"
  is_ephemeral: true
---
```

Three rules this tool holds itself to:

1. **No invented metadata.** A field that was not available is `null`, never a
   guess and never an empty string.
2. **No mislabelling summaries as full text.** Search APIs return a *summary*
   of a page, not its body text, so every record from a search channel is
   labelled `SEARCH_SNIPPET_ONLY` or `URL_ONLY` — never `VERBATIM_FULL_TEXT`.
   The stronger labels can only come from the model's own output, where prompt 2
   governs their use. The label also follows what each provider actually
   returned: Hunyuan citations carry no body text so they are `URL_ONLY`, while
   Z.ai's `web_search` entries include a summary and so become
   `SEARCH_SNIPPET_ONLY`.
3. **Reported values are never recomputed.** `url_type` and the final
   `CONTENT_ACCESS_STATUS` tallies are stored exactly as the model reported
   them. The tool's own mechanical second opinion lives separately under
   `derived`, so a downstream process can compare the two — including the
   ephemeral-link rule that flags `src=11` / `timestamp=` / `signature=` /
   `token=` URLs as non-canonical.

`02_sources.json`'s `sources` array is a structural *index* of the `SOURCE_ID`
blocks. The complete, untouched model output is always in `text` and in
`02_sources.md`, so a parser miss can never lose evidence.

Everything is UTF-8 with `ensure_ascii=false`, so Chinese characters are stored
as Chinese characters, not `\uXXXX` escapes.

### What this actually does that a chat assistant cannot

Measured, not asserted. Two distinct wins:

**1. The Chinese search index reaches a different layer.** Running the same
company through Baidu (`engine=baidu`, `ct=2`) versus a Claude-web-search pass:

| | Domains |
| --- | --- |
| Claude web search corpus | 34 |
| Baidu, 4 queries | 49 (114 URLs) |
| Overlap | 10 |
| **Baidu-only** | **39 (79%)** |

The 10 that overlap are the tier-1 media layer a Western index reaches well —
新浪, 澎湃, 财联社, 界面, 钛媒体. The Baidu-only 39 include layers it does not:

| Domain | What it is |
| --- | --- |
| `baijiahao.baidu.com` | 百家号 — Baidu's own content ecosystem (39 of 114 hits) |
| `ccgp.gov.cn` | 中国政府采购网 — government procurement, legally-mandated |
| `aiqicha.baidu.com`, `tianyancha.com`, `qcc.com` | company registry aggregators |
| `nourl.ubs.baidu.com`, `b2b.baidu.com` | tender listings, supply-chain B2B |
| `xueqiu.com`, `guba.sina.com.cn`, `mguba.eastmoney.com` | investor forums |
| `10jqka.com.cn`, `stock.pingan.com` | financial data, brokerage research |
| `nmgwx.gov.cn`, `app.dahecube.com` | regional government, regional media |

Concrete finds from that layer, none of which a web-search pass produced:
`智元机器人（江苏）有限公司` (an entity web search could not confirm exists),
`爱仕达` subsidiary strategic cooperation covering OEM **and equity investment**,
`珞石机器人` named an "excellent supplier partner", and the bearing sole-supplier
story. This is the supply-chain and registry layer prompts 1 and 2 ask for.

Note the qualifier: *a different layer*, not *forbidden information*. Ask a chat
assistant about `GenieReasoner` by name and it finds it. What differs is what
the index volunteers when you have not yet learned the name.

**2. Enumeration of primary sources.** 巨潮资讯网 and CNIPA are open to
everyone, but a chat window samples them and this enumerates them.

**Enumeration: a large, structural difference.** A chat window is query-driven —
you get what you thought to ask about. These stages are index-driven, so you get
the complete set, dated, whether or not you knew the names existed:

| Stage | Result on AgiBot | Prompt 2 §10 priority |
| --- | --- | --- |
| Exchange filings (`--filings 上纬新材`) | **60 filings**, each with a direct PDF URL | **1** (highest) |
| Official newsroom (`--official-site …`) | **19 articles** in full text (62 discoverable) | **2** |
| Patents (`--patents <legal entity>`) | **272 reported** by the endpoint | **5** |
| Baidu sweep (`search_sweep.provider: serpapi`) | **57 results, 79% Baidu-only domains** | varies |

A single run produced **83 preserved sources, 79 of them in priority tiers 1–2.**

The gap this closes is real. A pure web-search pass over the same company
missed all 18 of these, which the newsroom index handed over for free:
`Act2Goal`, `GenieReasoner`, `均胜电子`, `北大—智元机器人联合实验室`, `X-Lab`,
`智元酷拓 D1`, `AGILINK`, `OmniPicker`, `临界点`, `长隆`, `Omdia 报告`,
`黄晓明工作室`, `临港集团`, `福布斯 50 强`, `CCTV 经济半小时`, `Genie Sim`,
`智元绝尘`, `夏澜`. You cannot search for a name you do not know; enumeration
hands you the names, and search then fills in the detail. The two are
complements.

So the value is **a different index plus recall over primary sources** — and
none of it requires a Chinese account or a Chinese model. Baidu access needs a
SerpApi key (email signup, 100 free searches/month); everything else is free.

### Retrieval injection, and why the model is not trusted with labels

A provider's built-in search is usually a paid add-on — Zhipu's is $0.01/use
and is refused outright on a zero-balance account. So the pipeline runs
retrieval **itself** (`research.retrieval_injection`) and injects the results
into the stage prompts inside a `<SEARCH_RESULTS>` block. Two gains beyond
cost: it uses the Baidu index, and the exact evidence handed to the model is
written to `01_entity_discovery.json` / `02_sources.json` under `retrieval`,
so a downstream reader can check what the model could and could not have seen.

That last point matters, because **models do not respect prompt 2's labelling
discipline.** Measured on `glm-4.7-flash`:

| Observed | Reality |
| --- | --- |
| 58 records labelled `VERBATIM_PARTIAL_TEXT` | their content was 32-161 chars — the injected snippets |
| `SEARCH_SNIPPED` | not a permitted status; a typo |
| `CONTENT_ACCESS_STATUS` omitted entirely | invisible to any downstream filter |

Prompt 2 §6 forbids exactly this, and the model does it anyway. Since the
pipeline knows what it injected, it can check — `verify_labels()` runs after
stage 2 and:

- **downgrades** a text-claiming label when the content fits inside the snippet
  budget the model was given (the model cannot have read more than a snippet);
- **normalises** a mistyped or missing status to the weakest claim the content
  supports;
- **never upgrades**, and keeps the model's original claim in
  `derived.label_claimed` with `derived.label_downgrade_reason`.

The audit is reported in `02_sources.json.label_audit` and counted in
`metadata.counts.labels_downgraded`. On a clean run the corpus therefore
contains **zero invalid or missing labels** — every record's access status is
one the retrieval can actually support.

### The WeChat gap, and how the pipeline closes it automatically

WeChat serves a verification page (`环境异常，完成验证后即可继续访问`) to automated
requests, so 公众号 article **bodies cannot be fetched**. There is no API that
changes this: Tencent's own developer community states plainly that
**公众号没有开放的文章检索接口** — the only official article API is for an account
owner reading their own posts. Paid search products (联网搜索API, Zhipu's
`search_pro_sogou`) solve *discovery*, not *content*; and 搜狗 has no public API
at all. This tool does not attempt to defeat that verification — prompt 2
forbids circumventing WeChat access controls, and a label produced by
circumvention would not be worth having.

Two automated stages recover the same material from channels that are open,
because Chinese corporate communications are published redundantly:

| Stage | What it does |
| --- | --- |
| **4. Official newsroom crawl** (`--official-site URL`) | Crawls the company's own paginated news index and preserves each article's full text. A primary source (prompt 2 §10 priority 2) carrying much of what the company also posts to WeChat. |
| **5. Repost resolution** (on by default) | For each source stuck at `URL_ONLY` whose host is gated, searches for the title and fetches a readable copy. |

Measured on AgiBot (智元机器人): the newsroom crawl preserved **12 articles in
full text**, and repost resolution recovered **3 of 5** blocked WeChat articles —
two from news outlets (央广网, 腾讯新闻) and one from the company's own site. The
remaining two are declared as gaps rather than quietly dropped.

**Labelling is strict here.** A repost's full text is the *repost's*, not the
original's, so:

- the WeChat record keeps `content_access_status: URL_ONLY` and is never mutated;
- the recovered copy is a **separate** record linked by
  `extra.reposts_source_id` and `extra.original_url`;
- if the recovered copy sits on the company's own domain it is labelled
  `Official Company Source` (priority 2) with id `OFFICIAL_ALT_*`; otherwise
  `Secondary Repost` (priority 10) with id `REPOST_*`;
- `extra.label_note` states that wording may differ from the original.

Fetching is polite: it identifies itself by User-Agent, rate-limits
(`fetch.delay_seconds`), honours `robots.txt`, caps response size, and refuses
to fetch bodies from gated hosts at all (`collectors.GATED_HOSTS`).

What still needs a human: 2 WeChat articles with no readable copy anywhere. The
tool leaves them as `URL_ONLY` with stable canonical URLs, which a person can
open in a normal browser.

### Reading the corpus downstream

A Claude research process can point at `./research/{company}/` and read the
Markdown directly, or load the JSON for structured ingestion. Suggested entry
points: `metadata.json` for run health, `01_entity_discovery.md` for the alias
dictionary, and `raw_sources/` for per-source evidence with URLs and access
status.

---

## Resume

Stage 1 is the expensive half, so it is restartable.

Run stage 1 only:

```bash
python research.py --company "AgiBot" --stage 1
```

Run stage 2 later against that saved result — stage 1 is **loaded from disk**,
not re-run:

```bash
python research.py --resume ./research/agibot/2026-09-02_174500 --stage 2
```

The company name is recovered from `metadata.json`, so you do not need to retype
it. As a shortcut, this picks up the most recent run that has a stage 1 result:

```bash
python research.py --company "AgiBot" --stage 2
```

Stage 1 text is recovered from `01_entity_discovery.json`, falling back to
`raw_stage1_response.json` and then to `01_entity_discovery.md` — the Markdown
file is a lossless carrier, so any one of the three is enough.

---

## Errors

Authentication failures, rate limits, timeouts, empty responses, malformed JSON
and unexpected response shapes are each reported as a readable one-line error
plus a suggested fix, and recorded in `metadata.json`:

```json
{
  "target_company": "AgiBot",
  "stage1_status": "completed",
  "stage2_status": "failed",
  "stage2_error": "ChatCompletions failed [RequestLimitExceeded]: ...",
  "provider": "tencent",
  "model": "hunyuan-turbos-latest"
}
```

**A later failure never destroys earlier results.** If stage 2 fails, the stage 1
files stay on disk untouched and the tool tells you how to retry just stage 2. If
one query in the search sweep fails, the rest of the sweep still completes and
the failure is listed in `03_search_sweep.json`. If the sweep is capped by
`search_sweep.max_queries`, the queries that were *not* searched are written out
explicitly rather than silently dropped.

Terminal output stays at progress level; full detail including warnings goes to
`<run_dir>/logs/run.log`. Use `-v` to see it live.

---

## Configuration

`config.yaml` holds behaviour; `.env` holds credentials. The file is optional —
without it (or without PyYAML) the tool runs on the defaults in
`src/config.py`.

```yaml
provider: zhipu            # tencent | zhipu | mock

output:
  root_dir: ./research
  save_markdown: true
  save_json: true
  save_raw_response: true
  save_raw_sources: true

research:
  stage1_prompt: ./prompts/prompt1_entity_discovery.md
  stage2_prompt: ./prompts/prompt2_source_collection.md
  max_context_chars: 120000    # soft warning only; never truncates

zhipu:
  model: glm-4.7-flash             # free tier; glm-5.3 is strongest
  base_url: https://api.z.ai/api/paas/v4
  search_engine: search_pro_jina   # chat web_search tool engine
  search_api_engine: search-prime  # standalone /web_search engine
  content_size: high
  require_search: true

tencent:
  model: hunyuan-turbos-latest
  region: ap-guangzhou
  temperature: 0.4
  timeout_seconds: 900
  enable_enhancement: true
  force_search_enhancement: true
  citation: true
  search_info: true
  enable_multimedia: true
  enable_speed_search: false

search_sweep:
  enabled: true
  max_queries: 12
  results_per_query: 20
  mode: 2                      # 0 web results, 1 VR cards, 2 mixed
  freshness: null              # e.g. y2 = last 2 years, m3 = last 3 months
  site_filters:
    - null                     # unrestricted
    - mp.weixin.qq.com         # WeChat Official Account articles
  industries: []               # e.g. [gov, acad]
```

Each query is run once per `site_filters` entry, so the call count is
`max_queries × len(site_filters) × max(1, len(industries))`. Keep that in mind
before raising the caps.

---

## Layout

```
research.py               CLI: arguments, terminal output, error reporting only
config.yaml               behaviour
prompts/                  the two research prompts, loaded at runtime
src/
├── config.py             config + .env resolution
├── models.py             ResearchResponse, SourceRecord, RunMetadata, URL rules
├── provider.py           ResearchProvider interface + MockProvider + error types
├── tencent_client.py     Tencent-specific code, nothing else imports it
├── zhipu_client.py       Z.ai-specific code, nothing else imports it
├── serpapi_client.py     Baidu index via SerpApi (search-only provider)
├── fetcher.py            polite HTTP fetch + readability-style body extraction
├── collectors.py         newsroom crawl, repost resolution, filing + patent registries
├── pipeline.py           stage 1, stage 2, context passing, parsers, sweep
├── storage.py            StorageBackend interface + LocalStorageBackend
└── utils.py              slugs, timestamps, logging, .env, redaction
tests/test_pipeline.py    104 offline tests, standard library only
```

Two seams are deliberate:

- **`ResearchProvider`** — adding a vendor means one new subclass with
  `run_research()` and optionally `search()`, registered in `build_provider()`.
  Providers translate their payloads into a small normalised citation/page
  shape and keep the original under `_raw`, so the pipeline imports nothing
  vendor-specific. `zhipu` was added this way without touching `pipeline.py`
  logic, `storage.py` or the prompts.
- **`StorageBackend`** — Google Drive, Notion or S3 would each be one new
  subclass. Only `LocalStorageBackend` is implemented, by design.

---

## Tests

```bash
python -m unittest discover tests -v
```

104 tests, no network and no credentials required. They cover the guarantees that
matter: that stage 2 really receives stage 1's full text, that a stage 2 failure
leaves stage 1 intact, that runs never overwrite each other, that search results
are never labelled as verbatim full text, that signed Sogou links and WeChat's
legacy query-form links are never treated as canonical, that a verification
interstitial is reported rather than stored as content, that a link-heavy
product menu never wins over the article body, that a repost never overwrites
the original's label, that a model's `VERBATIM_*` claim is downgraded when the
retrieval cannot support it, that a mistyped or missing status is normalised
rather than stored, and that Chinese characters survive every round trip.

---

## Scope

Not built here, on purpose: the downstream competitive research system, any web
frontend, any database, and any task queue.

One measured caveat: the Google Patents query endpoint rate-limits bursts with
HTTP 503, and the block can persist. Backoff is implemented and a throttled run
reports a failure rather than an empty result, but if you need patents reliably
use an API with a key (EPO OPS has a free tier and also indexes CN).

On source access: the tool fetches public pages that serve their content to a
normal, self-identifying, rate-limited request, and it crawls a company's own
newsroom. It does **not** bypass authentication, defeat CAPTCHAs or bot
detection, circumvent WeChat access controls, or fabricate full text where only
a snippet was available. Hosts that gate automated access are recorded as
`URL_ONLY` with a stable canonical URL for a human to open.

One note on the prompt files: they are your Chinese prompts verbatim, with only
the Word/Markdown paste artefacts repaired (`\_` → `_`, `\#` → `#`, rejoined
mid-sentence line breaks, and collapsed runs of intra-line spaces in prompt 2's
bullet lists). No wording was changed, simplified or reordered.
