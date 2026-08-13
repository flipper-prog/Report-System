"""엔드투엔드 파이프라인: 검증 → 정제 → 분석 → 판정 → 봉인 → 리포트."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import median  # noqa: F401  (앵커·가격갭 계산에 사용)

from .affordability import simulate
from .alerts import Alert, scan_catalyst, scan_market
from .backtest import (BacktestReport, backtest_price_bands,
                       backtest_subscription, quarterly_cutoffs)
from .catalyst import assess
from .competitor import CompetitorSnapshot, scan as scan_competitors
from .coverage_table import build as build_coverage
from .modelcard import (detect_drift, price_band_card, scenario_card,
                        subscription_card)
from .claims import lint
from .evidence import EvidenceLedger
from .feedback import check as feedback_check
from .funnel import FunnelSnapshot, diagnose as diagnose_funnel
from .ledger import ForecastLedger
from .liquidity import analyze as analyze_liquidity
from .jeonse import analyze as analyze_jeonse
from .models import (AdGrade, CatalystPlan, Claim, ClaimGrade, Comparable,
                     DatasetMeta, FieldFeedback, ListingSnapshot, RentRecord,
                     Site, SubscriptionRecord, SupplyItem, Transaction,
                     total_acquisition_cost)
from .price_decision import sweep as price_sweep
from .pricing import (DEFAULT_COEF, Coefficients, market_positions,
                      quality_adjusted_bands)
from .profiles import applicability_note, get as get_profile
from .report import ReportInputs, generate_markdown
from .runstore import RunSnapshot, RunStore, describe_change
from .salespack import build as build_salespack
from .scenarios import build as build_scenarios
from .subscription import SubscriptionForecast, predict
from .supply import probability_adjusted
from .timeseries import monthly_trend
from .transactions import RULES_VERSION, clean
from .validation import (has_fatal, validate_rents, validate_site,
                         validate_transactions)
from .verdicts import (catalyst_verdict, demand_verdict, price_verdict,
                       supply_verdict)


@dataclass
class PipelineResult:
    markdown: str
    inputs: ReportInputs
    forecast_id: str


class FatalInputError(RuntimeError):
    """치명적 입력 결함 — 분석 중단 (제안서 5.10.2)."""


def run(
    site: Site,
    comps: dict[str, Comparable],
    txs: list[Transaction],
    sub_history: list[SubscriptionRecord],
    supply_items: list[SupplyItem],
    catalyst_plans_old: list[CatalystPlan],
    catalyst_plans_new: list[CatalystPlan],
    dataset_meta: list[DatasetMeta],
    incomes: list[float],
    feedback: FieldFeedback,
    listings: tuple[ListingSnapshot, ListingSnapshot],
    asof: date,
    ledger: ForecastLedger,
    store: "RunStore | None" = None,
    competitors_old: "list[CompetitorSnapshot] | None" = None,
    competitors_new: "list[CompetitorSnapshot] | None" = None,
    region_stats: object | None = None,     # L1·L2·L4 (SGIS)
    commerce: object | None = None,         # L9 (상권)
    unsold: object | None = None,           # L12 보강 (미분양)
    migration: object | None = None,        # L3 (인구이동)
    mobility: object | None = None,         # L7 (생활이동·O/D)
    transit: object | None = None,          # L8 (교통망·접근성)
    rents: "list[RentRecord] | None" = None,   # L11 전월세
    coef: Coefficients = DEFAULT_COEF,      # 품질조정 계수(교정 시 교체)
    provenance: "list | None" = None,       # 수집 이력 → 근거원장 출처 연결
    housing: object | None = None,          # L13 (주택건설실적)
    income_stats: object | None = None,     # L5 (실측 소득 통계)
    funnel: "FunnelSnapshot | None" = None,   # 퍼널 실적 (제10장)
    funnel_benchmarks: "dict[str, float] | None" = None,
) -> PipelineResult:
    # 1) 입력 검증 — 치명 결함 시 중단
    issues = (validate_site(site, asof) + validate_transactions(txs, asof)
              + validate_rents(rents or [], asof))
    if has_fatal(issues):
        msgs = "; ".join(i.message for i in issues if i.fatal)
        raise FatalInputError(f"분석 중단: {msgs}")

    # 1-2) 상품 프로파일 (P2-2) — 비교군·표본·수요가중·청약 적용 여부를 결정
    profile = get_profile(site.product_type)

    # 2) 거래 정제
    cr = clean(txs)
    if not cr.kept:
        # 비교 거래가 하나도 남지 않으면 가격·추세·환금성 전부가 성립하지 않는다.
        # 억지로 진행해 빈 수치를 내는 대신 '분석 불가'로 멈춘다 (5.10.2).
        dropped = sum(v for k, v in cr.summary.items() if k != "사용")
        raise FatalInputError(
            f"분석 중단: 정제 후 사용 가능한 비교 거래 0건 "
            f"(입력 {len(txs)}건 · 정제 제외 {dropped}건). "
            "비교단지 설정과 수집 기간을 확인하십시오.")

    # 3) 가격 밴드·시장 위치
    bands = quality_adjusted_bands(site, comps, cr.kept, asof, profile=profile,
                                   coef=coef)
    positions = market_positions(site, bands)

    # 4) 실부담
    afford = [simulate(t, incomes) for t in site.types]

    # 5) 청약 전망 → 장부 봉인 (P0-1/P0-3)
    market_ppsm = median(t.price / t.area_m2 for t in cr.kept)
    subj_ppsm = median(total_acquisition_cost(t) / t.area_m2 for t in site.types)
    gap_pct = (subj_ppsm - market_ppsm) / market_ppsm * 100
    concurrent = int(probability_adjusted(supply_items, window_months=12).adjusted_units)
    if profile.subscription_applicable:
        sub_fc = predict(sub_history, site.region, gap_pct, concurrent)
    else:
        sub_fc = SubscriptionForecast(
            ok=False,
            reason=f"{profile.product.value}은 청약 제도 비적용 상품 — 전망 미실행 (프로파일 규칙)")
    fid = ""
    if sub_fc.ok:
        fid = ledger.seal(
            kind="subscription", target=site.id,
            lo=sub_fc.lo, hi=sub_fc.hi, confidence=sub_fc.confidence,
            model_version=sub_fc.model_version, data_asof=str(asof),
            payload={"gap_pct": round(gap_pct, 2), "concurrent": concurrent,
                     "n_cases": sub_fc.n_cases})

    # 5-2) 분양가 결정 시뮬레이션 — "그래서 얼마로?"에 같은 근거로 답한다
    # 밴드가 없어도 실행한다 — 권고하지 못한 '이유'를 보여 주는 것도 산출물이다.
    decision = None
    if profile.subscription_applicable and market_ppsm > 0:
        decision = price_sweep(site, bands, market_ppsm, incomes, sub_history,
                               concurrent)

    # 6) 공급
    sa = probability_adjusted(supply_items, window_months=36)

    # 7) 촉매 + 조기경보
    cards = [assess(p) for p in catalyst_plans_new]

    # 7-2) 시계열 추세 → 조건부 가격 시나리오 → 장부 봉인
    trend = monthly_trend(cr.kept)
    anchor = median([b.q50 for b in bands if b.level == "타입"]) if bands else 0.0
    scen = None
    scen_id = ""
    if anchor > 0:
        supply_ratio = sa.adjusted_units / site.total_units if site.total_units else 0.0
        scen = build_scenarios(anchor, trend.slope_pct_per_year, supply_ratio, cards)
        scen_id = ledger.seal(
            kind="price", target=f"{site.id}:anchor",
            lo=scen.low.price_ppsm, hi=scen.high.price_ppsm,
            confidence=0.0,   # 시나리오 범위는 확률 구간이 아님(전제 기반) — E.2 적중률 산출 제외
            model_version="scenario-driver-0.1", data_asof=str(asof),
            payload={"anchor": anchor, "trend_pct_year": trend.slope_pct_per_year,
                     "supply_ratio": round(supply_ratio, 2),
                     "legs": [(l.name, l.annual_pct) for l in scen.legs]})

    # 7-3) 백테스트 (운영과 동일 함수로 시점 분리 검증)
    backtests: list[BacktestReport] = []
    if cr.kept:
        first = min(t.trade_date for t in cr.kept)
        cuts = quarterly_cutoffs(first, asof)
        if cuts:
            backtests.append(backtest_price_bands(site, comps, cr.kept, cuts,
                                                  coef=coef))
    if sub_history:
        backtests.append(backtest_subscription(sub_history))

    # 7-4) 커버리지표 · 모델 카드 · 드리프트
    cov_rows = build_coverage(
        tx_count=len(cr.kept), sub_count=len(sub_history),
        supply_items=len(supply_items), catalyst_items=len(catalyst_plans_new),
        income_model=bool(incomes),
        income_measured=income_stats is not None,
        listings_connected=bool(listings and listings[1].listings),
        rent_connected=bool(rents),
        region_stats_connected=region_stats is not None,
        commerce_connected=commerce is not None,
        unsold_connected=unsold is not None,
        migration_connected=migration is not None,
        mobility_connected=mobility is not None,
        transit_connected=transit is not None,
        housing_connected=housing is not None)
    span = (f"{min(t.trade_date for t in cr.kept)} ~ {max(t.trade_date for t in cr.kept)}"
            if cr.kept else "없음")
    bt_price = next((b for b in backtests if b.name.startswith("가격")), None)
    bt_sub = next((b for b in backtests if b.name.startswith("청약")), None)
    cards_md = [price_band_card(len(cr.kept), span, bt_price, coef),
                subscription_card(sub_fc.n_cases, bt_sub)]
    if scen:
        cards_md.append(scenario_card(scen.horizon_months))
    drifts = [detect_drift(b) for b in backtests if b.folds]
    alerts: list[Alert] = []
    old_by_id = {p.id: p for p in catalyst_plans_old}
    for p in catalyst_plans_new:
        if p.id in old_by_id:
            alerts += scan_catalyst(old_by_id[p.id], p)
    alerts += scan_market(*listings)
    if competitors_new:
        alerts += scan_competitors(competitors_old or [], competitors_new, asof)

    # 8) 판정 4종
    jeonse_res = analyze_jeonse(rents or [], cr.kept, asof) if rents else None
    v1 = price_verdict(positions, jeonse=jeonse_res)
    v2 = demand_verdict(afford, sub_fc, region_stats=region_stats,
                        commerce=commerce, migration=migration,
                        mobility=mobility, transit=transit)
    liq = analyze_liquidity(cr.kept, comps, asof, good_threshold=profile.turnover_good_pct)
    v3 = supply_verdict(sa, site.total_units, liq, unsold=unsold,
                        housing=housing)
    v4 = catalyst_verdict(cards)
    verdicts = [v1, v2, v3, v4]

    # 8-2) 직전 회차 대비 '변화' 속성 산출 후 이번 회차 저장 (5.4.5)
    cur_metrics = {
        "anchor_ppsm": anchor,
        "supply_ratio": (sa.adjusted_units / site.total_units) if site.total_units else 0.0,
        "sub_mid": sub_fc.mid if sub_fc.ok else 0.0,
        "turnover": liq.turnover_pct_year or 0.0,
    }
    if store is not None:
        prev = store.latest(site.id, str(asof))
        for v in verdicts:
            cur = {"direction": v.direction, "strength": v.strength,
                   "confidence": v.confidence}
            v.change = describe_change(v.name, cur, prev, cur_metrics)
        store.save(RunSnapshot(
            site_id=site.id, asof=str(asof),
            verdicts={v.name: {"direction": v.direction, "strength": v.strength,
                               "confidence": v.confidence} for v in verdicts},
            metrics=cur_metrics))

    # 9) 현장 반응 정합성 (P1-3)
    flags = feedback_check(
        feedback,
        price_verdict_positive=(v1.direction == "긍정"),
        expected_home_regions=[site.region],
        catalyst_ad_active=any(c.ad_grade != AdGrade.FORBIDDEN for c in cards),
        migration=migration)

    # 10) 표현 통제 — 리포트에 실릴 후보 문장 구성 후 린트
    claims = _build_claims(site, positions, sub_fc, cards, transit)
    lint_res = lint(claims)

    # 9-2) 퍼널 병목 진단 — 광고·상담·조건 중 어디가 막혔는가 (10.5)
    funnel_dx = None
    if funnel is not None:
        funnel_dx = diagnose_funnel(funnel, funnel_benchmarks,
                                    feedback=feedback, verdicts=verdicts)

    # 10-2) 판매 논리 산출물 — 분석을 영업 언어로 옮기는 통제 지점 (제6장)
    pack = build_salespack(
        positions=positions, verdicts=verdicts, afford=afford,
        sub_forecast=sub_fc, supply=sa, site_units=site.total_units,
        catalysts=cards, alerts=alerts, feedback=feedback,
        liquidity=liq, jeonse=jeonse_res, unsold=unsold, housing=housing,
        price_decision=decision, region_stats=region_stats,
        migration=migration, mobility=mobility, transit=transit,
        commerce=commerce)

    # 11) 근거원장 — 리포트의 핵심 수치마다 출처·산출식·표본·한계를 등재
    ledger_ev = _build_evidence(
        provenance=provenance, cr=cr, bands=bands, anchor=anchor, coef=coef,
        jeonse=jeonse_res, afford=afford, sub_fc=sub_fc, fid=fid, sa=sa,
        site=site, liq=liq, scen=scen, scen_id=scen_id, cards=cards,
        region_stats=region_stats, migration=migration, mobility=mobility,
        transit=transit, commerce=commerce, unsold=unsold, housing=housing,
        income_stats=income_stats, decision=decision,
        competitor_alerts=[a for a in alerts if a.category == "경쟁 현장"])

    inputs = ReportInputs(
        site=site, asof=asof, clean=cr, dataset_meta=dataset_meta,
        bands=bands, positions=positions, afford=afford,
        sub_forecast=sub_fc, sub_forecast_id=fid, supply=sa,
        catalysts=cards, verdicts=verdicts, alerts=alerts,
        feedback_flags=flags, lint=lint_res,
        trend=trend, scenarios=scen, scenario_id=scen_id, backtests=backtests,
        coverage_rows=cov_rows, model_cards=cards_md, drifts=drifts,
        liquidity=liq, profile_note=applicability_note(profile),
        profile_notes=list(profile.notes),
        region_stats=region_stats, commerce=commerce, unsold=unsold,
        migration=migration, mobility=mobility, transit=transit,
        jeonse=jeonse_res, evidence=ledger_ev, housing=housing,
        income_stats=income_stats, price_decision=decision,
        salespack=pack, funnel=funnel_dx)
    return PipelineResult(generate_markdown(inputs), inputs, fid)


def _build_evidence(*, provenance, cr, bands, anchor, coef, jeonse, afford,
                    sub_fc, fid, sa, site, liq, scen, scen_id, cards,
                    region_stats, migration, mobility, transit, commerce,
                    unsold, housing, income_stats, decision,
                    competitor_alerts) -> EvidenceLedger:
    """리포트의 핵심 수치를 순서대로 등재한다.

    산출하지 못한 지표도 사유와 함께 남긴다 — 검토하지 않은 것과 표본이 없어
    수치를 내지 않은 것은 다르고, 독자는 그 둘을 구분할 수 있어야 한다.
    """
    ev = EvidenceLedger(provenance)
    sale_src = ev.find("매매 실거래", "RTMSDataSvcAptTrade", "E01)")
    rent_src = ev.find("전월세")
    sub_src = ev.find("청약")

    ev.add("정제 후 사용 거래", f"{len(cr.kept):,}건",
           f"transactions.clean (룰 {RULES_VERSION})", ClaimGrade.FACT,
           sale_src, n=len(cr.kept),
           limitations=[f"제외 {sum(v for k, v in cr.summary.items() if k != '사용'):,}건 "
                        "— 취소·중복·이상·특수 의심"])

    if anchor > 0:
        ev.add("품질조정 앵커 (타입 중위 ㎡단가)", f"{anchor/1e4:,.0f}만원/㎡",
               f"pricing.quality_adjusted_bands · 계수 출처: {coef.source}",
               ClaimGrade.CALCULATION, sale_src,
               n=sum(b.n for b in bands if b.level == "타입"),
               limitations=([] if not coef.source.startswith("초기값")
                            else ["조정계수 미교정 — `calibrate` 실행 전 예시값"]))
    else:
        ev.add_missing("품질조정 앵커", "비교 표본 부족으로 밴드 미산출",
                       "pricing.quality_adjusted_bands")

    if jeonse is not None and jeonse.ratio_pct is not None:
        ev.add("전세가율", f"{jeonse.ratio_pct:.1f}%",
               f"jeonse.analyze (최근 {jeonse.ratio_window_months}개월, 갱신 제외)",
               ClaimGrade.CALCULATION, rent_src + sale_src,
               n=jeonse.n_ratio_jeonse, limitations=list(jeonse.limitations))
    elif jeonse is not None:
        ev.add_missing("전세가율", "전세·매매 표본 미달로 미산출", "jeonse.analyze")
    else:
        ev.add_missing("전세가율", "전월세 데이터 미수집", "jeonse.analyze")

    if afford and not any(a.share_available for a in afford):
        ev.add_missing("구매 가능 가구 비율 (기준 금리)",
                       next((a.note for a in afford if a.note), "소득 표본 부족"),
                       "affordability.simulate")
    elif afford:
        shares = [a.scenarios[1]["eligible_share"] for a in afford
                  if len(a.scenarios) > 1 and a.scenarios[1]["eligible_share"] is not None]
        if shares:
            ev.add("구매 가능 가구 비율 (기준 금리)",
                   f"{sum(shares)/len(shares):.0%}",
                   "affordability.simulate (LTV·DSR·30년 원리금균등)",
                   ClaimGrade.CALCULATION,
                   ev.find("소득") if income_stats is not None else [],
                   n=len(afford),
                   limitations=(
                       [f"소득 분포 중심: {income_stats.summary()}"]
                       + list(getattr(income_stats, "limitations", []) or [])
                       if income_stats is not None
                       else ["소득 분포는 공공 대체 근사 — 실측 소득으로 교체 권고"])
                   + ["가용 자기자본 ≈ 연소득×4 가정"])

    if sub_fc.ok:
        ev.add("청약 경쟁률 전망", f"{sub_fc.lo:.1f}~{sub_fc.hi:.1f} : 1",
               f"subscription.predict ({sub_fc.model_version}) · 봉인 {fid}",
               ClaimGrade.FORECAST, sub_src, n=sub_fc.n_cases,
               limitations=["명목 신뢰수준 기반 구간 — 실적은 예측 이력 장부에서 대조",
                            f"필터 완화 {sub_fc.filters_relaxed}단계"])
    else:
        ev.add_missing("청약 경쟁률 전망", sub_fc.reason, "subscription.predict")

    ratio = sa.adjusted_units / site.total_units if site.total_units else 0.0
    ev.add("확률조정 공급배수", f"{ratio:.1f}배",
           f"supply.probability_adjusted ({sa.window_months}개월, 단계별 실현률 가중)",
           ClaimGrade.CALCULATION, n=sa.n_items if hasattr(sa, "n_items") else None,
           limitations=["단계별 실현률은 실적 누적 전까지 예시값",
                        "공급 목록은 설정 입력 — 인허가 통계 연동 시 자동화 가능"])

    if liq is not None and liq.turnover_pct_year is not None:
        ev.add("연환산 거래 회전율", f"{liq.turnover_pct_year:.1f}%",
               "liquidity.analyze (거래건수 ÷ 세대수)", ClaimGrade.CALCULATION,
               sale_src, n=liq.n_trades, limitations=list(liq.limitations))
    else:
        ev.add_missing("연환산 거래 회전율", "비교단지 세대수 미입력 또는 거래 부족",
                       "liquidity.analyze")

    if scen is not None:
        ev.add("조건부 가격 시나리오",
               f"{scen.low.price_ppsm/1e4:,.0f}~{scen.high.price_ppsm/1e4:,.0f}만원/㎡",
               f"scenarios.build ({scen.horizon_months}개월) · 봉인 {scen_id}",
               ClaimGrade.FORECAST, sale_src,
               limitations=["전제 기반 구간 — 확률적 신뢰구간이 아님",
                            "드라이버 탄력성은 교정 전 초기값"])

    # 레이어별 (표시명, 객체, 산출 모듈, 등급, 수집 이력에서 찾을 출처 키워드)
    for label, obj, method, grade, keys in (
            ("인구·가구·사업체 추세", region_stats, "connectors.sgis",
             ClaimGrade.FACT, ("SGIS",)),
            ("인구이동 순이동", migration, "connectors.migration",
             ClaimGrade.FACT, ("인구이동", "KOSIS")),
            ("생활권 자족성", mobility, "connectors.mobility",
             ClaimGrade.CALCULATION, ()),
            ("대중교통 접근성", transit, "connectors.transit + geo",
             ClaimGrade.CALCULATION, ("TAGO", "정류소")),
            ("생활 인프라 충족도", commerce, "connectors.commerce",
             ClaimGrade.FACT, ("상가업소", "소상공인")),
            ("미분양 추세", unsold, "connectors.unsold", ClaimGrade.FACT, ()),
            ("주택건설실적 추세", housing, "connectors.housing",
             ClaimGrade.FACT, ("주택건설실적",))):
        if obj is None:
            ev.add_missing(label, "해당 레이어 미수집 (설정·인증 미지정)", method)
        else:
            ev.add(label, obj.summary(), method, grade,
                   ev.find(*keys) if keys else [],
                   limitations=list(getattr(obj, "limitations", []) or []))

    if decision is not None and decision.recommended is not None:
        r = decision.recommended
        ev.add("분양가 권고", f"현재 대비 {r.multiplier:+.1%}",
               f"price_decision.sweep — 기준: {decision.criteria}",
               ClaimGrade.INFERENCE, n=len(decision.options),
               limitations=list(decision.limitations))
    elif decision is not None:
        ev.add_missing("분양가 권고", decision.reason, "price_decision.sweep")

    if competitor_alerts is not None:
        if competitor_alerts:
            ev.add("경쟁 현장 변동", f"{len(competitor_alerts)}건 감지",
                   "competitor.scan (가격 2%·잔여 20% 임계, 수집 30일 신선도)",
                   ClaimGrade.FACT, n=len(competitor_alerts),
                   limitations=["경쟁 현장 자료는 수기 수집 — 수집 시점·정확도가 "
                                "현장 담당자에게 의존 [LIMITATION]"])
        else:
            ev.add_missing("경쟁 현장 변동",
                           "스냅숏 미제공 또는 회차가 1개 — 변화 감지 불가",
                           "competitor.scan")

    if cards:
        allowed = [c.name for c in cards if c.ad_grade != AdGrade.FORBIDDEN]
        ev.add("광고 사용 가능 촉매", f"{len(allowed)}건 / 전체 {len(cards)}건",
               "catalyst.assess (성숙도 단계 × 재정 확보율)", ClaimGrade.INFERENCE,
               n=len(cards),
               limitations=["단계·예산은 고시·예산 원문 수기 등록 — 원문 링크 확인 필요"])
    return ev


def _build_claims(site, positions, sub_fc, cards, transit=None) -> list[Claim]:
    claims: list[Claim] = []
    # 접근성 문장은 좌표로 검증된 범위 안에서만 생성한다 (L8).
    # 역이 도보 기준 밖이면 문장 자체를 만들지 않는다 — 린트가 아니라 생성 단계에서 차단.
    if transit is not None:
        st = getattr(transit, "nearest_station", None)
        if st is not None and st.walk_min <= 10.0:
            claims.append(Claim(
                text=(f"{st.name}까지 도보 약 {st.walk_min:.0f}분 거리입니다"
                      f"(직선 {st.dist_m:,.0f}m, 보행 보정 적용)"),
                grade=ClaimGrade.CALCULATION,
                ad_grade=AdGrade.ALLOWED,
                evidence=["transit:nearest_station"]))
    for p in positions:
        claims.append(Claim(
            text=(f"{p.type_name} 총취득원가는 ㎡당 {p.subject_ppsm/1e4:,.0f}만원으로, "
                  f"품질조정 비교 밴드 기준 {p.label}입니다"),
            grade=ClaimGrade.CALCULATION,
            ad_grade=AdGrade.ALLOWED,
            evidence=[f"pricing:{p.type_name}"]))
    if sub_fc.ok:
        claims.append(Claim(
            text=(f"유사 조건 사례 기준, 청약 경쟁률은 {sub_fc.lo:.1f}~{sub_fc.hi:.1f}대 1 "
                  f"구간으로 전망됩니다(시장 여건 변동 시 달라질 수 있음)"),
            grade=ClaimGrade.FORECAST,
            ad_grade=AdGrade.CONDITIONAL,
            evidence=["subscription:forecast"]))
    for c in cards:
        if c.ad_grade == AdGrade.CONDITIONAL:
            claims.append(Claim(
                text=(f"{c.name}은(는) 현재 {c.stage} 단계로, 추진 시 접근성 개선이 "
                      f"기대되는 조건부 요인입니다(일정 변경 가능)"),
                grade=ClaimGrade.INFERENCE,
                ad_grade=AdGrade.CONDITIONAL,
                evidence=c.sources, counter=c.negatives))
        else:
            # 검토 단계 호재를 확정처럼 쓰는 문장 — 린트가 차단해야 정상
            claims.append(Claim(
                text=f"{c.name} 신설 확정으로 미래가치 상승",
                grade=ClaimGrade.FORECAST,
                ad_grade=AdGrade.ALLOWED,
                evidence=[]))
    return claims
