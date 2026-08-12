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
from .coverage_table import build as build_coverage
from .modelcard import (detect_drift, price_band_card, scenario_card,
                        subscription_card)
from .claims import lint
from .feedback import check as feedback_check
from .ledger import ForecastLedger
from .models import (AdGrade, CatalystPlan, Claim, ClaimGrade, Comparable,
                     DatasetMeta, FieldFeedback, ListingSnapshot, Site,
                     SubscriptionRecord, SupplyItem, Transaction,
                     total_acquisition_cost)
from .pricing import market_positions, quality_adjusted_bands
from .report import ReportInputs, generate_markdown
from .scenarios import build as build_scenarios
from .subscription import predict
from .supply import probability_adjusted
from .timeseries import monthly_trend
from .transactions import clean
from .validation import has_fatal, validate_site, validate_transactions
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
) -> PipelineResult:
    # 1) 입력 검증 — 치명 결함 시 중단
    issues = validate_site(site, asof) + validate_transactions(txs, asof)
    if has_fatal(issues):
        msgs = "; ".join(i.message for i in issues if i.fatal)
        raise FatalInputError(f"분석 중단: {msgs}")

    # 2) 거래 정제
    cr = clean(txs)

    # 3) 가격 밴드·시장 위치
    bands = quality_adjusted_bands(site, comps, cr.kept, asof)
    positions = market_positions(site, bands)

    # 4) 실부담
    afford = [simulate(t, incomes) for t in site.types]

    # 5) 청약 전망 → 장부 봉인 (P0-1/P0-3)
    market_ppsm = median(t.price / t.area_m2 for t in cr.kept)
    subj_ppsm = median(total_acquisition_cost(t) / t.area_m2 for t in site.types)
    gap_pct = (subj_ppsm - market_ppsm) / market_ppsm * 100
    concurrent = int(probability_adjusted(supply_items, window_months=12).adjusted_units)
    sub_fc = predict(sub_history, site.region, gap_pct, concurrent)
    fid = ""
    if sub_fc.ok:
        fid = ledger.seal(
            kind="subscription", target=site.id,
            lo=sub_fc.lo, hi=sub_fc.hi, confidence=sub_fc.confidence,
            model_version=sub_fc.model_version, data_asof=str(asof),
            payload={"gap_pct": round(gap_pct, 2), "concurrent": concurrent,
                     "n_cases": sub_fc.n_cases})

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
            backtests.append(backtest_price_bands(site, comps, cr.kept, cuts))
    if sub_history:
        backtests.append(backtest_subscription(sub_history))

    # 7-4) 커버리지표 · 모델 카드 · 드리프트
    cov_rows = build_coverage(
        tx_count=len(cr.kept), sub_count=len(sub_history),
        supply_items=len(supply_items), catalyst_items=len(catalyst_plans_new),
        income_model=bool(incomes))
    span = (f"{min(t.trade_date for t in cr.kept)} ~ {max(t.trade_date for t in cr.kept)}"
            if cr.kept else "없음")
    bt_price = next((b for b in backtests if b.name.startswith("가격")), None)
    bt_sub = next((b for b in backtests if b.name.startswith("청약")), None)
    cards_md = [price_band_card(len(cr.kept), span, bt_price),
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

    # 8) 판정 4종
    v1 = price_verdict(positions)
    v2 = demand_verdict(afford, sub_fc)
    v3 = supply_verdict(sa, site.total_units)
    v4 = catalyst_verdict(cards)
    verdicts = [v1, v2, v3, v4]

    # 9) 현장 반응 정합성 (P1-3)
    flags = feedback_check(
        feedback,
        price_verdict_positive=(v1.direction == "긍정"),
        expected_home_regions=[site.region],
        catalyst_ad_active=any(c.ad_grade != AdGrade.FORBIDDEN for c in cards))

    # 10) 표현 통제 — 리포트에 실릴 후보 문장 구성 후 린트
    claims = _build_claims(site, positions, sub_fc, cards)
    lint_res = lint(claims)

    inputs = ReportInputs(
        site=site, asof=asof, clean=cr, dataset_meta=dataset_meta,
        bands=bands, positions=positions, afford=afford,
        sub_forecast=sub_fc, sub_forecast_id=fid, supply=sa,
        catalysts=cards, verdicts=verdicts, alerts=alerts,
        feedback_flags=flags, lint=lint_res,
        trend=trend, scenarios=scen, scenario_id=scen_id, backtests=backtests,
        coverage_rows=cov_rows, model_cards=cards_md, drifts=drifts)
    return PipelineResult(generate_markdown(inputs), inputs, fid)


def _build_claims(site, positions, sub_fc, cards) -> list[Claim]:
    claims: list[Claim] = []
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
