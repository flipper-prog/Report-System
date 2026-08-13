"""분양가 결정 시뮬레이터 — "그래서 얼마로?"에 근거를 붙여 답한다.

진단리포트는 "지금 가격이 어디쯤인가"를 말한다. 그러나 시행사·시공사가 실제로
결정해야 하는 것은 **분양가를 얼마로 잡을 것인가**다. 이 모듈은 후보 가격대를
훑으면서 각 가격에서 무엇이 어떻게 달라지는지를 같은 근거로 계산한다.

  · 밴드 위치      — 품질조정 비교 밴드 대비 하단/내/상단
  · 구매 가능 가구  — 실부담 시뮬레이션(LTV·DSR·금리)
  · 청약 전망      — 가격 갭이 바뀌면 유사 사례 매칭이 바뀌고 경쟁률도 바뀐다
  · 미달 위험      — 유사 사례 중 순위 내 미마감 비율
  · 총 분양수입    — 세대수 × 가격 (의사결정의 반대편 축)

**추천은 규칙이지 예언이 아니다.** 아래 두 조건을 동시에 만족하는 후보 중
가장 높은 가격을 권고하고, 그 조건을 문장으로 함께 낸다. 조건을 바꾸면 답도
바뀐다는 사실이 리포트에 그대로 드러나야 하기 때문이다.

  1) 미달 위험이 임계 이하
  2) 총취득원가가 비교 밴드 상단을 넘지 않음

두 조건을 만족하는 후보가 없으면 추천하지 않고 그 사실을 말한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from statistics import median
from typing import Optional

from .affordability import simulate
from .models import Site, TypeSpec, total_acquisition_cost
from .pricing import Band
from .subscription import SubscriptionForecast, predict

#: 기본 후보 가격대 — 현재 분양가 대비 배율
DEFAULT_STEPS = (0.90, 0.925, 0.95, 0.975, 1.00, 1.025, 1.05, 1.075, 1.10)

#: 권고 임계 — 미달 위험이 이보다 높으면 후보에서 제외
SHORTFALL_LIMIT = 0.20

#: 구매 가능 가구 비율이 이보다 낮으면 경고를 붙인다(제외하지는 않는다)
THIN_DEMAND = 0.10

#: 비교 밴드가 없을 때의 표기. 이 상태에서는 가격 위치를 판단할 수 없으므로
#: 권고 후보에서 제외한다 — 근거 없이 가격을 권하지 않는다.
NO_BAND = "표본 부족"


@dataclass
class PriceOption:
    multiplier: float
    total_revenue: int                 # 전체 세대 분양수입(옵션 제외)
    subject_ppsm: float                # 총취득원가 ㎡당(타입 중위)
    band_label: str
    gap_pct: float                     # 시장 대비 가격 갭
    eligible_share: Optional[float]    # 기준 금리 구매 가능 가구 비율
    forecast: SubscriptionForecast
    notes: list[str] = field(default_factory=list)

    @property
    def shortfall(self) -> Optional[float]:
        return self.forecast.shortfall_prob if self.forecast.ok else None

    @property
    def has_band(self) -> bool:
        """비교 밴드 근거가 있는가. 없으면 가격 위치를 말할 수 없다."""
        return self.band_label != NO_BAND

    @property
    def within_band(self) -> bool:
        """밴드 상단을 넘지 않는가. 밴드 자체가 없으면 참이라고 하지 않는다."""
        return self.has_band and "상단" not in self.band_label


@dataclass
class PriceDecision:
    options: list[PriceOption] = field(default_factory=list)
    recommended: Optional[PriceOption] = None
    criteria: str = ""
    reason: str = ""
    limitations: list[str] = field(default_factory=list)

    def as_markdown(self) -> str:
        L = ["| 분양가 | 총 분양수입 | ㎡당 취득원가 | 밴드 위치 | 시장 갭 | "
             "구매 가능 가구 | 예상 경쟁률 | 미달 위험 |",
             "|--------|-------------|----------------|-----------|---------|"
             "----------------|-------------|-----------|"]
        for o in self.options:
            mark = " **←권고**" if o is self.recommended else ""
            share = f"{o.eligible_share:.0%}" if o.eligible_share is not None else "—"
            if o.forecast.ok:
                rate = f"{o.forecast.lo:.1f}~{o.forecast.hi:.1f}:1"
                sf = f"{o.forecast.shortfall_prob:.0%}"
            else:
                rate, sf = "표본 부족", "—"
            L.append(f"| {o.multiplier:+.1%}{mark} | {o.total_revenue/1e8:,.0f}억 | "
                     f"{o.subject_ppsm/1e4:,.0f}만원 | {o.band_label} | "
                     f"{o.gap_pct:+.1f}% | {share} | {rate} | {sf} |")
        L += ["", f"**판단 기준**: {self.criteria}", "", f"**결과**: {self.reason}"]
        if self.limitations:
            L.append("")
            L += [f"- {x}" for x in self.limitations]
        return "\n".join(L)


def _scaled_types(site: Site, mult: float) -> list[TypeSpec]:
    """분양가만 배율 조정한 타입 사양. 유상옵션은 그대로 둔다."""
    return [replace(t, base_price=int(round(t.base_price * mult)))
            for t in site.types]


def sweep(site: Site, bands: list[Band], market_ppsm: float,
          incomes: list[float], sub_history: list, concurrent_supply: int,
          steps: tuple[float, ...] = DEFAULT_STEPS,
          shortfall_limit: float = SHORTFALL_LIMIT) -> PriceDecision:
    """후보 가격대를 훑고, 규칙에 따라 하나를 권고한다.

    market_ppsm 은 정제된 실거래의 ㎡당 중위값이다. 가격 갭은 총취득원가 기준
    ㎡단가와의 차이로 계산하며, 이는 청약 전망의 매칭 조건으로 그대로 들어간다.
    """
    dec = PriceDecision()
    band_by_type = {b.type_name: b for b in bands if b.level == "타입"}

    for mult in steps:
        types = _scaled_types(site, mult)
        subj_ppsm = median(total_acquisition_cost(t) / t.area_m2 for t in types)
        gap = (subj_ppsm - market_ppsm) / market_ppsm * 100 if market_ppsm else 0.0

        labels = []
        for t in types:
            b = band_by_type.get(t.name)
            if b is None:
                continue
            p = total_acquisition_cost(t) / t.area_m2
            labels.append("하단" if p <= b.q25 else ("내" if p <= b.q75 else "상단"))
        band_label = (NO_BAND if not labels
                      else ("상단" if "상단" in labels
                            else ("하단" if all(l == "하단" for l in labels) else "내")))

        shares = []
        for t in types:
            r = simulate(t, incomes)
            if len(r.scenarios) > 1 and r.scenarios[1]["eligible_share"] is not None:
                shares.append(r.scenarios[1]["eligible_share"])
        share = sum(shares) / len(shares) if shares else None

        fc = predict(sub_history, site.region, gap, concurrent_supply)
        opt = PriceOption(
            multiplier=mult - 1.0,
            total_revenue=sum(t.base_price * t.units for t in types),
            subject_ppsm=subj_ppsm, band_label=band_label, gap_pct=gap,
            eligible_share=share, forecast=fc)
        if share is not None and share < THIN_DEMAND:
            opt.notes.append("구매 가능 가구 비율이 얇음")
        dec.options.append(opt)

    dec.criteria = (f"미달 위험 {shortfall_limit:.0%} 이하 · 총취득원가가 비교 밴드 "
                    "상단을 넘지 않음 — 두 조건을 동시에 만족하는 후보 중 최고가")

    feasible = [o for o in dec.options
                if o.within_band and o.shortfall is not None
                and o.shortfall <= shortfall_limit]
    if feasible:
        dec.recommended = max(feasible, key=lambda o: o.multiplier)
        r = dec.recommended
        parts = [f"현재 분양가 대비 {r.multiplier:+.1%}",
                 f"밴드 {r.band_label}",
                 f"미달 위험 {r.shortfall:.0%}"]
        if r.eligible_share is not None:
            parts.append(f"구매 가능 가구 {r.eligible_share:.0%}")
        dec.reason = " · ".join(parts)
        if r.notes:
            dec.reason += " (" + " · ".join(r.notes) + ")"
    else:
        # 권고하지 않는 사유를 구분해서 말한다 — 세 경우의 대응이 서로 다르다.
        if not any(o.has_band for o in dec.options):
            dec.reason = ("비교 밴드 표본이 부족해 가격 위치를 판단할 수 없음 — "
                          "근거 없이 가격을 권고하지 않음")
        elif all(o.shortfall is None for o in dec.options):
            dec.reason = ("유사 청약 사례가 부족해 미달 위험을 산출할 수 없음 — "
                          "가격 권고를 제시하지 않음")
        else:
            dec.reason = ("두 조건을 동시에 만족하는 후보 없음 — "
                          "원가·상품 구성 재검토 필요")

    dec.limitations = [
        "권고는 위 두 조건에 따른 규칙 결과이며 예측이 아니다. 조건을 바꾸면 "
        "권고도 바뀐다 [LIMITATION]",
        "청약 전망은 유사 사례 경험분포 기반 구간이며 제도·시장 변화는 반영되지 "
        "않는다 [FORECAST]",
        "총 분양수입은 유상옵션·잔여 세대 리스크를 제외한 단순 합계다",
    ]
    return dec
