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

`OK (229 tests)`가 나오면 정상임. 이 테스트는 네트워크·API 키가 필요 없음.

---

## 2. Claude Code CLI 설치 — LLM 담당, API 키 불필요

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

설치 후 PATH 에 추가할 것 (설치 스크립트가 경고를 냄):

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
```

처음이면 한 번 실행해 로그인할 것. 이후 파이프라인이 그 인증을 그대로 씀.

```bash
claude
```

폴더 신뢰 확인이 뜨면 `Yes, I trust this folder` 를 선택하고, 로그인 안내가 나오면
브라우저로 인증한 뒤 `/exit` 로 나올 것.

확인:

```bash
claude --version
```

> **유의** — 이 방식은 대화형 Claude Code 와 **같은 사용량 한도를 공유함**.
> 회사를 여러 개 연속으로 돌리면 본인 작업이 느려질 수 있음. 그럴 때는 잠시 뒤
> 다시 실행할 것.

## 3. API 키 발급 — SerpApi 하나

Baidu 검색용임. 발급받을 키는 이것뿐임.

| 키 | 하는 일 | 없으면 |
| --- | --- | --- |
| `SERPAPI_KEY` | **Baidu 검색** — 중국 로컬 소스 발견 | 중국 인덱스 접근 불가 |

### 3-1. SerpApi (Baidu 검색)

1. <https://serpapi.com/users/sign_up> — 이메일 가입, 카드 불필요
2. <https://serpapi.com/manage-api-key> 에서 키 복사
3. **월 250건 무료.** 회사 1개 수집에 약 10~30건 사용 → 월 8~25개 회사

키가 여러 개 있으면 `.env` 에 나란히 넣을 것. 한 키가 소진되면 자동으로 다음
키로 넘어감:

```
SERPAPI_KEY=키1
SERPAPI_KEY_2=키2
SERPAPI_KEY_3=키3
```

번호는 `SERPAPI_KEY_2`, `_3`, `_4` … 순서로 읽고 **첫 공백에서 멈춤.**
`_2` 를 비우고 `_3` 만 넣으면 `_3` 은 무시됨.

> 무료 한도는 계정당임. 한 사람이 계정을 여러 개 만들어 한도를 넘기는 것은
> SerpApi 약관과 충돌할 수 있음.

### 3-2. `.env` 작성

```bash
cp .env.example .env
```

`.env` 를 열어 한 줄만 채울 것:

```
SERPAPI_KEY=여기에
```

LLM 은 Claude CLI 가 담당하므로 다른 키는 필요 없음.

## 4. 동작 확인 (키 소모 없음)

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

## 5. 첫 실제 수집

### 5-1. 회사명 하나만 넣으면 됨

**영어(글로벌) 회사명 하나로 충분함.** 중국어 이름을 알 필요 없음.

```bash
python research.py
```

```
========================================
 Deepdive — China Source Ingest
========================================

조사할 회사명을 입력하세요.
> AgiBot
```

그 뒤로는 아무것도 묻지 않음.

### 5-2. 나머지는 파이프라인이 알아냄

수집 범위를 결정하는 값들을 **묻지 않고 도출함.**

| 값 | 어디서 나오는지 |
| --- | --- |
| 중국어 검색명 | Stage 0 이 영어 이름을 전개 |
| 특허 출원인 법인명 | Stage 0 의 `legal_entity` 이름 |
| 거래소 공시용 상장사명 | 후보 이름을 巨潮资讯网 에 조회해 공시가 나오는 것 |

Stage 0 이 필요한 이유는 영어 이름만으로 중국 인덱스를 검색하면 **동명이인
회사가 섞여 나오기 때문**임. 실측: Baidu 에서 `AgiBot` 을 검색하면 수술로봇
회사 `AGIBOT敏捷机器人` 이 함께 반환됨. Stage 0 이 그런 회사를 `collisions` 로
분리해 Stage 1 에 "합치지 말라"고 전달함.

상장사명은 추측하지 않고 **등기소에 실제로 조회해서** 정함. 중국어에는 띄어쓰기가
없어 문장에서 이름을 뽑으면 `客户集中度` 의 `户集中度` 같은 조각이 회사명으로
잡혔기 때문임. 조회 자체는 무료라 후보를 몇 개 시도해도 비용이 없음.

도출에 실패하면 그 채널만 건너뛰고 나머지는 정상 진행됨. 도출 근거는
`metadata.json` 의 `notes` 와 `00_name_resolution.md` 에 기록됨.

### 5-3. 실행 화면

10~20분 걸림. 이런 순서로 흘러감.

```
[0/2] Resolving Chinese names...
      8 search names (6 Chinese), 5 name collision(s)
      retrieval: 智元机器人
      reading: 以智能机器创造无限生产力-智元创新(上海)科技股份有限公司
      injected 24 results from serpapi (4 pages read in full)
[1/2] Discovering company entities...
✓ Entity discovery complete
✓ Results saved
  자동 인식 — 거래소 공시: 上纬新材
  자동 인식 — 특허 출원인: 上海智元新创技术有限公司
[2/2] Collecting Chinese local sources...
✓ Source collection complete
✓ Results saved
[+] Fetching exchange filings for 上纬新材...
✓ Indexed 20 exchange filings with direct PDF links

Research saved to:
./research/agibot/2026-09-05_120000/

Done.
```

값을 직접 지정하려면 플래그로 넘길 것. 플래그가 자동 도출을 덮어씀:

```bash
python research.py --company "AgiBot" \
  --official-site "https://www.agibot.com.cn/article/315" \
  --filings "上纬新材" \
  --patents "上海智元新创技术有限公司"
```

### 5-4. 결과 확인

```bash
ls research/agibot/*/
cat research/agibot/*/metadata.json
```

`metadata.json` 의 `*_status` 가 `completed` 인지 확인할 것. `failed` 가 있으면
`stage2_error` 등에 사유가 있음.

**중간에 실패해도 이미 수집된 것은 남음.**

Stage 2 가 실패했을 때만 다시 실행하려면:

```bash
python research.py --resume "research/agibot/2026-09-05_120000" --stage 2
```

**이미 완료된 Stage 2 는 덮어쓰지 않도록 막혀 있음.** 수집 채널만 추가하려면:

```bash
python research.py --resume "research/agibot/2026-09-05_120000" --stage channels
```

`--stage channels` 는 거래소 공시·특허·검색 스윕만 실행하고
Stage 1·2 는 건드리지 않음. Stage 2 를 의도적으로 다시 만들려면 `--force` 를
붙일 것.

## 6. 코퍼스를 다운스트림에 넘기기

이 도구의 일은 코퍼스를 만드는 데서 끝남. 분석은 각자의 deepdive 파이프라인이 함.

### 6-1. 산출물은 그냥 파일임

```
research/{회사}/{실행시각}/
```

Markdown 과 JSON 이라 무엇으로 읽든 상관없음. deepdive 프롬프트가 이 경로를
읽게 하면 됨.

### 6-2. 함께 넘겨야 하는 것 — 증거 등급 규칙

각 소스에 `content_access_status` 가 붙어 있음. **이걸 모르는 채로 분석하면
검색 스니펫을 원문처럼 다뤄 없는 사실을 만들어냄.**

| 값 | 분석에서 쓸 수 있는 방식 |
| --- | --- |
| `VERBATIM_FULL_TEXT` | 인용 가능. 숫자·날짜·발언 그대로 |
| `VERBATIM_PARTIAL_TEXT` | 확보된 범위 내에서 인용 가능 |
| `TRANSCRIPT_EXTRACTED` | 자막 출처임을 밝히고 인용 |
| `HIGH_FIDELITY_EXTRACTION` | 숫자·이름은 신뢰. 원문 인용으로 쓰지 말 것 |
| `SEARCH_SNIPPET_ONLY` | 단서로만. 근거로 단독 사용 금지 |
| `URL_ONLY` | 내용 추측 금지. "해당 자료 존재"까지만 |

전체 규칙은 저장소 루트의 [CLAUDE.md](CLAUDE.md) 에 있음. 가장 간단한 전달
방법은 deepdive 프롬프트 앞에 한 줄을 붙이는 것임:

```
분석 전에 이 저장소의 CLAUDE.md 를 읽고 그 증거 등급 규칙을 따를 것.
```

Claude Code 를 이 저장소 디렉터리에서 열면 `CLAUDE.md` 가 자동 적용되므로
그 한 줄도 필요 없음.

### 6-3. 읽을 때 알아둘 것

- **`00_INDEX.md` 를 먼저 열 것.** 전체 소스가 한 줄씩 정리된 목차임. 여기서
  필요한 것만 골라 해당 파일을 여는 것이 기본 사용법임
- **전체를 한 번에 읽지 말 것.** 회사 1개 코퍼스가 컨텍스트 창을 넘김. 목차로
  좁히고, 그래도 못 찾으면 `grep` 을 쓸 것
- **같은 소스의 `.json` 과 `.md` 를 둘 다 읽지 말 것.** 같은 내용임
- 목차에 `dup` 으로 표시된 것은 다른 소스와 사실상 같은 자료임. 건너뛰어도 됨
- 회사명이 CJK 면 경로를 인용부호로 감쌀 것 — `"research/智元机器人/..."`
- `00_name_resolution.md` 의 `collisions` 에 **동명이인 회사**가 정리되어 있음.
  이름이 헷갈리면 여기를 먼저 볼 것
- 코퍼스에는 **해결하지 않은 모순**이 의도적으로 남아 있음(융자 라운드 9회/10회
  등). 한쪽을 고르지 말고 병기할 것
- `02_sources.md` 끝의 `REMAINING_SOURCE_GAPS` 와 각 `.json` 의 `failures` 에
  무엇을 못 얻었는지 기록되어 있음

### 6-4. 자료가 부족할 때

deepdive 중 부족한 부분이 나오면 직접 웹검색으로 메우기보다 파이프라인으로
재수집하는 편이 좋음. 그래야 증거 등급이 붙고 다음 사람도 같은 자료를 봄.

```bash
python research.py --company "AgiBot"
```

기존 실행을 덮어쓰지 않고 새 타임스탬프 폴더가 생김.

## 7. 새 회사 추가

### 자동 도출을 고정하고 싶으면

보통은 회사명만 넣으면 되고 아래는 필요 없음. 자동 도출 결과가 계속 틀릴 때만
`config.yaml` 에 박아두면 됨:

```yaml
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

## 8. 자주 겪는 문제

| 증상 | 원인 / 대응 |
| --- | --- |
| `the Claude CLI ('claude') was not found on PATH` | CLI 미설치. 2절의 설치 명령 실행 |
| `claude cli failed ... Not logged in` | `claude` 를 한 번 실행해 로그인할 것 |
| `claude cli failed ... usage limit` | 구독 한도 소진. 시간을 두고 재실행 |
| `claude cli failed ... ENOTFOUND` | 네트워크·DNS 일시 오류. 재실행하면 됨 |
| `Indexed 0 exchange filings` | 비상장이거나 상장사명 도출 실패. `--filings "상장사명"` 으로 직접 지정 가능 |
| `429 monthly quota` (SerpApi) | 월 250건 소진. <https://serpapi.com/dashboard> 확인 |
| 공시 0건인데 회사는 상장사 | cninfo 504(일시적). 재시도 로직이 있으나 계속되면 잠시 후 다시 |
| 특허 0건 + `503 throttled` | Google Patents 스로틀. 시간 두고 재시도. 안정성 필요 시 EPO OPS 검토 |
| `Baidu hasn't returned any results` | 정상임. Baidu는 장문 다중키워드 쿼리에 빈 결과를 반환함 |
| 위챗 소스가 전부 `URL_ONLY` | 정상임. 자동 취득 불가이며 우회하지 않음. 브라우저로 직접 열 것 |
| CJK 경로 glob 오류 | 경로를 인용부호로 감쌀 것: `"research/智元机器人/..."` |

---

## 9. 더 읽을 것

| 문서 | 내용 |
| --- | --- |
| [README.md](README.md) | 시스템이 하는 일, 왜 필요한가, 파이프라인 개요 |
| [CLAUDE.md](CLAUDE.md) | **코퍼스 증거 등급 규칙.** deepdive 파이프라인에 그대로 전달할 것 |
| `prompts/` | 수집 단계용 프롬프트 2개. **분석용이 아님** |
