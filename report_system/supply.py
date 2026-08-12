"""확률조정 공급 (부록 A.9).

확률조정 공급 = Σ(각 공급 물량 × 해당 단계에서 실제 입주 기간 내 실현될 가능성)
발표 물량의 단순 합산을 배제한다.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import SupplyItem, SupplyStage

STAGE_REALIZATION = {
    SupplyStage.PERMIT: 0.55,
    SupplyStage.START: 0.90,
    SupplyStage.SALE: 0.95,
    SupplyStage.MOVEIN: 1.00,
}


@dataclass
class SupplyAssessment:
    nominal_units: int            # 발표 물량 단순 합
    adjusted_units: float         # 확률조정 물량
    window_months: int
    items: list[tuple[str, int, str, float]]  # (이름, 세대, 단계, 조정치)

    @property
    def burden_label(self) -> str:
        # 라벨 기준은 현장 세대수 대비 비율로 판정하는 것이 원칙이나,
        # 모듈 단독 사용을 위해 절대 규모 기준의 보조 라벨을 제공한다.
        if self.adjusted_units >= 3000:
            return "공급 부담 높음"
        if self.adjusted_units >= 1000:
            return "공급 부담 중간"
        return "공급 부담 낮음"


def probability_adjusted(items: list[SupplyItem], window_months: int = 36) -> SupplyAssessment:
    rows: list[tuple[str, int, str, float]] = []
    nominal = 0
    adjusted = 0.0
    for it in items:
        if it.months_to_movein > window_months:
            continue
        w = STAGE_REALIZATION[it.stage]
        nominal += it.units
        adj = it.units * w
        adjusted += adj
        rows.append((it.name, it.units, it.stage.value, adj))
    return SupplyAssessment(nominal, adjusted, window_months, rows)
