# 인수인계 — 처음 쓰는 사람을 위한 전체 절차

클론부터, Claude가 수집된 중국 소스를 기반으로 딥다이브하는 것까지의 전 과정임.
소요 시간 약 20분(대부분 API 키 발급 대기).

---

## 0. 사전 준비

| 필요한 것 | 확인 |
| --- | --- |
| Python 3.10 이상 | `python3 --version` |
| git | `git --version` |
| Claude Code | 딥다이브 단계에서 사용 |

API 키 2개를 **본인 계정으로 발급**해야 함. 다른 사람 키를 공유하지 말 것 —
무료 한도가 계정당이므로 공유하면 서로 소진시킴.

---

## 1. 클론 및 설치

```bash
git clone git@github.com:seungbeenjeon-rlwrld/Deepdive-china-source-ingest.git
cd Deepdive-china-source-ingest
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

설치 확인:

```bash
python -m unittest discover tests
```

`OK (130 tests)`가 나오면 정상임. 이 테스트는 네트워크·API 키가 필요 없음.

---

## 2. API 키 발급 — 2개, 성격이 다름

두 키의 역할이 다르므로 **둘 다** 필요함.

| 키 | 하는 일 | 없으면 |
| --- | --- | --- |
| `SERPAPI_KEY` | **Baidu 검색** — 중국 로컬 소스 발견 | 중국 인덱스 접근 불가 |
| `ZHIPU_API_KEY` | **프롬프트 실행** (LLM) — 실체 사전·소스 정리 | Stage 1·2 실행 불가 |

### 2-1. SerpApi (Baidu 검색)

1. <https://serpapi.com/users/sign_up> — 이메일 가입, 카드 불필요
2. <https://serpapi.com/manage-api-key> 에서 키 복사
3. **월 250건 무료.** 회사 1개 수집에 약 10~30건 사용 → 월 8~25개 회사

### 2-2. Z.ai (LLM)

1. <https://z.ai/model-api> — `Register or Login`, 이메일 가입 (중국 전화번호 불필요)
2. <https://z.ai/manage-apikey/apikey-list> → `Create API Key`
   - **생성 시 한 번만 전체가 보임.** 그때 복사할 것
3. 기본 모델은 `glm-4.7-flash`

> **주의:** 무료 티어는 혼잡할 때 `429`(코드 1305/1113/1302)를 비결정적으로 반환함.
> 파이프라인이 최대 7회 백오프 재시도하나, 계속 실패하면 시간을 두고 재시도하거나
> <https://z.ai/manage-apikey/billing> 에서 $5~10 충전할 것. 충전하면 안정성과
> 출력 품질이 모두 개선됨(회사당 몇 센트 수준).

### 2-3. `.env` 작성

```bash
cp .env.example .env
```

`.env`를 열어 두 줄을 채울 것:

```
SERPAPI_KEY=여기에
ZHIPU_API_KEY=여기에
```

`.env`는 `.gitignore`에 있어 커밋되지 않음. **절대 커밋하지 말 것.**

---

## 3. 동작 확인 (키 소모 없음)

먼저 mock으로 파이프라인이 도는지 확인:

```bash
python research.py --company "TestCorp" --provider mock
```

`research/testcorp/{시각}/` 이 생기고 파일들이 채워지면 정상임. 이건 합성 데이터이므로
결과 내용은 의미 없음 — **배관만 확인하는 용도**임.

확인 후 지울 것:

```bash
rm -rf research/testcorp
```

---

## 4. 첫 실제 수집

### 4-1. 회사별 세 값 준비

회사마다 아래 세 가지를 알면 수집 범위가 크게 넓어짐.

| 값 | 어디서 찾는지 | AgiBot 예시 |
| --- | --- | --- |
| 공식 뉴스룸 인덱스 URL | 회사 홈페이지 → 뉴스/新闻资讯 목록 페이지 | `https://www.agibot.com.cn/article/315` |
| 상장사명 (있으면) | 해당 기업이 지배하는 상장사 | `上纬新材` |
| 특허 출원인 법인명 | 중국어 정식 법인명 | `上海智元新创技术有限公司` |

**모르면 비워도 됨.** Stage 1이 법인명을 찾아주므로, 1차 실행 후
`01_entity_discovery.md`에서 확인하여 2차 실행에 넣는 방식이 편함.

### 4-2. 실행

```bash
python research.py --company "智元机器人" \
  --official-site "https://www.agibot.com.cn/article/315" \
  --filings "上纬新材" \
  --patents "上海智元新创技术有限公司"
```

10~20분 소요됨. 진행 상황이 터미널에 출력됨.

### 4-3. 결과 확인

```bash
ls research/智元机器人/*/
cat research/智元机器人/*/metadata.json
```

`metadata.json`의 `*_status`가 `completed`인지 확인할 것.
`failed`가 있으면 `stage2_error` 등에 사유가 있음.

**중간에 실패해도 이미 수집된 것은 남음.** Stage 2만 다시 하려면:

```bash
python research.py --resume "research/智元机器人/2026-09-04_120000" --stage 2
```

---

## 5. Claude로 딥다이브 — 여기가 본론

수집은 끝났고, 이제 Claude가 그 코퍼스를 읽고 분석하는 단계임.

### 5-1. 저장소 디렉터리에서 Claude Code를 열 것

```bash
cd Deepdive-china-source-ingest
claude
```

**중요:** 이 디렉터리에서 열어야 함. 저장소 루트의 `CLAUDE.md`를 Claude가 자동으로 읽고,
증거 등급을 어떻게 다뤄야 하는지(스니펫을 원문으로 취급하지 않기 등) 지침을 받음.

다른 위치에서 열거나 코퍼스 파일만 복사해 가면 그 지침이 적용되지 않아
**증거 등급을 무시한 분석이 나올 위험이 있음.**

### 5-2. 첫 질문 — 코퍼스 파악

```
research/智元机器人/ 아래 코퍼스를 파악해줘.
metadata.json과 01_entity_discovery.md를 먼저 읽고,
어떤 소스가 몇 건씩 어떤 증거 등급으로 있는지 정리해줘.
```

Claude가 전체를 읽지 않고 `grep`으로 탐색함(코퍼스 1개가 컨텍스트 창을 넘기므로).

### 5-3. 실제 분석 요청 예시

**공급망 분석**

```
research/智元机器人/ 코퍼스에서 공급업체·협력사 관계를 정리해줘.
각 항목에 source_id와 증거 등급을 붙이고,
SEARCH_SNIPPET_ONLY인 것은 1차 확인이 필요하다고 표시해줘.
```

**자본 구조**

```
06_exchange_filings.json 의 공시를 기준으로 智元과 上纬新材의 관계를 정리해줘.
공시는 1차 증거이므로 언론 보도와 내용이 다르면 공시를 우선하고,
차이가 있으면 그 차이를 명시해줘.
```

**기술 스택**

```
07_patents.json 과 04_official_site.json 을 함께 읽고
智元의 기술 스택을 정리해줘. 특허는 출원일 순으로,
공식 발표는 발표일 순으로 배열해줘.
```

**모순 정리**

```
코퍼스 안에서 소스 간 내용이 엇갈리는 항목을 찾아서 나열해줘.
어느 쪽이 맞는지 판정하지 말고, 각각의 근거와 증거 등급만 제시해줘.
```

**경쟁사 비교** (회사 2개 이상 수집한 뒤)

```
research/ 아래 수집된 회사들을 비교해줘.
양산 규모·고객·자금조달을 표로 정리하고,
비교 가능한 항목만 넣고 한쪽에 자료가 없으면 "자료 없음"으로 표시해줘.
```

### 5-4. Claude에게 요구할 것 / 요구하지 말 것

**요구할 것**

- 모든 주장에 `source_id` 표기
- 증거 등급 함께 표기
- 소스 간 모순은 판정하지 않고 병기
- 코퍼스에 없는 것은 "자료 없음"으로 명시

**요구하지 말 것**

- `URL_ONLY` 소스의 내용 추측 — 본문을 못 읽은 자료임
- 스니펫 여러 개를 합쳐 확정 사실로 만드는 것
- 위챗 자료 기반 서술 — 대부분 URL만 있음

`CLAUDE.md`가 이 규칙을 이미 지시하고 있으나, 명시적으로 다시 요구하면 더 확실함.

### 5-5. 자료가 부족할 때

분석 중 부족한 부분이 나오면, Claude에게 직접 웹검색시키지 말고
**파이프라인으로 재수집하는 편이 좋음.** 그래야 증거 등급이 붙고 다음 사람도 같은 자료를 봄.

```bash
python research.py --company "智元机器人" --official-site "..." --filings "..."
```

기존 실행을 덮어쓰지 않고 새 타임스탬프 폴더가 생김.

---

## 6. 새 회사 추가

### 매번 플래그를 쓰지 않으려면

`config.yaml`에 넣어두면 됨:

```yaml
official_site:
  enabled: true
  index_url: "https://www.example.com.cn/news"
registries:
  filings_search_key: "상장사명"
  patent_assignee: "중국어 법인명"
```

### 조사 대상 목록을 저장소에 남기고 싶지 않으면

이 저장소는 **public**임. 대상 회사 목록은 "우리가 누구를 보고 있는지"의 목록이므로,
공개하고 싶지 않으면 분리할 것:

```bash
cp config.yaml config.local.yaml   # 실제 타겟은 여기에
echo "config.local.yaml" >> .gitignore
python research.py --company "..." --config config.local.yaml
```

---

## 7. 자주 겪는 문제

| 증상 | 원인 / 대응 |
| --- | --- |
| `ZHIPU_API_KEY is not set` | `.env` 미작성 또는 저장소 루트가 아닌 곳에서 실행 |
| `429 ... 1305/1113/1302` 반복 | Zhipu 무료 티어 혼잡. 시간 두고 재시도하거나 소액 충전 |
| `429 monthly quota` (SerpApi) | 월 250건 소진. <https://serpapi.com/dashboard> 확인 |
| 공시 0건인데 회사는 상장사 | cninfo 504(일시적). 재시도 로직이 있으나 계속되면 잠시 후 다시 |
| 특허 0건 + `503 throttled` | Google Patents 스로틀. 시간 두고 재시도. 안정성 필요 시 EPO OPS 검토 |
| `Baidu hasn't returned any results` | 정상임. Baidu는 장문 다중키워드 쿼리에 빈 결과를 반환함 |
| 위챗 소스가 전부 `URL_ONLY` | 정상임. 자동 취득 불가이며 우회하지 않음. 브라우저로 직접 열 것 |
| CJK 경로 glob 오류 | 경로를 인용부호로 감쌀 것: `"research/智元机器人/..."` |

---

## 8. 더 읽을 것

| 문서 | 내용 |
| --- | --- |
| [README.md](README.md) | 시스템이 하는 일, 왜 필요한가, 파이프라인 개요 |
| [CLAUDE.md](CLAUDE.md) | **Claude가 코퍼스를 읽을 때의 규칙.** 딥다이브 전에 한 번 읽어볼 것 |
| [README.en.md](README.en.md) | 기술 세부 구현, 측정 근거, 엔드포인트 스펙 |
| `prompts/` | 수집 단계용 프롬프트 2개. **분석용이 아님** |
