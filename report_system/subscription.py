"""청약 수요 전망 모듈 (P0-1, 제안서 표 5-14-1 / 부록 E.1).

방식: 유사 사례 경험분포 기반 구간 예측.
 - 시장권·가격 갭·동시 공급 조건이 유사한 과거 청약 사례를 수집하고,
   그 경쟁률 분포의 분위수로 예측 구간을 산출한다.
 - 표본 미달 시 필터를 단계적으로 완화하며, 그래도 미달이면 수치를 제시하지
   않고 정성 판정으로 전환한다(5.4.5의 규율).
 - 모든 산출은 FORECAST 등급이며 예측 이력 장부에 봉인된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median, quantiles

from .models import SubscriptionRecord

MODEL_VERSION = "sub-empirical-0.1"
MIN_CASES = 5
CONFIDENCE = 0.60             # q20~q80 구간의 명목 신뢰수준
GAP_TOL_STEPS = (3.0, 6.0, 10.0)      # 가격 갭 허용 오차 완화 단계(%p)
SUPPLY_TOL_STEPS = (1000, 3000, 10**9)  # 동시 공급 허용 오차 완화 단계(세대)


@dataclass
class SubscriptionForecast:
    ok: bool
    reason: str = ""
    n_cases: int = 0
    lo: float = 0.0            # 경쟁률 구간 하단 (q20)
    mid: float = 0.0           # 중위
    hi: float = 0.0            # 상단 (q80)
    confidence: float = CONFIDENCE
    shortfall_prob: float = 0.0   # 유사 사례 중 미달(순위 내 미마감) 비율
    sensitivities: list[str] = field(default_factory=list)
    model_version: str = MODEL_VERSION
    filters_relaxed: int = 0


def _match(history: list[SubscriptionRecord], region: str,
           price_gap_pct: float, concurrent_supply: int,
           gap_tol: float, supply_tol: int, same_region: bool) -> list[SubscriptionRecord]:
    out = []
    for r in history:
        if same_region and r.region != region:
            continue
        if abs(r.price_gap_pct - price_gap_pct) > gap_tol:
            continue
        if abs(r.concurrent_supply - concurrent_supply) > supply_tol:
            continue
        out.append(r)
    return out


def _sensitivity_notes(history: list[SubscriptionRecord]) -> list[str]:
    """가격 갭·동시 공급이 결과를 얼마나 가르는지 간이 분석."""
    notes = []
    if len(history) >= 10:
        cheap = [r.competition_rate for r in history if r.price_gap_pct <= 0]
        rich = [r.competition_rate for r in history if r.price_gap_pct > 0]
        if cheap and rich:
            notes.append(
                f"가격 갭: 시세 이하 사례 중위 {median(cheap):.1f}:1 vs 시세 초과 {median(rich):.1f}:1")
        low = [r.competition_rate for r in history if r.concurrent_supply <= 1000]
        high = [r.competition_rate for r in history if r.concurrent_supply > 1000]
        if low and high:
            notes.append(
                f"동시 공급: 1천세대 이하 중위 {median(low):.1f}:1 vs 초과 {median(high):.1f}:1")
    return notes


def predict(history: list[SubscriptionRecord], region: str,
            price_gap_pct: float, concurrent_supply: int) -> SubscriptionForecast:
    matched: list[SubscriptionRecord] = []
    relaxed = 0
    for same_region in (True, False):
        for gi, gap_tol in enumerate(GAP_TOL_STEPS):
            for si, supply_tol in enumerate(SUPPLY_TOL_STEPS):
                matched = _match(history, region, price_gap_pct,
                                 concurrent_supply, gap_tol, supply_tol, same_region)
                relaxed = gi + si + (0 if same_region else 1)
                if len(matched) >= MIN_CASES:
                    break
            if len(matched) >= MIN_CASES:
                break
        if len(matched) >= MIN_CASES:
            break

    if len(matched) < MIN_CASES:
        return SubscriptionForecast(
            ok=False, n_cases=len(matched),
            reason=f"유사 사례 {len(matched)}건(<{MIN_CASES}) — 수치 전망을 제시하지 않고 "
                   f"정성 판정으로 전환 (5.4.5)")

    rates = sorted(r.competition_rate for r in matched)
    if len(rates) >= 5:
        qs = quantiles(rates, n=5, method="inclusive")  # 20,40,60,80분위
        lo, hi = qs[0], qs[3]
    else:
        lo, hi = rates[0], rates[-1]
    shortfall = sum(1 for r in matched if not r.sold_out_in_order or r.competition_rate < 1.0)

    return SubscriptionForecast(
        ok=True, n_cases=len(matched), lo=lo, mid=median(rates), hi=hi,
        shortfall_prob=shortfall / len(matched),
        sensitivities=_sensitivity_notes(matched),
        filters_relaxed=relaxed,
    )
