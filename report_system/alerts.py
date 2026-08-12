"""조기경보 체계 (제안서 5.11.2, 표 5-25).

개발계획·시장·공급·표현 유효성의 4개 신호 유형을 스냅숏 비교로 감지한다.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import CatalystPlan, ListingSnapshot, MaturityStage

LISTING_SURGE = 0.30       # 매물 30% 이상 급증
ASK_GAP_WIDEN = 0.07       # 호가-실거래 갭 7%p 이상 확대


@dataclass
class Alert:
    category: str        # 개발계획 | 시장 신호 | 공급 신호 | 표현 유효성
    message: str
    refresh_targets: list[str]


def scan_catalyst(old: CatalystPlan, new: CatalystPlan) -> list[Alert]:
    alerts: list[Alert] = []
    if new.stage != old.stage:
        direction = "진전" if list(MaturityStage).index(new.stage) > list(MaturityStage).index(old.stage) else "후퇴"
        alerts.append(Alert(
            "개발계획",
            f"{new.name}: 단계 {old.stage.value} → {new.stage.value} ({direction})",
            ["성숙도 판정", "시나리오", "촉매카드", "게시 중 광고 문구"]))
    if new.budget_secured < old.budget_secured:
        alerts.append(Alert(
            "개발계획",
            f"{new.name}: 확보 예산 감소 ({old.budget_secured/1e8:,.0f}억 → {new.budget_secured/1e8:,.0f}억)",
            ["실현 가능성", "촉매카드", "게시 중 광고 문구"]))
    return alerts


def scan_market(old: ListingSnapshot, new: ListingSnapshot) -> list[Alert]:
    alerts: list[Alert] = []
    if old.listings and (new.listings - old.listings) / old.listings >= LISTING_SURGE:
        alerts.append(Alert(
            "시장 신호",
            f"매물량 급증: {old.listings} → {new.listings}건",
            ["가격 판정", "환금성 평가"]))
    old_gap = (old.ask_ppsm - old.traded_ppsm) / old.traded_ppsm
    new_gap = (new.ask_ppsm - new.traded_ppsm) / new.traded_ppsm
    if new_gap - old_gap >= ASK_GAP_WIDEN:
        alerts.append(Alert(
            "시장 신호",
            f"호가-실거래 갭 확대: {old_gap:.1%} → {new_gap:.1%}",
            ["가격 판정", "시나리오"]))
    return alerts


def scan_supply(old_adjusted: float, new_adjusted: float) -> list[Alert]:
    if old_adjusted and (new_adjusted - old_adjusted) / old_adjusted >= 0.20:
        return [Alert(
            "공급 신호",
            f"확률조정 공급 20%+ 증가: {old_adjusted:,.0f} → {new_adjusted:,.0f}세대",
            ["확률조정 공급", "판매 시한 판단", "게시 중 광고 문구"])]
    return []
