# Report-System — 분양하마 현장분석 리포트 시스템

분양 현장의 가격·수요·공급·개발계획을 검증하여 **진단리포트를 생성하는 파이프라인**.
제안서(`proposal/`)의 제5장·부록 E에 기술된 방법론과 설계검토보고서(`docs/`)의
P0·P1 항목을 실행 코드로 구현한다. 외부 의존성 없음(Python 3.11+ stdlib).

## 실행

```bash
# 1) 샘플(합성 데이터) — 키 없이 즉시 실행
python3 -m report_system generate                  # → out/sample_report.md
python3 -m report_system generate --full           # 전 레이어를 켠 리포트 시연

# 2) 실데이터 — 공공데이터포털 인증키 필요
export DATA_GO_KR_API_KEY='발급받은_Decoding_키'
cp examples/site_config.json my_site.json          # 현장·비교단지 정보 입력
python3 -m report_system doctor --config my_site.json        # ★ 먼저 진단
python3 -m report_system live --config my_site.json          # → out/live_report.md
python3 -m report_system live --config my_site.json --offline # 캐시만 사용(재현 실행)

# 3) 검증
python3 -m report_system coverage   # 예측 이력 장부 적중률
python3 -m report_system backtest   # 백테스트 단독 실행 → out/backtest.md
python3 -m report_system calibrate --config my_site.json   # 조정계수 교정 → out/calibration.md
python3 -m report_system history --site SAMPLE-001   # 회차별 판정·지표 변화
python3 tests/run_all.py            # 전체 테스트 (293건)
```

### 실데이터 준비 절차

1. [공공데이터포털](https://www.data.go.kr) 가입 후 아래 API에 활용신청
   (모두 같은 인증키를 쓴다)
   - **국토교통부_아파트 매매 실거래가 자료** (E01) — 승인까지 수십 분
   - **국토교통부_아파트 전월세 실거래가 자료** (E01-R) — 전세가율 산출에 필요
   - **한국부동산원_청약홈 분양정보/경쟁률 조회** (E02) — odcloud 계열, 자동승인
   - (선택) 소상공인시장진흥공단 상권정보, 국토교통부 TAGO 정류소정보
2. 마이페이지에서 **일반 인증키(Decoding)** 복사 → `DATA_GO_KR_API_KEY`로 export
3. `examples/site_config.json`을 복사해 현장 정보 입력
   - `lawd_cd`: 법정동 코드 5자리 (예: 강남구 `11680`)
   - `comparables[].apt_nm`: **국토부 실거래 데이터의 단지명과 정확히 일치**해야 함.
     불일치 시 CLI가 해당 지역의 실제 단지명 목록을 오류 메시지로 안내한다.
   - `site.types[].base_price`: 분양가(원), `option_cost`: 유상옵션·확장비

### doctor — 실행 전 진단 (키 수령 직후 첫 명령)

`live` 실행 전에 설정·연결·데이터 가용성을 점검한다. API 호출은 엔드포인트당 1회.

```
[1] 설정 검사        필수 키·타입, 세대수 정합, 분양가 단위, 법정동 코드 5자리
[2] 선택 레이어      어떤 레이어가 켜지는지 + 지정한 파일이 실제로 있는지
[3] API 연결         인증키 인식, E01/E01-R/E02 실제 응답 건수
[4] 비교단지 매칭     설정의 apt_nm 이 실데이터에 존재하는지 + 유사 후보 제안
                     → 지역 단지명 전체를 out/apt_names.txt 로 저장
```

`--skip-api` 로 설정 검사만 수행할 수 있다. 실패 항목이 있으면 종료코드 1.

### 레이어별 커넥터 현황

| 레이어 | 커넥터 | 인증 | 설정 키 |
|--------|--------|------|---------|
| L11 매매 실거래 | `molit` (국토부 E01) | `DATA_GO_KR_API_KEY` | `lawd_cd`, `comparables` |
| L11 전월세 실거래 | `rent` (국토부 E01-R) | 〃 | `collect_rent`(기본 on), `rent_months` |
| L12 청약 | `applyhome` (청약홈 E02) | 〃 | `subscription_regions` |
| L12 미분양 | `unsold` (파일) | — | `unsold_file` |
| L13 주택건설실적 | `housing` (KOSIS 또는 파일) | `KOSIS_API_KEY` (API 경로만) | `kosis_housing` 또는 `housing_file` |
| L1·L2·L4 인구·가구·사업체 | `sgis` (통계청) | `SGIS_CONSUMER_KEY`/`SECRET` | `sgis_adm_cd`, `sgis_years` |
| L3 인구이동 | `migration` (KOSIS 또는 파일) | `KOSIS_API_KEY` (API 경로만) | `kosis_migration` 또는 `migration_file`, `region_population` |
| L7 생활이동·O/D | `mobility` (파일) | — | `mobility_file`, `mobility_focus`, `mobility_purpose` |
| L8 교통망·접근성 | `transit` (TAGO 정류소 + 역 좌표 파일) | `DATA_GO_KR_API_KEY` | `transit_radius_m`, `stations_file` |
| L9 상권 | `commerce` (소상공인공단) | `DATA_GO_KR_API_KEY` | `commerce_radius_m` |
| 매물·호가 | `listings` (파일) | — | `listings_file` |
| L6·L10 (유동·카드) | 민간 라이선스 별도 협의 | — | — |

선택 레이어는 설정 키가 없으면 **건너뛰고 리포트 커버리지표에 '미수집'으로 표기**된다.
`doctor` 의 `[2] 선택 레이어 가용성` 이 어떤 레이어가 켜지는지, 지정한 파일이
실제로 있는지를 먼저 알려준다.

레이어별로 반드시 병기되는 한계
- **L1·L2·L4** — SGIS 집계 단위가 시군구여서 생활권보다 해상도가 낮다.
- **L3** — `region_population` 미지정 시 순이동률 대신 총이동 대비 비중으로 판정한다.
- **L7** — KTDB·통신사 O/D는 계약·승인 자료로 갱신 주기가 길어 최근 개통·입주
  효과가 반영되지 않는다.
- **L8** — 직선거리에 보행 보정계수 1.3을 적용한 환산값이며, 배차 간격·환승
  편의·실제 보행 경로는 평가에 포함되지 않는다.

### 커넥터 동작

| 항목 | 내용 |
|------|------|
| 캐시 | 모든 응답을 `out/cache/`에 저장. 동일 요청은 재호출하지 않으며 `--offline`로 재현 실행 |
| 재시도 | 네트워크 오류 시 3회, 지수 백오프 |
| 수집 이력 | `out/provenance.json` — 출처·URL·수집 시각·응답 sha256 (근거원장 입력). **인증키는 `***KEY***`로 마스킹** |
| 응답 형식 | 실거래는 신형(`aptNm`)·구형(`아파트`) 태그 모두 파싱. 해제 거래(`cdealType=O`)는 정제 단계에서 제거·집계 |
| 미제공 필드 | 청약홈은 가격 갭·동시 공급을 제공하지 않음 → 해당 조건을 매칭에서 제외하고 리포트에 LIMITATION 표기 |
| 매물·호가 | 무료 공개 API 없음 → `listings_file`(CSV/JSON) 로 적재. 스키마: `asof,listings,ask_ppsm,traded_ppsm`. 부적합 행은 사유와 함께 제외되고 기준일 이후 관측은 자동 배제 (예시: `examples/listings_sample.csv`) |
| 파일 적재 스키마 | 인구이동 `period,moved_in,moved_out[,from_region]` · O/D `origin,destination,trips[,purpose]` · 역 좌표 `name,lat,lng[,lines]` · 미분양 `month,unsold[,after_done]` · 주택건설실적 `period,permit[,start,sale,done]` (예시 파일 모두 `examples/`) |
| 접근성 표현 통제 | 최근접역이 도보 10분 이내일 때만 '도보 n분' 문장이 생성된다. 그 밖에는 문장을 만들지 않고 리포트에 **'역세권 표현 사용 불가'** 와 실측 거리를 표기한다 |

## 아키텍처

```
[실데이터] connectors/  molit(E01 매매) · rent(E01-R 전월세) · applyhome(E02 청약)
                        sgis(L1·L2·L4 인구·가구·사업체)
                        migration(L3 인구이동) · mobility(L7 O/D) · transit(L8 접근성)
                        commerce(L9 상권) · unsold(L12 미분양) · listings(매물·호가)
           └ 캐시·재시도·수집이력(Provenance) → live.py 가 설정(JSON)과 결합
[샘플]     sample_data(합성)
  ↓
입력(현장·거래·청약·공급·계획·소득·현장반응)
  → validation   입력 검증 6종 + 치명 결함 시 분석 중단
  → transactions 거래 정제(취소·중복·이상·특수)          [정제 내역 리포트 표기]
  → quality      데이터 적합성 5축 → A~D (D는 사용 금지)
  ├→ pricing     품질조정 가격 밴드(타입·층구간, 분양권 우선 비교군, 표본 미달 시 롤업)
  ├→ jeonse      전세가율·전월세전환율(하방 완충 두께) → 판정 ① 보강
  ├→ affordability 실부담 시뮬레이터(LTV·DSR·금리 시나리오, 구매 가능 가구 비율)
  ├→ subscription 청약경쟁률 구간 예측(유사 사례 경험분포, 표본 미달 시 정성 전환)
  │    └→ ledger  예측 이력 장부: 봉인(불변 트리거)·실적 대조·적중률(coverage)
  ├→ timeseries  월별 추세·국면 전환 감지 → scenarios 하방/기준/상방 + 민감도
  ├→ backtest    시점 분리 검증(운영과 동일 함수) → 적중률 → modelcard 드리프트
  │    └→ calibrate 조정계수 헤도닉 회귀 추정 → 홀드아웃 검증 통과 시에만 교체
  ├→ liquidity   환금성(회전율·가격분산·거래간격) → 판정 ③ 강화
  ├→ supply      확률조정 공급(단계별 실현 가능성 가중)
  │    └→ housing 인허가 실적으로 공급 목록 교차검증 + 중기 압력 선행 신호
  ├→ catalyst    성숙도 엔진(검토/추진/확정 단계 → 광고 취급 등급, 촉매카드)
  ├→ alerts      조기경보(개발계획·시장·공급 신호 스냅숏 비교)
  ├→ competitor  경쟁 현장 모니터링(가격·혜택·잔여 변동, 수집 신선도 경고)
  ├→ feedback    현장 반응 정합성(거절 사유 vs 4개 판정, 재검토 플래그)
  → verdicts     4개 독립 판정(가격·수요·공급/환금성·촉매) × 4속성 — 단일 점수 합산 없음
  → claims       표현 5등급 + 광고 3등급 + 린트 게이트(금지 표현·무근거 차단)
  → runstore     회차 저장·직전 대비 '변화' 산출 (판정 4속성 완성)
  → evidence     근거원장: 핵심 수치마다 출처(sha256)·산출식·표본·한계 등재
  → report       진단리포트 조립 → render_html 배포용 단일 HTML
                 (목차·판정 배지·인쇄 스타일 포함, 외부 자산 없음)
```

## 설계 원칙 (제안서와의 대응)

| 원칙 | 구현 | 근거 |
|------|------|------|
| 전망은 봉인·대조된다 | `ledger.py` — UPDATE/DELETE 차단 트리거, sha256 봉인, coverage 산출 | 부록 E.2·E.3 (P0-2·P0-3) |
| 표본이 지지하지 않는 수치는 내지 않는다 | 밴드 롤업(`pricing.py`), 청약 정성 전환(`subscription.py`), 치명 결함 중단(`validation.py`) | 5.4.5·5.10 (P1-5) |
| 검증되지 않은 문장은 나가지 않는다 | 린트 게이트(`claims.py`) — FORECAST는 '사용 가능' 불가, 금지 표현 차단 | 5.9·14.5 |
| 신축 비교군은 분양권 우선 | 분양권 거래 가중(`pricing.py`) | P1-1 |
| 현장이 분석을 교정한다 | 거절 사유 vs 판정 정합성, 방문객 거주지 vs 인구이동 유입 출발지(`feedback.py`) | 5.11.3 (P1-3) |
| 접근성은 주장이 아니라 좌표로 말한다 | 최근접역 도보 10분 이내에서만 문장 생성(`transit.py`·`pipeline.py`) | 5.9 광고 표현 통제 |
| 가격의 하방은 전세가 말한다 | 전세가율·전월세전환율(`jeonse.py`) — 갱신 계약 제외, 표본 미달 시 미산출 | 5.4 가격 검증 |
| 단일 AI 점수로 합치지 않는다 | 4개 독립 판정(`verdicts.py`) | 5.8 |
| 예측은 사후 검증된다 | 시점 분리 백테스트(`backtest.py`) — 운영과 동일 함수 호출 | 5.4.2·E.2 |
| 계수는 검증을 통과할 때만 바뀐다 | 헤도닉 회귀 + 홀드아웃 잔차 분산(`calibrate.py`) — 유의성·부호·범위 게이트 통과 후에도 검증 구간이 개선돼야 채택 | 5.4.2 |
| 상품이 다르면 모델도 다르다 | 상품 프로파일(`profiles.py`) — 비교군·표본·청약 적용 분리 | P2-2 |
| 판정은 직전 회차와 비교된다 | 실행 이력(`runstore.py`) — 판정별 관련 지표만 델타 표기 | 5.4.5 '변화' |
| 모든 수치는 되물을 수 있다 | 근거원장(`evidence.py`) — 지표별 출처·수집 해시·산출식·표본·한계. **미산출 항목도 사유와 함께 등재** | 5.10 검증 가능성 |

## 저장소 구성

```
report_system/             파이프라인 패키지 (stdlib only)
  connectors/              실데이터 커넥터 (molit=E01, applyhome=E02, base=캐시·이력)
  geo.py                   좌표 유틸 (직선거리·보행 보정 도보 시간)
  calibrate.py             조정계수 교정 (헤도닉 회귀 + 홀드아웃 검증, stdlib OLS)
  live.py                  설정 JSON + 커넥터 → 리포트
tests/                     unittest 스위트 (293건) — run_all.py 로 일괄 실행
examples/site_config.json  실데이터 실행 설정 예시
proposal/                  사업 제안서 (md + docx 납품본 + 변환 스크립트)
docs/                      설계검토보고서 (P0/P1/P2 진단)
out/                       생성 산출물·캐시·장부 (git 미추적)
```

## 현재 한계

| 항목 | 상태 |
|------|------|
| 매매(E01)·전월세(E01-R)·청약(E02) | **구현 완료** — 캐시·재시도·수집이력 포함. 전월세로 전세가율·전월세전환율 산출 |
| 매물·호가 (P1-2 선행 신호) | **파일 수집 구현** — 무료 공개 API 부재로 CSV/JSON 적재 방식. `listings_file` 지정 시 활성화 |
| 인구·가구·사업체(L1·L2·L4)·인구이동(L3)·접근성(L8)·상권(L9) | **구현 완료** — 각 커넥터의 공간 해상도·환산 한계는 리포트에 병기 |
| 공급 파이프라인 (L13) | **인허가 실적 연동** — 설정 공급 목록을 시군구 인허가와 교차검증하고, 인허가 급증 시 중기 공급 압력을 판정 ③에 반영 |
| 생활이동·O/D (L7) | **파일 적재 구현** — KTDB·통신사 자료는 계약·승인 대상. 파일 확보 시 즉시 활성화 |
| 소득·구매력 (L5) | 공공 대체 로그정규 근사 — 설정의 분포 파라미터 기반, LIMITATION 표기 |
| 조정계수(연식·층·시점) | **교정 엔진 구현** — `calibrate` 가 헤도닉 회귀로 추정하고 홀드아웃 검증을 통과할 때만 교체. 미교정 상태는 모델 카드에 '예시값'으로 표기 |
| 단계 실현률·DSR 가정 | 파라미터 노출. **실적 누적 전까지 예시값** |
| 드리프트·상품 프로파일·커버리지표 | **구현 완료** (P2-1·P2-2·P2-4) |
| 경쟁 현장 모니터링·정제 룰 버전화 | **구현 완료** (P2-6·P2-3) |

실데이터 실행 시 조정계수가 미교정 상태라는 점은 리포트의 가정·한계 절에 표기되며,
예측 이력 장부(`coverage`)에 실적이 누적되면 적중률 기준으로 재보정한다.
