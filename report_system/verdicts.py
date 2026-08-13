"""종합 판정 — 하나의 점수로 합산하지 않는 4개 독립 결과 (제안서 5.8).

각 판정은 방향·강도·신뢰도·변화의 4속성(5.4.5)을 갖는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean

from .affordability import AffordabilityResult
from .liquidity import LiquidityResult
from .pricing import MarketPosition
from .subscription import SubscriptionForecast
from .supply import SupplyAssessment
from .catalyst import CatalystCard


@dataclass
class Verdict:
    name: str
    direction: str        # 긍정 / 중립 / 부정
    strength: str         # 강 / 중 / 약
    confidence: str       # 높음 / 보통 / 낮음
    change: str           # 직전 분석 대비 (초기 분석은 "최초")
    rationale: list[str]


def price_verdict(positions: list[MarketPosition]) -> Verdict:
    if not positions:
        return Verdict("① 현재 가격 위치", "중립", "약", "낮음", "최초",
                       ["비교 표본 부족으로 판정 유보"])
    labels = [p.label for p in positions]
    lower = sum("하단" in l for l in labels)
    upper = sum("상단" in l for l in labels)
    n_min = min(p.band.n for p in positions)
    direction = "긍정" if lower > upper else ("부정" if upper > lower else "중립")
    strength = "강" if abs(lower - upper) == len(labels) and len(labels) > 1 else "중"
    confidence = "높음" if n_min >= 30 else ("보통" if n_min >= 8 else "낮음")
    rationale = [f"{p.type_name}: 취득원가 {p.subject_ppsm/1e4:,.0f}만원/㎡ → {p.label} "
                 f"(밴드 {p.band.q25/1e4:,.0f}~{p.band.q75/1e4:,.0f}, n={p.band.n}"
                 f"{', 롤업' if p.band.rolled_up else ''})"
                 for p in positions]
    return Verdict("① 현재 가격 위치", direction, strength, confidence, "최초", rationale)


def demand_verdict(afford: list[AffordabilityResult],
                   sub: SubscriptionForecast,
                   region_stats: object | None = None,
                   commerce: object | None = None) -> Verdict:
    rationale: list[str] = []
    scores: list[float] = []

    if region_stats is not None:
        rationale.append(f"지역 통계(L1·L2·L4): {region_stats.summary()}")
        hh = getattr(region_stats, "household_cagr", None)
        if hh is not None:
            # 가구 증가는 주거 수요의 직접 신호 — 연 1% 증가를 기준선으로 정규화
            scores.append(max(0.0, min(1.0, (hh + 1.0) / 3.0)))
    if commerce is not None:
        rationale.append(f"생활 인프라(L9): {commerce.summary()}")
        scores.append(commerce.essential_coverage)

    if afford:
        base_shares = [a.scenarios[1]["eligible_share"] for a in afford if len(a.scenarios) > 1]
        if base_shares:
            share = mean(base_shares)
            scores.append(share)
            rationale.append(f"기준 금리에서 구매 가능 가구 비율 평균 {share:.0%} (실부담 시뮬레이션)")

    if sub.ok:
        rationale.append(
            f"예상 청약경쟁률 {sub.lo:.1f}~{sub.hi:.1f}:1 (중위 {sub.mid:.1f}, "
            f"유사 사례 {sub.n_cases}건, 미달 위험 {sub.shortfall_prob:.0%}) [FORECAST]")
        scores.append(min(sub.mid / 10.0, 1.0))
    else:
        rationale.append(f"청약 전망: {sub.reason}")

    if not scores:
        return Verdict("② 수요 지속성", "중립", "약", "낮음", "최초", rationale)
    s = mean(scores)
    direction = "긍정" if s >= 0.45 else ("부정" if s < 0.25 else "중립")
    confidence = "보통" if sub.ok and sub.filters_relaxed <= 1 else "낮음"
    return Verdict("② 수요 지속성", direction, "중", confidence, "최초", rationale)


def supply_verdict(sa: SupplyAssessment, site_units: int,
                   liq: "LiquidityResult | None" = None,
                   unsold: object | None = None) -> Verdict:
    ratio = sa.adjusted_units / site_units if site_units else 0
    if ratio >= 8:
        direction, strength = "부정", "강"
    elif ratio >= 3:
        direction, strength = "부정", "중"
    elif ratio >= 1.5:
        direction, strength = "중립", "중"
    else:
        direction, strength = "긍정", "중"

    rationale = [
        f"{sa.window_months}개월 내 확률조정 공급 {sa.adjusted_units:,.0f}세대 "
        f"(발표 물량 {sa.nominal_units:,}세대) — 현장 세대수 대비 {ratio:.1f}배"]

    confidence = "보통"
    if unsold is not None and getattr(unsold, "latest", None) is not None:
        rationale.append(f"미분양(L12): {unsold.summary()}")
        t = unsold.trend_pct()
        if t is not None and t >= 20 and direction != "부정":
            direction = "부정"
            rationale.append("→ 미분양 증가 추세로 위험 판정 상향")
    if liq is not None:
        rationale.append(liq.as_rationale())
        # 환금성 취약이 확인되면 위험 판정을 한 단계 강화한다
        if liq.turnover_pct_year is not None:
            if liq.label == "환금성 취약" and direction != "부정":
                direction, strength = "부정", "중"
                rationale.append("→ 공급 부담은 낮으나 환금성 취약으로 위험 판정 상향")
            confidence = "보통" if liq.n_trades >= 30 else "낮음"
        else:
            confidence = "낮음"

    return Verdict("③ 공급·환금성 위험", direction, strength, confidence, "최초", rationale)


def catalyst_verdict(cards: list[CatalystCard]) -> Verdict:
    if not cards:
        return Verdict("④ 촉매·실행 가능성", "중립", "약", "보통", "최초",
                       ["평가 대상 개발계획 없음"])
    strong = [c for c in cards if c.feasibility >= 0.65 and c.relevance in ("상", "중")]
    weak = [c for c in cards if c.feasibility < 0.45]
    direction = "긍정" if strong else ("부정" if len(weak) == len(cards) else "중립")
    rationale = [
        f"{c.name}: {c.stage}({c.stage_group}) 실현성 {c.feasibility:.0%}, "
        f"관련성 {c.relevance}, {c.budget_note} → 광고 {c.ad_grade.value}"
        for c in cards]
    return Verdict("④ 촉매·실행 가능성", direction,
                   "중" if strong else "약", "보통", "최초", rationale)
