"""좌표 유틸 — 직선거리·도보 시간 환산.

거리 기반 판정(역세권·정류장 접근성)에 쓰인다. 본 모듈이 산출하는 값은
**직선거리**이며 실제 보행 경로가 아니다. 도로·하천·고저차로 인해 실제
보행거리는 통상 1.2~1.4배 길어지므로, 접근성 라벨은 이 계수를 반영한다.
"""
from __future__ import annotations

import math

EARTH_R_M = 6_371_000.0

#: 직선거리 → 실제 보행거리 보정계수 (국내 도시부 통상값)
DETOUR_FACTOR = 1.3

#: 보행 속도 (m/분) — 성인 평균, 신호 대기 포함
WALK_M_PER_MIN = 67.0


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """두 좌표 사이의 대권 직선거리(m)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(min(1.0, math.sqrt(a)))


def walk_minutes(straight_m: float, detour: float = DETOUR_FACTOR) -> float:
    """직선거리(m)를 보정계수 적용 도보 시간(분)으로 환산한다."""
    return straight_m * detour / WALK_M_PER_MIN
