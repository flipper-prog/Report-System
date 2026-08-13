"""종합 판정 — 하나의 점수로 합산하지 않는 4개 독립 결과 (제안서 5.8).

각 판정은 방향·강도·신뢰도·변화의 4속성(5.4.5)을 갖는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, median

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


def price_verdict(positions: list[MarketPosition],
                  jeonse: object | None = None) -> Verdict:
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

    # 전세 기반 하방 점검 — 매매 표본만으로는 보이지 않는 완충 두께를 덧댄다.
    # 전세가율이 얇은데 가격이 밴드 상단이면, 두 신호가 같은 방향을 가리키므로
    # 강도를 올린다. 반대로 두터운 완충은 상단 판정의 강도를 낮춘다.
    if jeonse is not None:
        rationale.append(jeonse.as_rationale())
        ratio = getattr(jeonse, "ratio_pct", None)
        if ratio is not None:
            subj = median([p.subject_ppsm for p in positions])
            cov = jeonse.coverage_of(subj)
            if cov is not None:
                rationale.append(
                    f"분양가 전세 충당율 {cov:.0f}% — 총취득원가 중 전세보증금으로 "
                    f"회수 가능한 비율")
            if jeonse.label == "하방 완충 얇음" and direction == "부정":
                strength = "강"
                rationale.append("→ 밴드 상단 + 전세 완충 부족 — 하방 위험 강도 상향")
            elif jeonse.label == "하방 지지 두터움" and direction == "부정" and strength == "강":
                strength = "중"
                rationale.append("→ 전세 완충이 두터워 하방 위험 강도 하향")

    return Verdict("① 현재 가격 위치", direction, strength, confidence, "최초", rationale)


#: 접근성 라벨 → 수요 점수. 현재 인프라이므로 촉매(④)가 아닌 수요(②)에 반영한다.
_TRANSIT_SCORE = {
    "대중교통 접근 우수": 1.0,
    "대중교통 접근 보통": 0.6,
    "대중교통 접근 취약": 0.2,
}


def demand_verdict(afford: list[AffordabilityResult],
                   sub: SubscriptionForecast,
                   region_stats: object | None = None,
                   commerce: object | None = None,
                   migration: object | None = None,
                   mobility: object | None = None,
                   transit: object | None = None) -> Verdict:
    rationale: list[str] = []
    scores: list[float] = []

    if region_stats is not None:
        rationale.append(f"지역 통계(L1·L2·L4): {region_stats.summary()}")
        hh = getattr(region_stats, "household_cagr", None)
        if hh is not None:
            # 가구 증가는 주거 수요의 직접 신호 — 연 1% 증가를 기준선으로 정규화
            scores.append(max(0.0, min(1.0, (hh + 1.0) / 3.0)))

    if migration is not None:
        rationale.append(f"인구이동(L3): {migration.summary()}")
        rate = migration.net_rate_per_1000()
        if rate is not None:
            # 인구 1천명당 -5 ~ +10 을 0~1 로 정규화 (시군구 분포의 통상 범위)
            scores.append(max(0.0, min(1.0, (rate + 5.0) / 15.0)))
        else:
            label = migration.label
            if label.startswith("순유입"):
                scores.append(0.7)
            elif label.startswith("순유출"):
                scores.append(0.2)
            elif label == "이동 균형":
                scores.append(0.5)

    if mobility is not None:
        rationale.append(f"생활이동·O/D(L7): {mobility.summary()}")
        sc = mobility.self_containment
        if sc is not None:
            scores.append(min(1.0, sc))
            targets = mobility.target_regions(3)
            if targets:
                rationale.append(
                    f"→ 실제 유입 통행 기준 광고 타깃 후보: {', '.join(targets)}")

    if transit is not None:
        rationale.append(f"교통 접근성(L8): {transit.summary()}")
        s = _TRANSIT_SCORE.get(transit.label)
        if s is not None:
            scores.append(s)

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
                   unsold: object | None = None,
                   housing: object | None = None) -> Verdict:
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
    if housing is not None and getattr(housing, "points", None):
        rationale.append(f"주택건설실적(L13): {housing.summary()}")
        cross = housing.pipeline_check(int(sa.nominal_units))
        if cross:
            rationale.append(f"→ 공급 목록 교차검증: {cross}")
        y = housing.yoy_pct("permit")
        # 인허가는 착공·분양의 선행 지표다. 지금 공급 부담이 낮아도 인허가가
        # 급증했다면 중기 위험이 남아 있으므로 '긍정'으로 닫지 않는다.
        if y is not None and y >= 30.0 and direction == "긍정":
            direction, strength = "중립", "중"
            rationale.append("→ 인허가 급증으로 중기 공급 압력 — 긍정 판정 보류")

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
