"""현장 반응 피드백 — 분석을 검증하는 역방향 경로 (P1-3, 제안서 5.11.3).

CRM에서 축적된 거절 사유·상담 분포로 4개 판정의 정합성을 점검하고,
어긋나는 항목을 재검토 대상으로 지정한다.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import FieldFeedback

PRICE_REJECTION_ALERT = 0.40    # 거절 중 '가격' 비중 임계
COMPETITOR_ALERT = 0.25         # 거절 중 '경쟁현장' 비중 임계


@dataclass
class ConsistencyFlag:
    verdict: str          # 어긋난 판정 (① ~ ④)
    signal: str
    action: str


#: 방문객 거주지가 통계상 유입 출발지와 일치하는 최소 비율
MIGRATION_ALIGN_ALERT = 0.25


def check(fb: FieldFeedback, price_verdict_positive: bool,
          expected_home_regions: list[str],
          catalyst_ad_active: bool,
          migration: object | None = None) -> list[ConsistencyFlag]:
    flags: list[ConsistencyFlag] = []
    total_rej = sum(fb.rejections.values()) or 1

    # ① 가격 판정 vs 가격 거절
    price_share = fb.rejections.get("가격", 0) / total_rej
    if price_verdict_positive and price_share >= PRICE_REJECTION_ALERT:
        flags.append(ConsistencyFlag(
            "① 현재 가격 위치",
            f"거절 사유 중 가격 비중 {price_share:.0%} — 판정(경쟁력 있음)과 상충",
            "가격 갭 재산정, 총취득원가·실부담 설명 자료 점검"))

    # ② 수요 판정 vs 방문객 거주지 분포
    if fb.visitor_home_regions:
        top = max(fb.visitor_home_regions, key=fb.visitor_home_regions.get)  # type: ignore[arg-type]
        if expected_home_regions and top not in expected_home_regions:
            flags.append(ConsistencyFlag(
                "② 수요 지속성",
                f"상담 고객 최다 거주지({top})가 타깃 생활권 가설({', '.join(expected_home_regions)}) 밖",
                "수요 레이어 가중치·광고 타깃 지역 재검토"))

    # ② 수요 판정 vs 인구이동 유입 출발지 (L3 교차검증)
    if migration is not None and fb.visitor_home_regions:
        aligned = migration.visitor_alignment(fb.visitor_home_regions)
        if aligned is not None and aligned[0] < MIGRATION_ALIGN_ALERT:
            flags.append(ConsistencyFlag(
                "② 수요 지속성",
                f"인구이동 교차검증 — {aligned[1]}",
                "유입 출발지 기준으로 광고 타깃 지역 재설정, 수요 가정 재검토"))

    # ③ 공급 위험 vs 경쟁현장 이탈
    comp_share = fb.rejections.get("경쟁현장", 0) / total_rej
    if comp_share >= COMPETITOR_ALERT:
        flags.append(ConsistencyFlag(
            "③ 공급·환금성 위험",
            f"경쟁현장 사유 이탈 {comp_share:.0%}",
            "경쟁 물량 평가·비교 논리 갱신"))

    # ④ 촉매 표현 vs 호재 불신
    transport_share = fb.rejections.get("교통", 0) / total_rej
    if catalyst_ad_active and transport_share >= 0.20:
        flags.append(ConsistencyFlag(
            "④ 촉매·실행 가능성",
            f"교통 관련 거절 {transport_share:.0%} — 호재 소구가 상담에서 반박되는 정황",
            "촉매카드 보강 또는 해당 문구 표현 등급 하향"))

    return flags
