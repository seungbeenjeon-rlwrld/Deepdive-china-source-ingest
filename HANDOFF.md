# Deepdive China Source Ingest 사용설명서

중국 기업의 **중국 로컬 소스를 자동으로 수집하고, Claude Code가 해당 자료를 기반으로 Deep Dive 분석을 수행할 수 있도록 준비하는 전체 과정**입니다.

전체 흐름은 다음과 같습니다.

**Repository Clone → 환경 설정 → API Key 등록 → 기업명 입력 → 중국 로컬 소스 수집 → Claude Deep Dive**

---

# 0. 사전 준비

아래 환경이 필요합니다.

| 항목             | 확인                  |
| -------------- | ------------------- |
| Python 3.10 이상 | `python3 --version` |
| Git            | `git --version`     |
| Claude Code    | 파이프라인 실행 및 Deep Dive 단계에서 사용 |
| SerpApi Key    | Baidu 검색에 사용        |

---

# 1. Repository 설치

```bash
git clone https://github.com/seungbeenjeon-rlwrld/Deepdive-china-source-ingest.git
cd Deepdive-china-source-ingest
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

설치 확인:

```bash
python -m unittest discover tests
```

아래처럼 나오면 정상입니다.

```text
OK (217 tests)
```

이 테스트는 네트워크와 API Key가 필요하지 않습니다.

---

# 2. Claude Code 설치 및 로그인

이 파이프라인의 언어모델 추론은 **Claude Code CLI**가 담당합니다.

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

PATH 경고가 나오면:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

처음 한 번 실행해 로그인합니다.

```bash
claude
```

* Folder trust → `Yes, I trust this folder`
* 브라우저 로그인 진행
* 완료 후 `/exit`

설치 확인:

```bash
claude --version
```


---

# 3. SerpApi Key 발급

SerpApi는 **Baidu 검색**에 사용됩니다.

### 발급

1. 회원가입
   https://serpapi.com/users/sign_up
2. API Key 확인
   https://serpapi.com/manage-api-key

무료 플랜 기준 월 **250 requests**이며, 회사 한 곳당 대략 **15~25 requests**가
사용됩니다. 실측(Unitree 전체 실행) 기준 14 requests였습니다.

Baidu 검색이 중국어 회사명 탐색의 근거가 되고, 그 결과가 공시·특허 채널의 검색어가 됩니다.

---

## `.env` 설정

```bash
cp .env.example .env
```

`.env` 파일에:

```text
SERPAPI_KEY=본인_API_KEY
```

를 입력합니다.

여러 Key를 사용할 경우:

```text
SERPAPI_KEY=키1
SERPAPI_KEY_2=키2
SERPAPI_KEY_3=키3
```


---

# 4. 설치 확인

실제 API 사용 없이 mock으로 파이프라인을 확인할 수 있습니다.

```bash
python research.py --company "TestCorp" --provider mock
```

정상 실행되면:

```text
research/testcorp/{실행시각}/
```

폴더가 생성됩니다.

`--provider mock`은 완전히 오프라인으로 동작하므로 SerpApi 요청을 소모하지
않습니다. 테스트 결과는 합성 데이터이므로 확인 후 삭제합니다.

```bash
rm -rf research/testcorp
```

---

# 5. 실제 기업 조사

실행:

```bash
python research.py
```

회사명을 입력합니다.

```text
========================================
 Deepdive — China Source Ingest
========================================

조사할 회사명을 입력하세요.
> Unitree
```

**영문 회사명 하나만 입력하면 됩니다.**
중국어 브랜드명, 법인명, 특허 출원인명, 상장사명 등은 파이프라인이 자동으로 탐색합니다.

---

## 자동화 파이프라인

파이프라인은 입력된 회사명을 기준으로:

1. 중국어 회사명 및 법인명 탐색
2. 중복 법인명 분리
3. Baidu 기반 중국 로컬 소스 검색
4. 관련 페이지 원문 수집
5. 거래소 공시 검색 **및 1차 문서 전문 추출**
6. 특허 출원인 탐색 및 특허 전수 조회 (구 사명 포함, 법인명 전부)
7. 중국 로컬 도메인 조회 (정부조달·工商 등기)
8. 수집 결과 저장

을 자동으로 수행합니다.


```text
[0/2] Resolving Chinese names...
      8 search names (6 Chinese), 3 name collision(s)
      injected 60 results from serpapi (4 pages read in full)
[1/2] Discovering company entities...
✓ Entity discovery complete
✓ Results saved
  자동 인식 — 거래소 공시: 宇树科技
  자동 인식 — 특허 출원인: 杭州宇树科技股份有限公司
[2/2] Collecting Chinese local sources...
✓ Source collection complete
✓ Results saved
[+] Fetching exchange filings for 宇树科技...
✓ Indexed 19 exchange filings with direct PDF links
✓ Extracted 538,087 chars of filing text in 35 sections
[+] Fetching patents for 杭州宇树科技股份有限公司...
✓ Indexed 33 of 33 patents
[+] Searching 4 Chinese local domains for 宇树科技...
      ccgp.gov.cn: 19
      tianyancha.com: 19
      qcc.com: 20
      aiqicha.baidu.com: 20
✓ 78 results from Chinese local domains (0 read in full)
[+] Sweeping structured search over 6 recommended queries...
✓ Collected 38 structured search results

Research saved to:
./research/unitree/2026-09-07_112053/

Done.
```

위 기록은 특허 채널이 출원인 이름 하나만 조회하던 시점의 것입니다. 이후 법인명
전부를 조회하도록 바뀌었으므로 특허 건수는 더 많아집니다(실측 기준 구 사명
`杭州宇树科技有限公司` 단독으로 135건). 바뀐 뒤의 전체 실행은 Google Patents
rate limit 때문에 아직 재측정하지 못했습니다.

`ccgp.gov.cn`이 `0 read in full`인 것은 정상입니다. 해당 사이트가 `robots.txt`로
자동 수집을 금지하고 있고 이를 준수하기 때문입니다. 낙찰 금액은 색인된 스니펫에
포함되어 있습니다.

---

# 6. 결과 확인

수집 결과는:

```text
research/{회사}/{실행시각}/
```

에 저장됩니다. 실측 기준 구성은 다음과 같습니다.

```text
00_INDEX.md                  ← 여기부터 확인. 전체 source 목차
metadata.json                채널별 성공/실패 및 건수
00_name_resolution.md/.json  중국어 이름 8개 + 동명이인 3개
01_entity_discovery.md/.json Stage 1 — 회사 실체
02_sources.md/.json          Stage 2 — 수집 결과
03_search_sweep.md/.json     Baidu 검색 38건
06_exchange_filings.md/.json 공시 19건 + 전문 35조각 538,087자
07_patents.md/.json          특허 (법인명 전부 조회)
08_local_sources.md/.json    중국 로컬 도메인 78건 (조달·工商)
raw_sources/source_NNN.md    source 1건 = 파일 1개 (총 221건)
logs/run.log                 실행 로그
```

상태 확인:

```bash
cat research/unitree/*/metadata.json
```

`*_status`가:

```text
completed
```

이면 정상입니다. `completed_with_errors`는 일부 항목만 실패한 경우이며, 사유는
해당 채널의 `.json` 파일 `failures` 항목에 기록됩니다. 예를 들어 스캔 이미지로만
된 공시는 텍스트 레이어가 없어 추출에 실패하며, 링크는 유지됩니다.

중간에 실패하더라도 이미 수집된 자료는 유지됩니다.

---

# 7. Claude Deep Dive에서 사용하는 방법

수집이 끝나면 해당 폴더를 Claude Code가 읽도록 하면 됩니다.

먼저:

```text
00_INDEX.md
```

를 확인합니다.


---

## Evidence Grade 확인

각 source에는:

```text
content_access_status
```

가 붙습니다.

이 값에 따라 자료를 사용할 수 있는 수준이 다릅니다.

| 값                          | 사용 방법                   |
| -------------------------- | ----------------------- |
| `VERBATIM_FULL_TEXT`       | 원문 인용 및 사실 근거로 사용 가능    |
| `VERBATIM_PARTIAL_TEXT`    | 확보된 범위 내에서 사용           |
| `TRANSCRIPT_EXTRACTED`     | 자막 출처임을 밝히고 사용          |
| `HIGH_FIDELITY_EXTRACTION` | 사실 확인에 사용 가능, 직접 인용은 지양 |
| `SEARCH_SNIPPET_ONLY`      | 탐색 단서로만 사용              |
| `URL_ONLY`                 | 해당 자료의 존재만 확인 가능        |

공시 전문은 `HIGH_FIDELITY_EXTRACTION`입니다. 문장은 원문 그대로이지만 추출
과정에서 표 구조가 평면화되므로, 숫자와 이름은 신뢰하되 레이아웃은 신뢰하지
않는 것이 원칙입니다.

전체 규칙은:

```text
CLAUDE.md
```

에 정의되어 있습니다.
