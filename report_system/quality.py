"""데이터 적합성 평가 (부록 A.7: 5축 100점 → A~D 등급).

등급별 사용 범위 제한(부록 A.3 표)은 파이프라인에서 강제된다:
C등급은 탐색용, D등급은 분석·광고 사용 금지.
"""
from __future__ import annotations

from .models import DataGrade, DatasetMeta


def score(meta: DatasetMeta) -> int:
    parts = [
        min(max(meta.spatial_fit, 0), 25),
        min(max(meta.temporal_fit, 0), 25),
        min(max(meta.definition_quality, 0), 20),
        min(max(meta.reproducibility, 0), 15),
        min(max(meta.license_ok, 0), 15),
    ]
    return sum(parts)


def grade(meta: DatasetMeta) -> DataGrade:
    # 권한 불명은 점수와 무관하게 사용 금지 (A.8 치명 결함)
    if meta.license_ok <= 0:
        return DataGrade.D
    s = score(meta)
    if s >= 80:
        return DataGrade.A
    if s >= 65:
        return DataGrade.B
    if s >= 50:
        return DataGrade.C
    return DataGrade.D


def usable_for_numbers(g: DataGrade) -> bool:
    """수치·모델·고객 설명에 사용 가능한가."""
    return g in (DataGrade.A, DataGrade.B)
