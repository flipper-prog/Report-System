"""경쟁 현장 모니터링 (P2-6).

경쟁 현장의 분양가·혜택·잔여 세대는 대부분 공개 API가 없어 수기 수집(MANUAL)에
의존한다. 본 모듈은 수집 주기와 변경 감지를 규칙화하여, 변화가 확인되면
조기경보로 승격시키고 어떤 판정을 갱신해야 하는지 지정한다.

수집 이력이 오래되면 '신선도 경고'를 발생시켜 방치를 막는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .alerts import Alert

STALE_DAYS = 30            # 이 기간 넘게 갱신 없으면 신선도 경고
PRICE_MATERIAL = 0.02      # 분양가 2% 이상 변동은 유의
REMAIN_SURGE = 0.20        # 잔여 세대 20% 이상 증가는 유의(판매 정체 신호)


@dataclass
class CompetitorSnapshot:
    name: str
    asof: date
    price_per_m2: float          # 대표 타입 ㎡당 분양가
    remaining_units: int
    incentives: list[str] = field(default_factory=list)   # 혜택(중도금 무이자 등)
    note: str = ""


def diff(old: CompetitorSnapshot, new: CompetitorSnapshot) -> list[Alert]:
    alerts: list[Alert] = []

    if old.price_per_m2 > 0:
        delta = (new.price_per_m2 - old.price_per_m2) / old.price_per_m2
        if abs(delta) >= PRICE_MATERIAL:
            direction = "인하" if delta < 0 else "인상"
            alerts.append(Alert(
                "경쟁 현장",
                f"{new.name}: 분양가 {direction} {abs(delta):.1%} "
                f"({old.price_per_m2/1e4:,.0f}→{new.price_per_m2/1e4:,.0f}만원/㎡)",
                ["가격 판정", "비교 논리", "게시 중 광고 문구"]))

    added = [i for i in new.incentives if i not in old.incentives]
    removed = [i for i in old.incentives if i not in new.incentives]
    if added:
        alerts.append(Alert(
            "경쟁 현장", f"{new.name}: 혜택 추가 — {', '.join(added)}",
            ["가격 판정", "상담 반론 대응"]))
    if removed:
        alerts.append(Alert(
            "경쟁 현장", f"{new.name}: 혜택 축소 — {', '.join(removed)}",
            ["가격 판정"]))

    if old.remaining_units > 0:
        rd = (new.remaining_units - old.remaining_units) / old.remaining_units
        if rd >= REMAIN_SURGE:
            alerts.append(Alert(
                "경쟁 현장",
                f"{new.name}: 잔여 세대 증가 {old.remaining_units}→{new.remaining_units} "
                f"(경쟁 현장 판매 정체 — 판촉 강화 가능성)",
                ["공급·환금성 판정", "판촉 시점 판단"]))
        elif rd <= -REMAIN_SURGE:
            alerts.append(Alert(
                "경쟁 현장",
                f"{new.name}: 잔여 세대 감소 {old.remaining_units}→{new.remaining_units} "
                f"(경쟁 현장 소진 — 수요 이동 확인 필요)",
                ["수요 판정"]))
    return alerts


def freshness(snapshots: list[CompetitorSnapshot], asof: date) -> list[Alert]:
    """수집 신선도 점검 — 오래된 항목을 경보로 노출한다."""
    out: list[Alert] = []
    for s in snapshots:
        age = (asof - s.asof).days
        if age > STALE_DAYS:
            out.append(Alert(
                "경쟁 현장",
                f"{s.name}: 마지막 수집 {s.asof} ({age}일 경과) — 재수집 필요",
                ["경쟁 현장 데이터"]))
    return out


def scan(old: list[CompetitorSnapshot], new: list[CompetitorSnapshot],
         asof: date) -> list[Alert]:
    old_by = {s.name: s for s in old}
    alerts: list[Alert] = []
    for s in new:
        prev = old_by.get(s.name)
        if prev is not None:
            alerts += diff(prev, s)
    alerts += freshness(new, asof)
    return alerts
