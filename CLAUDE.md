# 이 저장소에서 리서치할 때 읽을 것

이 파일은 **Claude가 이 저장소에서 작업할 때 자동으로 읽는 지침**임.
`research/` 아래에 수집된 중국 로컬 소스를 읽고 심층 분석할 때 반드시 아래 규칙을 따를 것.

## 이 저장소의 역할 분담

```
china-source-ingest (이 저장소)  →  수집·보존만. 분석하지 않음
Claude (지금 이 세션)             →  수집된 것을 읽고 분석
```

`prompts/` 안의 두 프롬프트는 **수집 단계용**임. 분석할 때 이 프롬프트를 따르지 말 것.
그것들은 수집기에게 "판단하지 말고 보존하라"고 지시하는 문서임.

---

## 1. 코퍼스 위치와 구조

```
research/{회사명}/{실행시각}/
├── metadata.json              실행 상태, 실패 사유, 집계 — 먼저 이걸 읽을 것
├── 01_entity_discovery.md     회사 실체·별칭 사전 (사람 가독)
├── 01_entity_discovery.json   같은 내용 + 주입된 검색 내역
├── 02_sources.md              수집 모델의 출력 원문
├── 02_sources.json            소스 색인 + 라벨 감사 결과
├── 03_search_sweep.json       Baidu 구조화 검색 결과
├── 04_official_site.json      공식 뉴스룸 기사 전문
├── 05_reposts.json            차단된 소스의 재게시본
├── 06_exchange_filings.json   거래소 공시 + PDF 직링크
├── 07_patents.json            특허
├── raw_sources/source_NNN.md  소스 1건 = 파일 1개 (YAML front matter + 본문)
└── logs/run.log
```

### 읽는 순서

1. **`metadata.json`** — 어느 단계가 성공/실패했는지. `stage2_status: failed`면 `02_sources`는 불완전함
2. **`01_entity_discovery.md`** — 회사의 법인명·별칭·자회사·제품. 이후 모든 검색·대조의 기준
3. **`raw_sources/`** — 개별 증거. 전문 검색(`grep`)으로 필요한 것만 열 것
4. 채널별 요약이 필요하면 `04`~`07`의 `.json`

**전체를 읽지 말 것.** 회사 1개 코퍼스가 컨텍스트 창을 넘김(실측 약 356,000자). `grep`으로 찾아서 해당 파일만 열 것.

```bash
# 회사명이 CJK면 경로를 반드시 인용부호로 감쌀 것 (zsh에서 glob이 깨짐)
RUN="research/智元机器人/2026-09-04_120000"

# 공급업체 관련 소스 찾기
grep -rl "供应商\|供应链" "$RUN/raw_sources/"

# 전문이 확보된 소스만 (가장 신뢰도 높은 것부터 보고 싶을 때)
grep -l 'content_access_status: "VERBATIM_FULL_TEXT"' "$RUN"/raw_sources/*.md

# 거래소 공시만 (1차 증거)
grep -l 'source_type: "Government / Regulatory / Exchange Disclosure"' "$RUN"/raw_sources/*.md

# 수집 모델이 라벨을 과장했다가 강등된 소스 (보수적으로 다룰 것)
grep -l "label_claimed" "$RUN"/raw_sources/*.md
```

---

## 2. 증거 등급 — 가장 중요한 규칙

모든 소스에 `content_access_status`가 붙어 있음. **이것이 그 자료를 얼마나 신뢰할 수 있는지를 결정함.**

| 값 | 의미 | 분석에서 쓸 수 있는 방식 |
| --- | --- | --- |
| `VERBATIM_FULL_TEXT` | 실제 본문 전문 확보 | 인용 가능. 숫자·날짜·발언 그대로 사용 가능 |
| `VERBATIM_PARTIAL_TEXT` | 본문 일부만 확보 | 확보된 범위 내에서 인용 가능 |
| `TRANSCRIPT_EXTRACTED` | 영상 자막/ASR | 발언으로 인용 시 자막 출처임을 밝힐 것 |
| `HIGH_FIDELITY_EXTRACTION` | 읽었으나 전문 보존 실패 | 숫자·이름은 신뢰. **원문 인용으로 쓰지 말 것** |
| `SEARCH_SNIPPET_ONLY` | 검색 요약 몇 줄만 | **사실 확인의 단서로만.** 근거로 단독 사용 금지 |
| `URL_ONLY` | URL만, 본문 못 읽음 | **내용을 추측하지 말 것.** "해당 자료가 존재한다"까지만 |

### 절대 하지 말 것

- `SEARCH_SNIPPET_ONLY`나 `URL_ONLY` 자료의 내용을 **추론해서 서술하지 말 것**. 제목만 보고 본문을 짐작하는 것이 가장 흔한 오류임
- 여러 스니펫을 합쳐 하나의 확정된 사실로 만들지 말 것
- `content`가 `null`인 소스를 근거로 주장하지 말 것

### `derived` 필드는 기계 판정이고 별개임

```yaml
url_type: "STABLE_WECHAT_ARTICLE_URL"        ← 수집 모델이 보고한 값
derived:
  url_type_heuristic: "TEMPORARY_SESSION_URL" ← 코드가 기계적으로 판정한 값
  label_claimed: "VERBATIM_PARTIAL_TEXT"      ← 모델이 주장했다가 강등된 라벨
  label_downgrade_reason: "..."               ← 강등 사유
```

**둘이 다르면 그 자체가 정보임.** `label_claimed`가 있는 소스는 수집 모델이 과장했고 파이프라인이 바로잡은 것임. 그런 소스는 더 보수적으로 다룰 것.

---

## 3. 소스 신뢰 순위

동일한 사실에 대해 여러 소스가 있으면 아래 순서로 우선함. `extra.source_priority`에 명시된 경우가 많음.

| 순위 | 소스 | 코퍼스 내 위치 |
| --- | --- | --- |
| 1 | 거래소·정부 공시 | `06_exchange_filings.json`, `origin: exchange_filing_registry` |
| 2 | 회사 공식 자료 | `04_official_site.json`, `origin: official_site_crawl` |
| 3 | 특허·논문 | `07_patents.json`, `origin: patent_registry` |
| 4 | 고품질 산업·경제 매체 | `origin: provider_search` 중 언론사 도메인 |
| 5 | 재게시본 | `05_reposts.json`, `origin: repost_resolution` |
| 6 | 검색 스니펫 | `origin: provider_search`, `SEARCH_SNIPPET_ONLY` |

**재게시본 주의.** `05_reposts.json`의 자료는 원본이 차단되어 다른 곳에서 가져온 것임.
`extra.reposts_source_id`가 원본 레코드를, `extra.original_url`이 원본 URL을 가리킴.
**재게시본의 전문은 재게시본의 것이지 원본의 것이 아님.** 표현이 다를 수 있으므로 원본 발언으로 인용하지 말 것.

---

## 4. 분석 결과를 쓸 때

### 반드시 출처를 붙일 것

```
智元은 2025년 8월 富临精工과 수천만元 규모 계약을 체결했고 远征A2-W 약 100대를
배치할 예정임 [SOURCE_019, SEARCH_SNIPPET_ONLY — 금액·수량은 스니펫 기준이며
1차 확인 필요]
```

`source_id`와 증거 등급을 함께 표기할 것. 등급을 숨기면 다음 사람이 검증할 수 없음.

### 확인되지 않은 것은 확인되지 않았다고 쓸 것

코퍼스에는 의도적으로 **해결하지 않은 모순**이 들어 있음. 예:

- 융자 라운드가 소스에 따라 9회/10회
- 卧龙电驱가 협력사인지 피투자사인지 엇갈림
- 등기 주소와 실제 사무실 주소 불일치

이런 것은 **한쪽을 골라 서술하지 말고 양쪽을 제시할 것.** 수집 단계에서 판정을 유보한 것은 근거가 부족했기 때문임.

### 코퍼스에 없는 것은 코퍼스에 없다고 쓸 것

`02_sources.md` 끝의 `REMAINING_SOURCE_GAPS`와 각 `.json`의 `failures` / `fetch_failures`에
무엇을 못 얻었는지 기록되어 있음. 분석에서 그 부분을 기억으로 채우지 말 것.

특히 **위챗 공중호 본문은 대부분 확보되지 않았음** (`URL_ONLY`).
위챗 자료를 근거로 서술할 때는 URL만 있는 상태임을 밝힐 것.

---

## 5. 코퍼스를 갱신하고 싶을 때

분석 중 자료가 부족하면 새로 수집할 것. 직접 웹검색으로 메우기보다 파이프라인을 쓰는 것이 좋음
— 그래야 증거 등급이 붙고 다음 사람도 같은 자료를 볼 수 있음.

```bash
# 같은 회사 재수집 (기존 실행은 덮어쓰지 않음)
python research.py --company "智元机器人" \
  --official-site "https://www.agibot.com.cn/article/315" \
  --filings "上纬新材"

# Stage 1은 재사용하고 Stage 2만 다시
python research.py --resume research/智元机器人/2026-09-04_034201 --stage 2
```

새 회사를 조사하려면 세 값이 필요함 — 공식 뉴스룸 URL, 상장사명(있으면), 특허 출원인 법인명.
`01_entity_discovery.md`의 법인명 항목에서 세 번째 값을 찾을 수 있음.

---

## 6. 코드를 수정할 때

- 테스트를 먼저 확인할 것: `python -m unittest discover tests`
- 새 provider는 `ResearchProvider` 서브클래스 + `build_provider()` 등록
- 새 저장 대상은 `StorageBackend` 서브클래스
- **증거 라벨 규칙을 완화하지 말 것.** `verify_labels()`가 모델의 과장을 강등하는 것은 실측된 문제
  (스니펫 100자를 `VERBATIM_PARTIAL_TEXT`로 라벨한 사례)에 대한 대응임
- 자동 접근을 차단하는 호스트를 우회하는 코드를 추가하지 말 것. `GATED_HOSTS` 참조
