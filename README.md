# Deepdive — China Source Ingest

## 1. 이 시스템이 하는 일

중국 기업을 조사할 때 필요한 중국어 원본 소스를 자동으로 수집하고 저장하는 백엔드 파이프라인임.

이 시스템 자체가 기업을 분석하거나 결론을 내리지는 않음. 역할은 명확히 두 단계로 분리됨.

```
중국 로컬 소스 수집
        ↓
구조화된 Evidence Corpus 저장
        ↓
Claude Deep Research에서 분석
```

즉,

- **이 시스템**: 자료를 최대한 넓고 정확하게 수집·보존
- **Claude**: 수집된 자료를 읽고 비교·분석·해석

하는 구조임.

## 2. 왜 필요한가

일반적인 Claude / Google 기반 웹검색만으로는 중국 현지 정보가 충분히 잡히지 않음.

AgiBot(智元机器人)을 대상으로 테스트한 결과:

| 구분 | 발견 도메인 |
| --- | --- |
| Claude 웹검색 | 34개 |
| Baidu 검색 | 49개 |
| 양쪽에 모두 존재 | 10개 |
| **Baidu에서만 발견** | **39개 (79%)** |

특히 Baidu에서는 일반 글로벌 검색에서 잘 잡히지 않는 다음 정보가 발견됨.

- 중국 정부조달 정보
- 현지 기업정보 DB
- 공급망·입찰 정보
- 지방정부 자료
- 중국 증권사·금융 데이터
- 투자자 커뮤니티
- 중국 로컬 미디어

실제로 AgiBot 조사에서는 다음과 같은 정보까지 추가로 발견됨.

- 기존 웹검색에서 발견되지 않았던 법인
- 소재·부품 공급업체
- OEM 및 지분투자 관계
- 우수 공급업체
- 자회사 구조
- 대외 투자 기업
- 공급망 분류

따라서 핵심 목적은 단순히 검색 결과를 더 많이 가져오는 것이 아니라,

> **서양 검색 인덱스가 놓치는 중국 로컬 정보 계층을 확보하는 것**

임.

## 3. 전체 파이프라인

회사명 하나를 입력하면 크게 다음 순서로 동작함.

```
회사명 입력
   ↓
① 회사 실체 및 검색 키워드 파악
   ↓
② 중국 로컬 검색
   ↓
③ 실제 페이지 본문 확보
   ↓
④ 추가 소스 전수 수집
   ↓
⑤ Evidence Corpus로 저장
   ↓
Claude Deep Research
```

조금 더 구체적으로는 다음과 같음.

### Stage 1 — 회사 실체 파악

먼저 해당 기업을 조사하기 위한 기본 정보를 확보함.

예:

- 중국어 법인명
- 영문명
- 과거 사명
- 별칭
- 주요 제품
- 경영진
- 자회사
- 추가 검색어

이 단계에서 이후 검색에 사용할 회사 이름 Dictionary와 Search Query를 생성함.

### Stage 2 — 실제 소스 수집

Stage 1에서 얻은 이름과 검색어를 기반으로 중국 웹을 본격적으로 탐색함.

주요 채널:

- Baidu 검색
- 회사 공식 뉴스룸
- 거래소 공시
- 특허
- 정부·입찰 사이트
- 중국 로컬 미디어

Stage 1 결과는 요약하지 않고 그대로 Stage 2에 전달함. 따라서 중간 단계에서 정보가 손실되지 않도록 설계됨.

## 4. Baidu는 어떻게 사용하는가

Baidu 자체 공식 Search API는 현재 사용할 수 없기 때문에 SerpApi를 통해 Baidu 검색 결과를 가져오는 구조임.

검색 예시:

```
智元机器人 供应商
智元机器人 投资
智元机器人 子公司
智元机器人 代工
```

검색 결과만 저장하는 것이 아니라, 가능한 경우 해당 URL의 실제 본문까지 직접 가져옴.

결과는 두 종류로 구분함.

**FULL_TEXT** — 실제 페이지 본문까지 확보한 경우

- 제목
- URL
- 날짜
- 본문 전체

**SNIPPET_ONLY** — 사이트가 자동 접근을 차단하여 검색 결과의 짧은 설명만 확보한 경우

- 제목
- URL
- 검색 결과 Snippet

이 둘을 명확히 구분하여 Snippet을 원문처럼 취급하지 않도록 함.

## 5. 검색만 하지 않고 '전수 열거'도 하는 이유

일반적인 웹검색에는 한 가지 큰 한계가 있음.

> **이름을 알아야 검색할 수 있음.**

예를 들어 AgiBot의 특정 자회사 이름을 모른다면 그 자회사를 직접 검색할 수도 없음.

따라서 검색과 별도로 특정 데이터베이스는 전체 목록을 직접 열거함.

현재 주요 대상:

| 채널 | 수집 방식 |
| --- | --- |
| 회사 공식 뉴스룸 | 전체 기사 목록 순회 |
| 거래소 공시 | 해당 기업 관련 공시 전체 조회 |
| 특허 | 출원인 기준 전체 특허 조회 |

AgiBot 테스트에서는:

- 거래소 공시 **652건**
- 공식 뉴스 **62건**
- 특허 **272건**

을 확인함.

즉,

```
Enumeration → 몰랐던 이름과 사건 발견
Search      → 발견한 항목을 더 깊게 조사
```

하는 보완 관계임.

## 6. 중국 사이트의 차단 문제

중국 웹에서는 자동 접근을 막는 사이트가 많음.

대표적으로:

- WeChat
- Baijiahao
- Xueqiu
- 일부 Baidu 서비스

등이 있음.

이 시스템은 CAPTCHA나 접근통제를 우회하지 않음. 대신 세 가지 방식으로 처리함.

### ① URL만 보존

본문을 읽지 못해도 해당 자료가 존재한다는 사실과 URL을 남김.

```
CONTENT_ACCESS_STATUS = URL_ONLY
```

### ② 다른 곳에 재게시된 글 탐색

중국 기업 발표는 여러 플랫폼에 중복 게시되는 경우가 많음.

예를 들어:

```
WeChat 원본
   ↓ 접근 불가
Tencent News / 회사 홈페이지 / 언론사
   ↓
동일 내용의 읽을 수 있는 재게시본 발견
```

실제 테스트에서는 접근이 막힌 WeChat 글 5개 중 3개의 재게시본을 복구함.

### ③ 원본과 재게시본을 별도 관리

재게시본을 찾았다고 해서 원본을 읽은 것으로 취급하지 않음.

```
WeChat 원본  → URL_ONLY
재게시 기사  → 별도 Source Record
```

으로 저장함.

## 7. 잘못된 데이터를 저장하지 않기 위한 검증

웹페이지를 가져왔다고 해서 모두 정상적인 기사 본문은 아님.

예를 들어 어떤 사이트에서는 실제 기사 대신 WAF 인증용 코드나 Base64 데이터가 반환될 수 있음.

따라서 다음을 자동으로 검사함.

- CAPTCHA / WAF 페이지 여부
- JavaScript Challenge 여부
- 비정상 Base64 데이터 여부
- 중국어 페이지인데 실제 중국어 본문이 거의 없는지
- 메뉴·링크 영역을 기사 본문으로 잘못 추출했는지

검증에 실패하면 본문으로 저장하지 않음.

## 8. LLM이 잘못 붙인 Evidence Label도 다시 검증

LLM이 검색 Snippet만 보고도 이를 원문이라고 잘못 표시하는 경우가 있음.

따라서 모델의 판단을 그대로 신뢰하지 않고 파이프라인에서 다시 확인함.

예:

```
실제로 시스템이 가진 것
→ 검색 Snippet 100자

LLM 주장
→ VERBATIM_PARTIAL_TEXT

최종 파이프라인
→ SEARCH_SNIPPET_ONLY로 강등
```

원칙은 간단함.

> **증거 수준을 높여서 추정하지 않고, 실제로 확보한 수준까지만 인정**

함.

## 9. 최종적으로 저장되는 자료

기업별로 한 번 조사할 때 다음과 같은 폴더가 생성됨.

```
research/
└── 智元机器人/
    └── 실행시간/
        ├── metadata.json
        ├── 01_entity_discovery.md
        ├── 02_sources.md
        ├── 03_search_sweep.md
        ├── 04_official_site.md
        ├── 05_reposts.md
        ├── 06_exchange_filings.md
        ├── 07_patents.md
        ├── raw_sources/
        └── logs/
```

크게 보면 세 종류임.

**Company Dictionary** — 회사명·법인명·제품·인물·별칭 등

**Source Corpus** — 기사·공시·특허·정부자료·공급망 자료 등

**Metadata** — 어디서 가져왔는지, 원문인지 Snippet인지, 접근 실패 여부 등

이 자료들을 이후 Claude가 직접 읽게 하는 구조임.

## 10. Source Record의 핵심 원칙

각 자료에는 출처와 Evidence Level이 함께 저장됨.

예:

```yaml
title: 智元第10,000台通用具身机器人正式下线！
source_platform: WeChat
retrieval_url: https://mp.weixin.qq.com/...
content_access_status: URL_ONLY
```

세 가지 원칙을 지킴.

### ① 모르는 정보는 만들지 않음

날짜나 Publisher를 확인하지 못했다면 추측하지 않고 `null` 처리.

### ② Snippet을 원문이라고 하지 않음

검색 결과에 나온 짧은 설명과 실제 페이지 본문을 구분.

### ③ 원본 데이터와 기계적 판단을 분리

원래 수집된 정보는 그대로 보존하고, 자동 판정 결과는 `derived` 필드에 별도로 기록.

## 11. 실패해도 이미 수집한 자료는 보존

중간 단계 하나가 실패했다고 전체 실행을 버리지 않음.

예:

- Stage 2 실패 → Stage 1 결과 보존
- 검색 Query 하나 실패 → 나머지 검색 계속
- API Rate Limit → 재시도
- 일부 사이트 Fetch 실패 → URL과 실패 이유 저장

따라서 한 사이트의 오류 때문에 전체 리서치가 중단되지 않도록 설계됨.

## 12. 현재 한계

**WeChat** — 자동으로 본문을 가져오기 어려움. → URL 보존 + 재게시본 탐색

**Baidu 검색 결과 사이트** — 중국 로컬 정보 발견에는 매우 유용하지만 자동 본문 접근 성공률은 낮음. 따라서 Baidu의 역할은 주로 **새로운 Source와 Entity 발견**임.

**공식 사이트** — 본문 확보에 가장 안정적임. **실제 전문 확보**에 활용.

**거래소 공시** — 기업의 법적·재무적 관계를 확인할 수 있는 가장 강한 1차 자료 중 하나임. **공식 Evidence 확보**에 활용.

정리하면:

```
Baidu       → 새로운 정보 발견
공식 사이트  → 실제 본문 확보
거래소 공시  → 강한 1차 증거 확보
특허        → 기술·R&D 관계 확인
```

## 13. 이 시스템의 핵심 가치

이 프로젝트의 목적은 중국 인터넷 전체를 완벽하게 크롤링하는 것이 아님.

핵심은 세 가지임.

**1. 중국 로컬 정보 발견** — Claude나 Google이 놓치는 중국 로컬 Source 확보.

**2. Evidence를 가능한 그대로 보존** — 요약본과 원문, 접근 성공과 실패를 명확히 구분.

**3. Claude Research의 입력 데이터 확장** — Claude가 기존 웹검색뿐 아니라 사전에 구축된 China Local Evidence Corpus까지 함께 읽을 수 있도록 하는 것.

최종 구조는 다음과 같음.

```
            기존 글로벌 Web Search
                     │
                     │
                     ▼
기업 입력 → China Source Ingest → Local Evidence Corpus
                                     │
                                     ▼
                              Claude Deep Research
                                     │
                                     ▼
                         경쟁사 / 공급망 / 기술 분석
```

즉,

> **Claude를 대체하는 리서치 시스템이 아니라, Claude가 기존에는 접근하기 어려웠던 중국 로컬 정보를 공급하는 데이터 레이어**

라고 이해하면 가장 간단함.

---

## 14. 설치 및 실행

> 처음 쓰는 사람은 **[HANDOFF.md](HANDOFF.md)** 를 볼 것.
> 클론부터 Claude 딥다이브까지 전 과정이 단계별로 정리되어 있음.

### 설치

```bash
git clone git@github.com:seungbeenjeon-rlwrld/Deepdive-china-source-ingest.git
cd Deepdive-china-source-ingest
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Python 3.10 이상 필요함.

### API 키 설정

```bash
cp .env.example .env
```

**검색**과 **프롬프트 실행**은 서로 다른 도구가 담당함. 둘 다 필요함.

### 검색 — SerpApi (Baidu)

| 변수 | 발급 |
| --- | --- |
| `SERPAPI_KEY` | [serpapi.com/manage-api-key](https://serpapi.com/manage-api-key) — 이메일 가입, 월 250건 무료, 카드 불필요 |

키를 여러 개 넣으면 한 키의 월 한도가 소진될 때 자동으로 다음 키로 넘어감.
소진된 키는 같은 실행 안에서 다시 시도하지 않음.

```
SERPAPI_KEY=키1
SERPAPI_KEY_2=키2
SERPAPI_KEY_3=키3
```

번호 읽기는 **첫 공백에서 멈춤** — `SERPAPI_KEY_2` 없이 `SERPAPI_KEY_4` 만 넣으면
4번은 무시됨. 오타가 조용히 넘어가지 않게 하기 위함.

> 무료 한도는 **계정당** 월 250건임. 계정을 여러 개 만들어 그 한도를 넘기는 것은
> SerpApi 약관과 충돌할 수 있으므로 확인이 필요함. 로테이션 기능 자체는
> 팀원별 키를 함께 쓰는 용도로도 동작함.

### 프롬프트 실행 — 아래 중 하나

| provider | 준비 | 특징 |
| --- | --- | --- |
| **`claude-cli`** (기본) | `curl -fsSL https://claude.ai/install.sh \| bash` | **API 키 불필요.** 대화형 Claude Code와 사용량 한도를 공유함 |
| `zhipu` | `ZHIPU_API_KEY` — [z.ai](https://z.ai/manage-apikey/apikey-list) | 별도 한도, 무료 모델 있음. 혼잡 시 `429` 재시도 필요 |
| `tencent` | `TENCENT_SECRET_ID` / `TENCENT_SECRET_KEY` | 위챗 커버리지 최상이나 **중국 신분증·법인 필요**. 미검증 |

기본값은 `claude-cli` 임. 발급받을 키가 SerpApi 하나로 줄고, 딥다이브 단계에서
어차피 Claude Code가 필요하므로 도구가 하나로 통일됨.

### 실행

인자 없이 실행하면 필요한 값을 순서대로 물어봄. 이게 기본 사용법임.

```bash
python research.py
```

```
조사할 회사명을 입력하세요.
> 智元机器人

추가 수집 채널 (선택) — 모르면 Enter 로 건너뛰세요.
1) 공식 뉴스룸 목록 URL
> https://www.agibot.com.cn/article/315
2) 거래소 공시 조회용 상장사명
> 上纬新材
3) 특허 출원인 법인명
> 上海智元新创技术有限公司
```

스크립트로 돌릴 때는 플래그로 지정함 (이때는 묻지 않음):

```bash
python research.py --company "智元机器人" --official-site "..." --filings "..."
python research.py --company "X" --provider mock   # 오프라인 테스트, 키 불필요
```

전수 열거 채널까지 사용:

```bash
python research.py --company "智元机器人" \
  --official-site "https://www.agibot.com.cn/article/315" \
  --filings "上纬新材" \
  --patents "上海智元新创技术有限公司"
```

### 주요 옵션

| 플래그 | 내용 |
| --- | --- |
| `--company NAME` | 대상 회사. 생략 시 입력 요청 |
| `--stage {1,2,all}` | 실행 단계. 기본 `all` |
| `--resume RUN_DIR` | 해당 실행의 Stage 1 결과를 재사용 |
| `--official-site URL` | 공식 뉴스룸 전체 순회 |
| `--filings LISTED_NAME` | 거래소 공시 전체 조회 (예: `上纬新材`) |
| `--patents ASSIGNEE` | 특허 전체 조회 (예: `上海智元新创技术有限公司`) |
| `--provider {tencent,zhipu,serpapi,mock}` | provider 강제 지정 |
| `--no-reposts` / `--no-search-sweep` | 해당 단계 생략 |
| `--verbose` | 상세 로그 출력 |

### 회사별 설정

회사마다 세 가지 값을 `config.yaml`에 넣으면 매번 플래그를 쓰지 않아도 됨.

```yaml
official_site:
  index_url: "https://www.agibot.com.cn/article/315"   # 공식 뉴스룸
registries:
  filings_search_key: "上纬新材"                        # 상장사명 (있는 경우)
  patent_assignee: "上海智元新创技术有限公司"             # 특허 출원인 법인명
```

> 조사 대상 회사 목록을 저장소에 두고 싶지 않으면 `config.local.yaml`로 분리하고 `.gitignore`에 추가하면 됨.

### 테스트

```bash
python -m unittest discover tests -v
```

130개, 표준 라이브러리만 사용하며 네트워크·API 키 불필요함.

---

## 문서

| 문서 | 내용 |
| --- | --- |
| **[HANDOFF.md](HANDOFF.md)** | **처음 쓰는 사람용 전체 절차** — 클론 → 키 발급 → 수집 → Claude 딥다이브 |
| [CLAUDE.md](CLAUDE.md) | Claude가 코퍼스를 읽을 때의 규칙. 저장소에서 Claude Code를 열면 자동 적용됨 |
| [README.en.md](README.en.md) | 기술 세부 구현, 측정 근거, 엔드포인트 스펙 |
| `prompts/` | 수집 단계용 프롬프트 2개 (분석용이 아님) |
