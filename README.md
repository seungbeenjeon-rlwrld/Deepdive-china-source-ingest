# Deepdive — China Source Ingest

중국 기업에 대한 **중국어 원본 소스를 자동 수집**하여, 다운스트림 Claude 리서치가 읽을 수 있는 구조화된 로컬 코퍼스로 저장하는 백엔드임.

서양 검색 인덱스가 도달하지 못하는 중국 로컬 소스 계층에 접근하는 것이 핵심임. 智元机器人(AgiBot) 대상 실측 결과, Baidu 쿼리 4개가 반환한 도메인 49개 중 **39개(79%)가 동일 회사에 대한 Claude 웹검색 코퍼스에 존재하지 않았음**.

## 책임 경계

```
중국 로컬 소스 수집  ←  이 저장소
        ↓
구조화된 증거 코퍼스
        ↓
Claude 심층 리서치  ←  별도 시스템, 여기 없음
```

수집·보존만 담당함. 분석·비교·해석은 하지 않음. 이 경계는 의도적이며, `prompts/prompt2_source_collection.md`가 명시적으로 금지함.

---

## 1. 중국 로컬 검색엔진 활용 방식

### 1.1 왜 서드파티 SERP API를 경유하는지

중국 주요 검색엔진의 공식 검색 API가 전부 폐지된 상태임.

| 엔진 | 공식 검색 API |
|---|---|
| Baidu | 百度搜索开放平台 **폐지** |
| Bing | Search API **2025-08 은퇴** |
| Sogou | 애초에 없음 |
| Tencent WSA (联网搜索API) | 존재하나 **중국 신분증/법인 인증 필요** |

따라서 프로그램으로 중국 인덱스에 접근하는 경로가 SERP 벤더 경유뿐임. 본 저장소는 SerpApi를 사용하며, `engine=baidu`로 Baidu 인덱스를 조회함.

> 이는 Baidu 1차 API가 아니라 벤더가 대신 검색·파싱하는 구조임. Tencent WSA 같은 공식 API와 성격이 다르므로, 사내 정책상 스크래핑 대행 벤더 사용이 제한되면 이 채널만 제외 가능함.

### 1.2 요청 구성

```python
{
  "engine": "baidu",
  "q":      "site:mp.weixin.qq.com 智元机器人 供应商",  # Baidu 연산자 지원
  "ct":     "2",     # 간체 중국어로 제한 — 중국 본토 콘텐츠만
  "rn":     "30",    # 페이지당 결과 (최대 50)
}
```

- `ct=2`가 중요함. 간체 제한 없이는 결과가 국제 콘텐츠로 희석됨
- `site:` 연산자로 도메인 타겟팅 가능하나, **Baidu는 `mp.weixin.qq.com`을 유효하게 색인하지 않음**. 이 경우 연산자만 제거하고 나머지 검색 의도를 유지하도록 처리됨 (`clean_queries()`)
- Baidu는 다중 키워드 장문 쿼리에 빈 결과를 반환하는 경향이 있음. 빈 결과는 오류가 아닌 정상 무매칭으로 처리됨

### 1.3 검색 결과를 프롬프트에 주입하는 구조

LLM 제공자의 내장 검색 툴을 쓰지 않고, **파이프라인이 직접 검색하여 프롬프트에 주입**함.

이유가 세 가지임.

1. **유료 애드온 회피** — Zhipu 내장 `web_search`는 회당 $0.01이며, 잔액 0 계정에서는 `429/1113`으로 거부됨. 모델 자체는 무료로 동작함
2. **인덱스 선택권** — 제공자 자체 검색이 아니라 Baidu 인덱스를 쓸 수 있음
3. **감사 가능성** — 모델에게 실제로 건넨 증거가 디스크에 기록됨. 다운스트림 독자가 "모델이 무엇을 볼 수 있었고 볼 수 없었는지" 검증 가능함

주입 형식:

```
<SEARCH_RESULTS>
[1] 제목
    URL: https://...
    DATE: 2026-03-31
    FULL_TEXT (34071 chars retrieved):     ← 실제 페이지를 fetch한 경우
    ...본문...
[2] 제목
    URL: https://...
    SNIPPET_ONLY: ...검색 요약 200자...    ← fetch 실패/차단된 경우
</SEARCH_RESULTS>
```

`FULL_TEXT`와 `SNIPPET_ONLY`를 구분 표기하고, 프롬프트에서 **`SNIPPET_ONLY` 항목은 원문이라고 주장할 수 없음**을 명시함. 이것이 라벨 오기를 줄임.

### 1.4 검색어 정제

모델이 생성한 추천 쿼리를 그대로 쓰면 오염됨. 실측 사례로 첫 쿼리가 `微信生态搜索**:` — 섹션 제목이 쿼리로 파싱된 것이었음.

`clean_queries()`가 처리하는 것:

- 마크다운 잔여물(`**`, 후행 `:`) 제거
- 제목·라벨 형태 제외
- 회사명 또는 별칭을 포함하지 않는 쿼리 제외 (주제 이탈 방지)
- 해당 엔진이 처리하지 못하는 `site:` 연산자만 제거하고 나머지 유지

### 1.5 페이지 본문 취득과 품질 검증

검색 스니펫만으로는 모델이 보존할 내용이 없으므로, 상위 N건의 실제 페이지를 fetch함 (`src/fetcher.py`).

fetch 정책:
- User-Agent로 자신을 밝힘, 요청 간 지연, `robots.txt` 준수, 응답 크기 상한
- **자동 접근을 차단하는 호스트는 본문 fetch를 시도하지 않음** (`GATED_HOSTS`)
- 본문 추출은 사이트별 설정 없이 동작함. 모든 블록 컨테이너를 텍스트 길이 × (1 − 링크밀도)² 로 점수화하여 선택함. 제품 메가메뉴가 기사보다 길 수 있으므로 링크 밀도 페널티가 필수적임

**품질 검증이 중요함.** 이를 넣기 전 실측에서 `xueqiu.com`이 서로 다른 4개 URL에 대해 **정확히 동일한 34,071자**를 반환했음. 내용은 `{"_waf_...}` + base64 페이로드였고, 길이 검사만으로는 통과되어 **141,477자 중 136,284자가 쓰레기로 코퍼스에 저장될 상태였음**.

검증 항목:
- WAF/JS 챌린지 마커 탐지 (`_waf_`, `__cf_`, `jschl`, `x5secdata` 등)
- 검증 페이지 문구 탐지 (`环境异常`, `完成验证后即可继续访问`, `captcha` 등)
- 산문 여부 판정 — 중국어 검색으로 도달한 페이지에 CJK 비율이 극히 낮고 긴 base64 토큰이 있으면 인코딩 페이로드로 판정함

또한 **조용한 실패를 허용하지 않음**. 이전에는 본문이 짧아 건너뛴 경우 기록이 남지 않아 40건 중 16건이 흔적 없이 사라졌음. 현재는 성공·실패 건수의 합이 시도 건수와 일치함.

---

## 2. 무슨 정보를 새로 얻을 수 있는지

### 2.1 실측 — Baidu 전용 도메인 79%

동일 회사에 대해 Baidu와 Claude 웹검색을 비교한 결과임.

| | 도메인 |
|---|---|
| Claude 웹검색 코퍼스 | 34 |
| Baidu 쿼리 4개 | 49 (URL 114건) |
| 겹침 | 10 |
| **Baidu 전용** | **39 (79%)** |

겹친 10개는 서양 인덱스도 잘 도달하는 1티어 매체임 — 新浪, 澎湃, 财联社, 界面, 钛媒体.

Baidu 전용 39개에 포함된 계층:

| 도메인 | 성격 |
|---|---|
| `baijiahao.baidu.com` | 百家号 — Baidu 자체 콘텐츠 생태 (114건 중 39건) |
| `ccgp.gov.cn` | **中国政府采购网** — 정부조달, 법정 공개 |
| `aiqicha.baidu.com`, `tianyancha.com`, `qcc.com` | **기업정보 레지스트리** |
| `nourl.ubs.baidu.com`, `b2b.baidu.com` | 招标 리스팅, 공급망 B2B |
| `xueqiu.com`, `guba.sina.com.cn`, `mguba.eastmoney.com` | 투자자 포럼 |
| `10jqka.com.cn`, `stock.pingan.com` | 금융 데이터, 증권사 리서치 |
| `nmgwx.gov.cn`, `app.dahecube.com` | 지방정부, 지역매체 |

### 2.2 실제로 발견된 정보 (웹검색이 산출하지 못한 것)

- **`智元机器人（江苏）有限公司`** — 웹검색은 존재 확인조차 못 한 법인. 招标 정보와 함께 발견됨
- **`永臻股份(603381.SH)`** — 알루미늄 소재 공급업체, 종목코드 포함
- **爱仕达 자회사 전략협력** — 代工 + **지분투자** 포함
- **珞石机器人** — "우수 공급업체 파트너" 수상
- **베어링 독점 공급업체** — 国资 배경
- **产业链 4대 분류** — 股权绑定企业 / 整机代工企业 / 通用零部件供应商 / 电机减速器执行器龙头
- **子公司 5개 분사 구조** — 智元酷拓(4족), 智鼎(상업청소), 临界点(灵巧手), 觅蜂科技(데이터), 擎天租(렌탈)
- **对外投资 72家**

공급업체명과 종목코드가 검색 스니펫 단계에서 이미 확보됨. 이는 `SEARCH_SNIPPET_ONLY`로 정직하게 라벨된 실제 증거임.

### 2.3 1차 소스 전수 열거

검색과 별개로, **법정 공개 데이터를 전수 열거**함. 챗 창은 표본을 제시하지만 이 파이프라인은 전체 집합을 반환함.

| 채널 | 실측 | Prompt 2 §10 우선순위 |
|---|---|---|
| 거래소 공시 (巨潮资讯网) | **652건 조회 가능**, PDF 직링크 | **1위 (최상)** |
| 관방 뉴스룸 크롤 | **62건 발견**, 전문 취득 | 2위 |
| CNIPA 특허 (Google Patents) | **272건** | 5위 |

이것이 챗 창과의 실질적 차이임. 웹검색은 **질문 기반**이라 물어본 것만 나오고, 열거는 **인덱스 기반**이라 이름을 몰라도 목록에 나타남.

실측 검증: 순수 웹검색 Stage 1이 놓쳤으나 뉴스룸 열거가 전부 산출한 항목 18개 —
`Act2Goal`, `GenieReasoner`, `均胜电子`, `北大—智元机器人联合实验室`, `X-Lab`, `智元酷拓 D1`, `AGILINK`, `OmniPicker`, `临界点`, `长隆`, `Omdia 报告`, `黄晓明工作室`, `临港集团`, `福布斯 50强`, `CCTV 经济半小时`, `Genie Sim`, `智元绝尘`, `夏澜`.

**모르는 이름은 검색할 수 없음.** 열거가 이름을 제공하고 검색이 상세를 채우는 보완 관계임.

### 2.4 위챗 공백과 우회

위챗은 자동 요청에 검증 페이지(`环境异常，完成验证后即可继续访问`)를 반환하므로 **공중호 본문 취득이 불가능함**. 이를 바꾸는 API는 존재하지 않음 — 텐센트 공식 개발자 커뮤니티가 **公众号没有开放的文章检索接口**라고 명시함. 계정 소유자가 자기 글을 조회하는 API만 존재함.

본 저장소는 해당 검증을 우회하지 않음. Prompt 2가 금지하며, 우회로 만든 라벨은 신뢰할 수 없음.

대신 중국 기업 커뮤니케이션이 **중복 게시**되는 성질을 이용함.

| 단계 | 동작 |
|---|---|
| 관방 뉴스룸 크롤 | 회사 자체 뉴스 인덱스를 순회하여 전문 보존. 위챗에 올리는 내용의 상당 부분을 포함함 |
| 전재본 해결 | `URL_ONLY` 상태인 차단 소스의 제목으로 재검색하여 읽을 수 있는 사본 취득 |

실측: 차단된 위챗 5건 중 **3건 복구** — 2건은 언론(央广网, 腾讯新闻), 1건은 회사 자체 사이트. 나머지 2건은 gap으로 명시됨.

라벨 규율이 엄격함. 전재본의 전문은 **전재본의** 전문이지 원본의 것이 아니므로:
- 위챗 레코드는 `URL_ONLY` 유지, 절대 변경하지 않음
- 복구본은 `extra.reposts_source_id` / `extra.original_url`로 연결된 **별도 레코드**
- 복구본이 회사 자체 도메인이면 `Official Company Source`(2위), 아니면 `Secondary Repost`(10위)로 분류함

---

## 3. 자동화 파이프라인 구조

### 3.1 단계 구성

```
회사명 입력
   │
   ├─ [1/2] Stage 1 — 실체·별칭 발견
   │        Baidu 검색 → 페이지 fetch → 프롬프트 주입 → LLM
   │        산출: 법인명·이력명·별칭·제품·인물·소스맵·추천쿼리
   │
   ├─ [2/2] Stage 2 — 소스 수집·증거 보존
   │        Stage 1 전문을 <STAGE_1_RESEARCH>로 주입 (요약하지 않음)
   │        + Stage 1의 추천 쿼리로 재검색·fetch → 프롬프트 주입 → LLM
   │        → 라벨 검증 (verify_labels)
   │
   ├─ [+] 구조화 검색 스윕      Baidu, 추천 쿼리 전체
   ├─ [+] 관방 뉴스룸 크롤       페이지네이션 순회 → 전문
   ├─ [+] 거래소 공시            巨潮资讯网 → PDF 직링크
   ├─ [+] CNIPA 특허             Google Patents, 출원인 기준
   └─ [+] 전재본 해결            차단 소스의 읽을 수 있는 사본
```

### 3.2 상태 전달

Stage 2는 Stage 1의 **실제 출력을 프로그램으로** 받음. 서버측 대화 메모리에 의존하지 않음.

```python
stage1 = run_stage1(company)
stage2 = run_stage2(company=company, stage1_context=stage1)  # 전문, 무손실
```

Stage 1 결과는 **요약·절단·재배열 없이** 주입됨. 합산 프롬프트가 `research.max_context_chars`를 초과하면 경고만 출력하고 전량 전송함 — 증거를 버리는 것이 목적에 반하기 때문임.

이 설계로 실행이 재시작 가능함:

```bash
python research.py --company "智元机器人" --stage 1
python research.py --resume ./research/智元机器人/2026-09-04_034201 --stage 2
```

Stage 1 텍스트는 `01_entity_discovery.json` → `raw_stage1_response.json` → `01_entity_discovery.md` 순으로 복구됨. Markdown도 무손실 보관 형식이므로 셋 중 하나만 있으면 충분함.

### 3.3 실패 격리

| 상황 | 동작 |
|---|---|
| Stage 2 실패 | Stage 1 파일 그대로 보존, 재시도 명령 안내, `metadata.json`에 사유 기록 |
| 스윕 쿼리 1건 실패 | 나머지 스윕 계속 진행, 실패 목록 기록 |
| `max_queries`로 절단 | 검색되지 않은 쿼리를 명시적으로 기록 |
| sweep provider 빌드 실패 | 메인 provider로 폴백, `metadata.notes`에 기록 |
| cninfo 504 | 재시도 백오프. 수백 건 있는 회사에 0건을 보고하지 않음 |
| Zhipu `429/1305·1113·1302` | 혼잡·레이트리밋으로 판정하여 재시도 (최대 7회). 인증 실패는 즉시 중단 |

### 3.4 증거 규율 — 모델의 자기보고를 신뢰하지 않음

Prompt 2 §6이 요약을 원문으로 표기하는 것을 금지하나, **모델은 그대로 위반함**. `glm-4.7-flash` 실측:

| 관측 | 실제 |
|---|---|
| 58건을 `VERBATIM_PARTIAL_TEXT`로 라벨 | 내용이 **32~161자** — 주입한 스니펫 그 자체 |
| `SEARCH_SNIPPED` | 허용되지 않는 라벨값 (오타) |
| `CONTENT_ACCESS_STATUS` 생략 | 다운스트림 필터에서 누락됨 |

파이프라인은 무엇을 주입했는지 알고 있으므로 검증이 가능함. `verify_labels()`가 Stage 2 이후 실행되어:

- 내용 길이가 주입한 스니펫 예산 안에 있으면 원문 주장을 **강등**함 (스니펫 이상을 읽었을 수 없음)
- 오타·누락 라벨을 내용이 지지하는 가장 약한 값으로 **정규화**함
- **절대 상향하지 않음**. 모델의 원래 주장을 `derived.label_claimed`, 사유를 `derived.label_downgrade_reason`에 보존함

정상 실행 시 코퍼스의 **무효·누락 라벨이 0건**이 됨. 증거 등급이 모델의 자기보고가 아니라 실제 검색 내역으로 뒷받침됨.

### 3.5 교체 가능한 두 지점

```python
class ResearchProvider:   # tencent | zhipu | serpapi | mock
    def run_research(self, prompt: str) -> ResearchResponse: ...
    def search(self, query: str) -> dict: ...

class StorageBackend:     # LocalStorageBackend 만 구현됨
    def save(self, ...): ...
```

- provider는 자체 응답을 정규화된 citation/page 형태로 변환하고 원본을 `_raw`에 보존함. 파이프라인은 벤더 고유 코드를 import하지 않음
- `search_sweep.provider`로 **스윕만 다른 provider** 사용 가능함. `serpapi`는 검색 전용이므로 Stage 1·2는 챗 provider가, 스윕은 Baidu가 담당하는 구성이 가능함
- Google Drive·Notion·S3는 `StorageBackend` 서브클래스 추가로 확장 가능함. MVP 범위상 구현하지 않음

---

## 4. 설치

```bash
git clone git@github.com:seungbeenjeon-rlwrld/Deepdive-china-source-ingest.git
cd Deepdive-china-source-ingest
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Python 3.10 이상 필요함. 의존성 4개임 — `requests`, `beautifulsoup4`, `PyYAML`, `tencentcloud-sdk-python-common`.

## 5. 설정

```bash
cp .env.example .env
```

### Baidu 검색 (SerpApi)

| 변수 | 필수 | 내용 |
|---|---|---|
| `SERPAPI_KEY` | 예 | [serpapi.com/manage-api-key](https://serpapi.com/manage-api-key). 이메일 가입, **월 250건 무료**, 카드 불필요 |

검색 전용이므로 프롬프트 실행은 불가함. 아래 LLM provider와 함께 사용함.

### LLM — Z.ai (Zhipu)

| 변수 | 필수 | 내용 |
|---|---|---|
| `ZHIPU_API_KEY` | 예 | [z.ai/manage-apikey/apikey-list](https://z.ai/manage-apikey/apikey-list). 이메일 가입, 중국 전화번호 불필요 |

무료 티어에서 확인된 사항 3가지:

- **내장 `web_search`는 유료 애드온**($0.01/회)이며 잔액 0에서 `429/1113`으로 거부됨. 모델 자체는 동작함. `zhipu.use_builtin_search`는 기본 `false`이고 대신 retrieval injection을 사용함
- **flash 모델은 기본이 thinking 모드**임. `thinking` 미지정 시 16토큰 예산이 전부 `reasoning_tokens`로 소진되고 content가 빔. 항상 명시적으로 전송함
- **무료 모델은 제한이 아니라 혼잡**임. `1305`/`1113`/`1302`가 비결정적으로 발생하고 재시도로 통과함. 6,000자 프롬프트가 성공하고 512토큰 요청이 실패한 사례가 있음

### LLM — Tencent Cloud (선택)

위챗·텐센트 생태계 커버리지가 가장 좋으나 **중국 본토 신분증, 港澳台 통행증, 또는 중국 사업자등록증이 필요함**. 외국 여권은 접수되지 않으며, 국제站에는 联网搜索API가 존재하지 않음.

```
TENCENT_SECRET_ID / TENCENT_SECRET_KEY / TENCENT_REGION / TENCENT_MODEL
```

구현은 완료되어 있으나 실제 크레덴셜로 검증되지 않았음. TC3 서명과 에러 매핑만 라이브 확인됨.

## 6. 실행

```bash
python research.py                          # 대화형
python research.py --company "智元机器人"     # 비대화형
python research.py --company "X" --provider mock   # 오프라인, 키 불필요
```

전체 채널 사용:

```bash
python research.py --company "智元机器人" \
  --official-site "https://www.agibot.com.cn/article/315" \
  --filings "上纬新材" \
  --patents "上海智元新创技术有限公司"
```

### 옵션

| 플래그 | 내용 |
|---|---|
| `--company NAME` | 대상 회사. 생략 시 입력 요청 |
| `--stage {1,2,all}` | 실행 단계. 기본 `all` |
| `--resume RUN_DIR` | 해당 실행의 Stage 1 결과를 재사용 |
| `--provider {tencent,zhipu,serpapi,mock}` | provider 강제 지정 |
| `--official-site URL` | 관방 뉴스룸 인덱스 크롤 |
| `--filings LISTED_NAME` | 巨潮资讯网 공시 열거 (예: `上纬新材`) |
| `--patents ASSIGNEE` | CNIPA 특허 열거 (예: `上海智元新创技术有限公司`) |
| `--no-reposts` | 전재본 해결 생략 |
| `--no-search-sweep` | 구조화 검색 스윕 생략 |
| `--verbose` | 상세 로그를 stderr에도 출력 |

## 7. 출력 구조

```
research/
└── 智元机器人/                        ← CJK 슬러그 보존, 결정적
    └── 2026-09-04_034201/            ← 실행마다 타임스탬프, 덮어쓰지 않음
        ├── metadata.json              단계 상태, 오류, 토큰 사용량, 집계
        ├── 01_entity_discovery.md     Stage 1, 사람·Claude 가독
        ├── 01_entity_discovery.json   Stage 1, 프로그램용 + 주입 내역
        ├── 02_sources.md              Stage 2, 모델 출력 원문
        ├── 02_sources.json            Stage 2 + 소스 색인 + 라벨 감사
        ├── 03_search_sweep.{md,json}  Baidu 구조화 검색 결과
        ├── 04_official_site.{md,json} 관방 기사 전문
        ├── 05_reposts.{md,json}       차단 소스의 읽을 수 있는 사본
        ├── 06_exchange_filings.{md,json}  공시 + PDF 직링크
        ├── 07_patents.{md,json}       CNIPA 특허
        ├── raw_stage1_response.json   무가공 API 응답
        ├── raw_stage2_response.json
        ├── raw_sources/               소스별 파일 (YAML front matter + 본문)
        └── logs/run.log
```

전부 UTF-8, `ensure_ascii=false`로 저장되어 중국어가 이스케이프되지 않음.

### 소스 레코드 형식

```yaml
---
source_id: "SOURCE_001"
title: "智元第10,000台通用具身机器人正式下线！"
publisher: null
publication_date: "2026-03-28"
source_platform: "WeChat Official Account（微信公众号）"
retrieval_url: "https://mp.weixin.qq.com/s/sANNvELq0lqDyl6aHs1MvA"
canonical_url: "https://mp.weixin.qq.com/s/sANNvELq0lqDyl6aHs1MvA"
url_type: "STABLE_WECHAT_ARTICLE_URL"
content_access_status: "URL_ONLY"
origin: "stage2_model_output"
derived:
  url_type_heuristic: "STABLE_WECHAT_ARTICLE_URL"
  wechat_url_form: "short_path"
  is_ephemeral: false
---
```

세 가지 원칙임.

1. **메타데이터를 만들어내지 않음.** 확보되지 않은 필드는 `null`이며, 빈 문자열이나 추측이 아님
2. **요약을 원문으로 표기하지 않음.** 검색 API는 페이지 요약을 반환하므로 검색 채널 레코드는 `SEARCH_SNIPPET_ONLY` 또는 `URL_ONLY`임. 강한 라벨은 모델 출력에서만 나오며 `verify_labels()`의 검증을 거침
3. **보고된 값을 재계산하지 않음.** `url_type`과 최종 집계는 모델이 보고한 그대로 보존하고, 기계적 판정은 `derived`에 별도 기록하여 다운스트림이 비교 가능하게 함. 여기에 `src=11` / `timestamp=` / `signature=` / `token=` URL을 canonical로 인정하지 않는 규칙과, 위챗 구식 쿼리형 링크(`/s?__biz=...`)를 canonical로 오판하지 않는 규칙이 포함됨

---

## 8. 측정된 한계

| 항목 | 상태 |
|---|---|
| **위챗 공중호 본문** | 취득 불가. 어떤 API로도 불가하며 우회하지 않음. `URL_ONLY` + 안정 canonical URL로 보존하여 사람이 브라우저로 열 수 있게 함 |
| **Baidu 전용 도메인의 fetch 성공률** | 낮음. `baijiahao`(JS 렌더링), `xueqiu`(WAF), `baike.baidu`(403) 등이 차단됨. 공급망 테스트에서 14건 시도 중 1건 성공. **Baidu 배타 계층과 봇 차단 계층이 상당 부분 겹침** |
| **fetch 성공 도메인** | 관방 사이트, 주류 매체 — `agibot.com.cn`(8/8), `news.qq.com`, `cnr.cn`, `sohu`, `leaderobot.com`, `stcn.com` |
| **CNIPA 특허** | Google Patents 비인증 엔드포인트가 버스트에 `503`을 반환하고 지속됨. 백오프 구현했으나 안정성 필요 시 EPO OPS(무료 키, CN 색인) 권장 |
| **Tencent provider** | 실제 크레덴셜 미검증 |
| **무료 LLM 출력 편차** | `glm-4.7-flash`가 실행마다 소스 블록 35~71건으로 변동하고, Prompt 2 후반부(`NEW_SEARCH_ANCHOR`, `Final Collection Summary`)를 준수하지 않는 경우가 있음. 유료 모델 사용 시 개선 예상 |

역할 분담이 이렇게 정리됨.

```
Baidu 검색   →  발견 + 스니펫 증거   ← 배타적 가치가 여기 있음
관방 크롤    →  전문                ← 본문은 여기서
cninfo      →  공시 PDF 직링크      ← 1차 문서는 여기서
```

Baidu에 전문까지 기대하지 않는 것이 맞음.

## 9. 테스트

```bash
python -m unittest discover tests -v
```

**130개**, 표준 라이브러리만 사용하며 네트워크·크레덴셜 불필요함.

검증 대상이 구현 세부가 아니라 보장 사항임 — Stage 2가 Stage 1 전문을 실제로 수신하는지, Stage 2 실패가 Stage 1을 보존하는지, 실행이 서로 덮어쓰지 않는지, 검색 결과가 원문으로 라벨되지 않는지, 서명된 搜狗 링크와 위챗 구식 쿼리형 링크가 canonical로 취급되지 않는지, 검증 페이지가 본문으로 저장되지 않는지, 링크 밀도 높은 제품 메뉴가 기사 본문을 이기지 않는지, 전재본이 원본 라벨을 덮어쓰지 않는지, 모델의 `VERBATIM_*` 주장이 근거 없을 때 강등되는지, 오타·누락 라벨이 정규화되는지, 중국어가 모든 왕복에서 보존되는지.

## 10. 범위 외

의도적으로 만들지 않은 것 — 다운스트림 경쟁 분석 시스템, 웹 프론트엔드, 데이터베이스, 태스크 큐.

소스 접근 원칙 — 자신을 밝히고 속도를 제한한 일반 요청에 콘텐츠를 제공하는 공개 페이지를 fetch하고, 회사 자체 뉴스룸을 크롤함. 인증 우회, CAPTCHA·봇 탐지 무력화, 위챗 접근통제 우회, 스니펫만 있는 상태에서 전문 조작은 하지 않음. 자동 접근을 차단하는 호스트는 `URL_ONLY` + 안정 canonical URL로 기록함.

---

상세 영문 문서는 [README.en.md](README.en.md) 참조.
