"""실부담 시뮬레이터 (P1-4, 제안서 표 5-14-2).

산출: 시점별 자기자본 요구액, LTV·DSR 한도 내 월 상환 구간(금리 시나리오별),
구매 가능 가구 비율(소득분포 결합 → 수요 판정 ②의 입력).

가정은 파라미터로 노출되며 리포트에 LIMITATION으로 병기된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import TypeSpec, total_acquisition_cost

TERM_YEARS = 30
EQUITY_INCOME_MULTIPLE = 4.0   # 가용 자기자본 ≈ 연소득 ×4 (가정, LIMITATION)

#: 구매 가능 가구 비율을 수치로 제시하기 위한 최소 소득 표본.
#: 이보다 적으면 비율을 내지 않는다(None). 모르는 것을 0%로 적으면 '수요가
#: 전혀 없다'는 전혀 다른 주장이 되기 때문이다.
MIN_INCOME_SAMPLES = 30


def annuity_monthly(principal: float, annual_rate: float, years: int = TERM_YEARS) -> float:
    r = annual_rate / 12
    n = years * 12
    if r == 0:
        return principal / n
    return principal * r * (1 + r) ** n / ((1 + r) ** n - 1)


def max_loan_by_dsr(annual_income: float, dsr_cap: float, annual_rate: float,
                    years: int = TERM_YEARS) -> float:
    """DSR 한도 내 최대 원리금균등 대출액."""
    monthly_cap = annual_income * dsr_cap / 12
    r = annual_rate / 12
    n = years * 12
    if r == 0:
        return monthly_cap * n
    return monthly_cap * ((1 + r) ** n - 1) / (r * (1 + r) ** n)


@dataclass
class AffordabilityResult:
    type_name: str
    acquisition_cost: int
    ltv_cap: float
    dsr_cap: float
    scenarios: list[dict] = field(default_factory=list)
    n_incomes: int = 0
    note: str = ""

    @property
    def share_available(self) -> bool:
        return self.n_incomes >= MIN_INCOME_SAMPLES
    # 각 원소: {"rate": .., "loan": .., "equity_required": .., "monthly": ..,
    #           "eligible_share": ..}


def simulate(
    t: TypeSpec,
    incomes: list[float],          # 생활권·통근권 가구 연소득 표본 (L5)
    ltv_cap: float = 0.5,
    dsr_cap: float = 0.4,
    rates: tuple[float, ...] = (0.030, 0.040, 0.055),
    equity_multiple: float = EQUITY_INCOME_MULTIPLE,
) -> AffordabilityResult:
    cost = total_acquisition_cost(t)
    res = AffordabilityResult(t.name, cost, ltv_cap, dsr_cap,
                              n_incomes=len(incomes))
    if not res.share_available:
        res.note = (f"소득 표본 {len(incomes)}건(<{MIN_INCOME_SAMPLES}) — "
                    "구매 가능 가구 비율 미산출 [LIMITATION]")

    for rate in rates:
        loan_ltv = cost * ltv_cap
        eligible = 0
        monthly_at_ltv = annuity_monthly(loan_ltv, rate)
        for inc in incomes:
            equity_avail = inc * equity_multiple
            loan_needed = max(cost - equity_avail, 0.0)
            loan_cap = min(loan_ltv, max_loan_by_dsr(inc, dsr_cap, rate))
            if loan_needed <= loan_cap:
                eligible += 1
        res.scenarios.append({
            "rate": rate,
            "loan": loan_ltv,
            "equity_required": cost - loan_ltv,
            "monthly": monthly_at_ltv,
            "eligible_share": (eligible / len(incomes)
                               if res.share_available else None),
        })
    return res
