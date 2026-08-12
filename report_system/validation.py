"""입력 검증 (부록 A.3의 6종 검증 + A.8 치명적 결함 판정).

치명 결함이 있으면 분석을 진행하지 않는다 — "분석 불가"의 정직한 판정이
분석 역량의 일부라는 원칙(제안서 5.10.2)의 구현.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import Site, Transaction


@dataclass
class Issue:
    code: str
    message: str
    fatal: bool = False


def validate_site(site: Site, asof: date) -> list[Issue]:
    issues: list[Issue] = []

    # 대상 미확정 (치명)
    if not site.address or site.lat == 0 or site.lng == 0:
        issues.append(Issue("SITE_UNRESOLVED", "현장 주소·좌표가 확정되지 않음", fatal=True))

    # 정합 검증: 세대수 합계
    type_sum = sum(t.units for t in site.types)
    if type_sum != site.total_units:
        issues.append(Issue(
            "UNIT_MISMATCH",
            f"타입별 세대 합({type_sum})이 총 세대수({site.total_units})와 불일치", fatal=True))

    # 단위 검증: 가격·면적의 상식 범위
    for t in site.types:
        if t.area_m2 <= 10 or t.area_m2 > 300:
            issues.append(Issue("AREA_RANGE", f"{t.name}: 전용면적 {t.area_m2}㎡ 범위 이상", fatal=True))
        if t.base_price < 10_000_000:
            issues.append(Issue("PRICE_UNIT", f"{t.name}: 분양가 단위 의심({t.base_price:,}원)", fatal=True))

    # 시간 검증: 입주예정일
    if site.expected_movein and site.expected_movein < asof:
        issues.append(Issue("MOVEIN_PAST", "입주예정일이 분석 기준일 이전", fatal=False))

    return issues


def validate_transactions(txs: list[Transaction], asof: date) -> list[Issue]:
    issues: list[Issue] = []
    future = [t for t in txs if t.trade_date > asof]
    if future:
        # 미래 정보 누출 (치명) — 백테스트 규율의 전제 (5.4.2)
        issues.append(Issue(
            "FUTURE_LEAK",
            f"분석 기준일({asof}) 이후 거래 {len(future)}건이 입력에 포함됨", fatal=True))
    for t in txs:
        if t.price <= 0 or t.area_m2 <= 0:
            issues.append(Issue("TX_INVALID", f"{t.complex_id} {t.trade_date}: 가격/면적 오류", fatal=True))
            break
    return issues


def has_fatal(issues: list[Issue]) -> bool:
    return any(i.fatal for i in issues)
