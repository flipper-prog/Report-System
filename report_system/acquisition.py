"""총취득원가 — 비교는 같은 기준으로 해야 성립한다.

이 시스템의 가격 판정은 "총취득원가 기준으로 비교한다"를 원칙으로 삼는다.
그런데 원칙이 한쪽에만 적용되면 비교 자체가 틀어진다.

  - 현장(subject): 분양가 + 유상옵션 + 취득 부대비용
  - 비교 거래(comparable): 실거래 신고가 **그대로**

비교단지를 사는 사람도 취득세를 낸다. 한쪽에만 세금을 얹으면 현장이 실제보다
비싸 보이고, 판정 ①이 '밴드 상단(가격 저항 위험)' 쪽으로 계통적으로 기운다.
그래서 **양쪽 모두** 같은 규칙으로 총취득원가로 환산한다.

세율도 정액이 아니다. 주택 유상거래 취득세는 가액 구간별 누진이므로, 6억 이하
주택에 9억 초과 세율(3.3%)을 적용하면 2%p 넘게 과대계상된다.

  취득세    6억 이하 1% · 6~9억 누진 · 9억 초과 3%
  지방교육세 취득세율의 10%
  농어촌특별세 전용 85㎡ 초과 시 0.2% (85㎡ 이하 비과세)

전제 — 1주택 유상거래, 다주택·법인 중과 미적용, 생애최초 감면 미적용.
중개보수·법무비용·인지세는 포함하지 않는다(현장은 중개보수가 없어 오히려
비교 거래 쪽이 과소계상되는 방향이므로, 현장에 유리하게 기울지 않는다).
이 전제는 리포트에 LIMITATION으로 병기된다.
"""
from __future__ import annotations

#: 구간 경계 (원)
BRACKET_LOW = 600_000_000
BRACKET_HIGH = 900_000_000
#: 구간별 취득세 기본세율
RATE_LOW = 0.01
RATE_HIGH = 0.03
#: 지방교육세 = 취득세율 × 10%
EDU_TAX_MULTIPLIER = 0.10
#: 농어촌특별세 — 전용면적 기준 초과분에만 부과
FARM_TAX_RATE = 0.002
FARM_TAX_AREA_M2 = 85.0

#: 다주택 중과세율 (조정대상지역 2주택·비조정 3주택 기준) — 강건성 검사용
HEAVY_RATE = 0.08

RATE_NOTE = ("취득세·지방교육세·농특세 (1주택 유상거래, 중과·감면 미적용). "
             "중개보수·법무비용 미포함")


def base_rate(price: float) -> float:
    """주택 유상거래 취득세 기본세율 (가액 구간별 누진)."""
    if price <= 0:
        return 0.0
    if price <= BRACKET_LOW:
        return RATE_LOW
    if price <= BRACKET_HIGH:
        # 6~9억 구간은 (가액(억) × 2/3 − 3) % 로 선형 누진한다.
        pct = (price / 1e8) * (2 / 3) - 3.0
        return max(RATE_LOW, min(RATE_HIGH, pct / 100))
    return RATE_HIGH


def tax_rate(price: float, area_m2: float,
             base: "float | None" = None) -> float:
    """총 취득 부대비용률.

    base 를 지정하면 기본세율을 대체한다(다주택 중과 등 시나리오 검사용).
    """
    b = base_rate(price) if base is None else base
    rate = b * (1 + EDU_TAX_MULTIPLIER)
    if area_m2 > FARM_TAX_AREA_M2:
        rate += FARM_TAX_RATE
    return rate


def total_cost(price: float, area_m2: float,
               base: "float | None" = None) -> float:
    """취득 부대비용을 더한 총취득원가."""
    return price * (1 + tax_rate(price, area_m2, base))


def total_ppsm(price: float, area_m2: float,
               base: "float | None" = None) -> float:
    """총취득원가 기준 ㎡단가. 비교의 양쪽 모두 이 함수를 통과한다."""
    if area_m2 <= 0:
        return 0.0
    return total_cost(price, area_m2, base) / area_m2
