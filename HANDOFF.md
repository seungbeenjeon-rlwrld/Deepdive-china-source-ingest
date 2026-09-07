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

> SerpApi Key는 반드시 **본인 계정으로 발급**하여 사용합니다.
> 무료 한도가 계정 단위이므로 다른 사람과 Key를 공유하지 않는 것을 권장합니다.

발급이 필요한 API Key는 **SerpApi 하나뿐**입니다. LLM 처리는 Claude Code CLI가
담당하며 자체 인증을 사용하므로 별도 Key가 없습니다.

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

이 파이프라인의 LLM 처리는 **Claude Code CLI**가 담당합니다.

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

> Claude Code와 동일한 사용량 한도를 공유합니다.
> 여러 회사를 연속 실행하면 usage limit에 걸릴 수 있습니다.

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

Key가 없으면 실행이 시작되지 않고 안내가 출력됩니다. Baidu 검색이 중국어 회사명
탐색의 근거가 되고, 그 결과가 공시·특허 채널의 검색어가 되기 때문입니다.

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

처럼 추가할 수 있습니다. 한 Key의 월 한도가 소진되면 자동으로 다음 Key를
사용합니다. 번호는 순서대로 읽고 **첫 공백에서 멈추므로**, `_2` 없이 `_3`만
넣으면 `_3`은 무시됩니다.

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

## 자동으로 수행되는 작업

파이프라인은 입력된 회사명을 기준으로:

1. 중국어 회사명 및 법인명 탐색
2. 동명이인 회사 분리
3. Baidu 기반 중국 로컬 소스 검색
4. 관련 페이지 원문 수집
5. 거래소 공시 검색 **및 1차 문서 전문 추출**
6. 특허 출원인 탐색 및 특허 전수 조회 (구 사명 포함, 법인명 전부)
7. 중국 로컬 도메인 조회 (정부조달·工商 등기)
8. 수집 결과 저장

을 자동으로 수행합니다.

실제 실행 기록입니다. 소요 시간 약 20분, 소스 221건, 본문 568,955자.

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

# 6. 자동 도출값 직접 지정

자동 탐색 결과가 정확하지 않은 경우 직접 지정할 수 있습니다.

```bash
python research.py --company "AgiBot" \
  --filings "上纬新材" \
  --patents "上海智元新创技术有限公司"
```

직접 입력한 값이 자동 도출 결과보다 우선합니다.

> 특허는 출원인 이름에 민감하지만 **수동 지정은 필요하지 않습니다.** 사명 변경으로
> 기록이 쪼개지는 문제(실측: `杭州宇树科技股份有限公司` 33건 /
> `杭州宇树科技有限公司` 135건)는 파이프라인이 Stage 0 이 찾은 **법인명 전부를
> 조회해 합치는 방식**으로 자동 처리합니다.

---

# 7. 결과 확인

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

# 8. 실패한 단계 재실행

전체를 처음부터 다시 실행하면 SerpApi 요청을 다시 소모합니다. 이미 성공한
단계는 그대로 두고 실패한 부분만 다시 실행하는 것이 좋습니다.

Stage 2만 다시 실행:

```bash
python research.py \
  --resume "research/unitree/2026-09-07_112053" \
  --stage 2
```

공시·특허·로컬 도메인 등 수집 채널만 다시 실행:

```bash
python research.py \
  --resume "research/unitree/2026-09-07_112053" \
  --stage channels
```

이미 완료된 Stage 2는 실수로 덮어쓰지 않도록 막혀 있습니다. 의도적으로 다시
만들려면 `--force`를 추가합니다.

---

# 9. Claude Deep Dive에서 사용하는 방법

수집이 끝나면 해당 폴더를 Claude Code가 읽도록 하면 됩니다.

먼저:

```text
00_INDEX.md
```

를 확인합니다.

이 파일에 전체 source가 정리되어 있으므로, **필요한 자료만 골라서 읽는 방식**을 권장합니다.
전체 corpus를 한 번에 읽으면 context가 너무 커질 수 있습니다.

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

Claude Code를 repository 안에서 실행하면 자동으로 적용됩니다.
별도 Deep Dive prompt에서는 다음 한 줄을 추가하면 됩니다.

```text
분석 전에 이 저장소의 CLAUDE.md 를 읽고 그 증거 등급 규칙을 따를 것.
```

---

# 10. 분석 시 권장 순서

```text
00_INDEX.md
↓
필요한 source 선별
↓
해당 Markdown 또는 JSON 확인
↓
부족한 경우 grep 검색
↓
Deep Dive 분석
```

주의사항:

* 같은 source의 `.md`와 `.json`을 둘 다 읽을 필요 없음
* `dup` 표시 source는 중복 자료이므로 건너뛰어도 됨
* 동명이인이 의심되면 `00_name_resolution.md` 확인
* 자료 간 숫자나 내용이 다르면 임의로 하나를 고르지 말고 병기
* 수집하지 못한 자료는 `REMAINING_SOURCE_GAPS` 및 `failures` 확인
* 공시 전문은 `extra.section_heading`으로 장을 고르고 `extra.page_start`로
  원본 PDF 페이지를 확인

---

# 11. 자료가 부족할 때

Deep Dive 중 자료가 부족하면 가능하면 임의 웹검색보다 **파이프라인을 다시 실행하는 것을 권장**합니다.

```bash
python research.py --company "AgiBot"
```

기존 결과를 덮어쓰지 않고 새로운 timestamp 폴더가 생성됩니다.
이렇게 하면 모든 자료에 동일한 evidence grade와 provenance가 유지됩니다.

---

# 12. Public Repository 사용 시 주의

이 repository는 public입니다.
조사 대상 기업 목록을 공개하고 싶지 않다면 local config를 사용합니다.

```bash
cp config.yaml config.local.yaml
echo "config.local.yaml" >> .gitignore
```

실행:

```bash
python research.py \
  --company "..." \
  --config config.local.yaml
```

---

# 13. 자주 발생하는 문제

| 증상                             | 해결                                    |
| ------------------------------ | ------------------------------------- |
| `No SerpApi key found`         | `.env`에 `SERPAPI_KEY` 등록              |
| `claude was not found on PATH` | Claude CLI 설치 및 PATH 설정               |
| `Not logged in`                | `claude` 실행 후 로그인                     |
| `usage limit`                  | Claude Code 사용량 제한. 이후 재실행            |
| `ENOTFOUND`                    | 네트워크/DNS 오류. 재실행                      |
| `429 monthly quota`            | SerpApi 무료 한도 소진                      |
| `Indexed 0 exchange filings`   | 비상장사 또는 상장사명 탐색 실패. `--filings` 직접 지정 |
| 공시 전문 `no text layer`          | 스캔 이미지 PDF. 링크는 유지되므로 직접 열람           |
| 특허 `throttled` + 일부 미조회        | Google Patents rate limit. 시간을 두고 `--stage channels` 재실행 |
| 특허 `503 throttled`             | Google Patents rate limit. 이후 재시도     |
| 로컬 도메인 `0 read in full`        | 정상. `robots.txt` 준수로 스니펫만 확보          |
| WeChat source가 `URL_ONLY`      | 정상. 자동 원문 확보 불가                       |
| 중국어 경로 오류                      | 경로를 `"research/智元机器人/..."`처럼 따옴표로 감싸기 |

---

# Quick Start

처음 사용하는 경우 아래 순서만 따라가면 됩니다.

```text
1. Repository Clone
↓
2. Python 환경 설치
↓
3. Claude Code 설치 및 로그인
↓
4. SerpApi Key 발급
↓
5. .env 설정
↓
6. Mock Test
↓
7. python research.py
↓
8. 영문 회사명 입력
↓
9. 자동 중국 로컬 소스 수집
↓
10. research/{company}/{timestamp}/ 확인
↓
11. 00_INDEX.md 확인
↓
12. Claude Code로 Deep Dive
```

핵심적으로 기억할 것은 세 가지입니다.

**1. 회사명은 영어 이름 하나만 입력하면 됩니다.**

**2. 분석 전 `CLAUDE.md`의 Evidence Grade 규칙을 반드시 따릅니다.**

**3. 자료가 부족하면 가능하면 파이프라인을 다시 실행해 동일한 방식으로 근거를 축적합니다.**
