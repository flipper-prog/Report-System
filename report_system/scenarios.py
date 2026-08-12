"""조건부 가격 시나리오 (제안서 5.7).

단일 예측값을 만들지 않는다. 앵커(품질조정 밴드 중위)에 명시된 드라이버의
가정별 조정을 적용해 하방·기준·상방 구간을 만들고, 각 시나리오의 전제와
민감도를 함께 산출한다. 모든 결과는 FORECAST 등급이며 장부에 봉인된다.

드라이버
  1) 시장 추세  — 실거래 시계열 기울기 (timeseries.monthly_trend)
  2) 공급 부담  — 확률조정 공급 / 현장 세대수
  3) 금리       — 기준·상하방 시나리오의 조달 여건
  4) 촉매       — 성숙도×관련성 가중 (확정 단계일수록 기여)
계수는 파라미터로 노출되며 실데이터 백테스트로 교정 전까지 예시값이다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .catalyst import CatalystCard

HORIZON_MONTHS = 24

# 드라이버별 조정 계수 (연율 기준, 백테스트 교정 대상)
TREND_CARRY = {"low": 0.30, "base": 0.55, "high": 0.80}   # 추세 지속률
SUPPLY_ELASTICITY = -0.35        # 공급배수 1 증가당 연 %p
SUPPLY_RATIO_CAP = 10.0
RATE_DELTA = {"low": +1.5, "base": 0.0, "high": -1.0}     # %p 금리 변화 가정
RATE_ELASTICITY = -1.8           # 금리 1%p 상승당 연 %p
CATALYST_MAX = 2.0               # 촉매 최대 기여 연 %p
BAND_FLOOR, BAND_CAP = -12.0, 12.0   # 연 %p 클램프


@dataclass
class ScenarioLeg:
    name: str                    # 하방 / 기준 / 상방
    annual_pct: float            # 연 변화율(%)
    horizon_pct: float           # 기간(HORIZON_MONTHS) 누적 변화율(%)
    price_ppsm: float            # 앵커 대비 기간말 ㎡당 가격
    assumptions: list[str]


@dataclass
class ScenarioSet:
    anchor_ppsm: float
    horizon_months: int
    legs: list[ScenarioLeg]
    sensitivities: list[tuple[str, float]] = field(default_factory=list)  # (드라이버, |영향 연%p|)
    limitations: list[str] = field(default_factory=list)

    @property
    def low(self) -> ScenarioLeg: return self.legs[0]

    @property
    def base(self) -> ScenarioLeg: return self.legs[1]

    @property
    def high(self) -> ScenarioLeg: return self.legs[2]

    def top_driver(self) -> str:
        return self.sensitivities[0][0] if self.sensitivities else "판정 불가"


def _catalyst_bonus(cards: list[CatalystCard]) -> float:
    rel_w = {"상": 1.0, "중": 0.6, "하": 0.2}
    if not cards:
        return 0.0
    score = sum(c.feasibility * rel_w.get(c.relevance, 0.2) for c in cards)
    return min(score, 1.0) * CATALYST_MAX


def build(anchor_ppsm: float, trend_pct_year: float, supply_ratio: float,
          cards: list[CatalystCard], horizon_months: int = HORIZON_MONTHS,
          limitations: list[str] | None = None) -> ScenarioSet:
    supply_ratio = min(max(supply_ratio, 0.0), SUPPLY_RATIO_CAP)
    supply_effect = SUPPLY_ELASTICITY * supply_ratio
    cat_bonus = _catalyst_bonus(cards)

    legs: list[ScenarioLeg] = []
    for name, key in (("하방", "low"), ("기준", "base"), ("상방", "high")):
        trend_part = trend_pct_year * TREND_CARRY[key]
        rate_part = RATE_ELASTICITY * RATE_DELTA[key]
        cat_part = cat_bonus * (0.3 if key == "low" else 0.7 if key == "base" else 1.0)
        annual = max(BAND_FLOOR, min(BAND_CAP,
                                     trend_part + supply_effect + rate_part + cat_part))
        horizon = ((1 + annual / 100) ** (horizon_months / 12) - 1) * 100
        legs.append(ScenarioLeg(
            name=name,
            annual_pct=round(annual, 2),
            horizon_pct=round(horizon, 2),
            price_ppsm=anchor_ppsm * (1 + horizon / 100),
            assumptions=[
                f"시장 추세 {trend_pct_year:+.1f}%/년의 {TREND_CARRY[key]:.0%} 지속",
                f"금리 {RATE_DELTA[key]:+.1f}%p 변화",
                f"확률조정 공급배수 {supply_ratio:.1f}배 반영",
                f"촉매 기여 {cat_part:+.1f}%p",
            ]))

    sens = sorted([
        ("시장 추세", abs(trend_pct_year * (TREND_CARRY["high"] - TREND_CARRY["low"]))),
        ("금리", abs(RATE_ELASTICITY * (RATE_DELTA["low"] - RATE_DELTA["high"]))),
        ("공급 부담", abs(supply_effect)),
        ("촉매", cat_bonus * 0.7),
    ], key=lambda x: -x[1])

    lims = list(limitations or [])
    lims.append("계수(추세 지속률·공급 탄력성·금리 탄력성)는 백테스트 교정 전 예시값 [LIMITATION]")
    return ScenarioSet(anchor_ppsm, horizon_months, legs, sens, lims)
