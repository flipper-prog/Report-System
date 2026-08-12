"""환금성 분석 (제안서 5.5 / L11).

"나중에 팔 수 있는가"는 투자 수요의 핵심 질문이며, 유리한 답이 나오지 않아도
그대로 기록한다(5.5 원칙).

산출 가능
  - 연환산 거래 회전율 (거래건수 / 세대수)
  - 가격 분산 (사분위 분산계수) — 낮을수록 가격 발견이 안정적
  - 평균 거래 간격 → 매도 소요기간 프록시
산출 불가(커넥터 미구현)
  - 전세 유동성, 실제 매물 체류일수 → LIMITATION으로 표기
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from statistics import median

from .models import Comparable, Transaction


@dataclass
class LiquidityResult:
    turnover_pct_year: float | None      # 연환산 회전율(%)
    dispersion: float | None             # (q75-q25)/중위
    months_between_trades: float | None  # 세대당 평균 거래 간격(개월)
    n_trades: int
    window_months: int
    limitations: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.turnover_pct_year is None:
            return "판정 불가"
        if self.turnover_pct_year >= 6.0:
            return "환금성 양호"
        if self.turnover_pct_year >= 3.0:
            return "환금성 보통"
        return "환금성 취약"

    def as_rationale(self) -> str:
        if self.turnover_pct_year is None:
            return "환금성: 비교단지 세대수 정보 부족으로 판정 불가"
        parts = [f"연환산 회전율 {self.turnover_pct_year:.1f}% ({self.label})"]
        if self.dispersion is not None:
            parts.append(f"가격 분산 {self.dispersion:.2f}")
        if self.months_between_trades is not None:
            parts.append(f"세대당 평균 거래 간격 {self.months_between_trades:.0f}개월")
        return "환금성: " + ", ".join(parts)


def analyze(txs: list[Transaction], comps: dict[str, Comparable],
            asof: date, window_months: int = 24) -> LiquidityResult:
    cutoff_key = (asof.year * 12 + asof.month) - window_months
    recent = [t for t in txs
              if not t.canceled and (t.trade_date.year * 12 + t.trade_date.month) > cutoff_key]

    lims: list[str] = ["전세 유동성·매물 체류일수는 커넥터 미구현으로 미반영 [LIMITATION]"]
    if not recent:
        return LiquidityResult(None, None, None, 0, window_months, lims)

    # 회전율: 관측 단지들의 세대수 합 대비 거래건수
    unit_total = sum(c.units for cid, c in comps.items()
                     if c.units and any(t.complex_id == cid for t in recent))
    turnover = None
    months_between = None
    if unit_total > 0:
        turnover = len(recent) / unit_total * (12 / window_months) * 100
        per_unit_year = len(recent) / unit_total * (12 / window_months)
        months_between = (12 / per_unit_year) if per_unit_year > 0 else None
    else:
        lims.append("비교단지 세대수 미입력 — 회전율 산출 불가. 설정에 units 기입 권고")

    ppsm = sorted(t.price / t.area_m2 for t in recent if t.area_m2 > 0)
    dispersion = None
    if len(ppsm) >= 4:
        q1 = ppsm[len(ppsm) // 4]
        q3 = ppsm[(3 * len(ppsm)) // 4]
        med = median(ppsm)
        dispersion = (q3 - q1) / med if med else None

    return LiquidityResult(turnover, dispersion, months_between,
                           len(recent), window_months, lims)
