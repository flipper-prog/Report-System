"""상품 유형별 분석 프로파일 (P2-2).

아파트와 오피스텔·지식산업센터·상가는 비교군 정의, 수요 레이어의 가중,
환금성 판정 기준이 서로 다르다. 동일 모델을 그대로 적용하면 오판이 발생하므로
상품별 프로파일로 파라미터를 분리한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ProductType(str, Enum):
    APARTMENT = "아파트"
    OFFICETEL = "오피스텔"
    KNOWLEDGE = "지식산업센터"
    RETAIL = "상가"


@dataclass
class Profile:
    product: ProductType
    area_tolerance: float           # 비교 거래 면적 허용 배수(±)
    max_dist_m: float               # 비교군 최대 거리
    min_samples_band: int           # 밴드 최소 표본
    demand_weights: dict[str, float]  # 카테고리 가중 (C1~C7)
    turnover_good_pct: float        # 환금성 '양호' 기준 회전율
    subscription_applicable: bool   # 청약 전망 적용 가능 여부
    notes: list[str] = field(default_factory=list)


_PROFILES: dict[ProductType, Profile] = {
    ProductType.APARTMENT: Profile(
        product=ProductType.APARTMENT,
        area_tolerance=0.20, max_dist_m=2000.0, min_samples_band=8,
        demand_weights={"C1": 1.0, "C2": 0.8, "C3": 0.8, "C4": 0.5,
                        "C5": 1.0, "C6": 1.0, "C7": 0.7},
        turnover_good_pct=6.0, subscription_applicable=True,
        notes=["표준 프로파일"]),

    ProductType.OFFICETEL: Profile(
        product=ProductType.OFFICETEL,
        area_tolerance=0.30, max_dist_m=1200.0, min_samples_band=6,
        demand_weights={"C1": 0.5, "C2": 1.0, "C3": 1.0, "C4": 0.8,
                        "C5": 0.8, "C6": 1.0, "C7": 0.6},
        turnover_good_pct=8.0, subscription_applicable=True,
        notes=["1~2인 가구·직주근접 수요 비중이 높아 C2·C3 가중 상향",
               "아파트 실거래를 비교군으로 쓰면 과대평가 — 오피스텔 거래로 한정 필요",
               "임대수익률이 가격 결정에 관여하므로 전월세 데이터 확보 시 반영 권고"]),

    ProductType.KNOWLEDGE: Profile(
        product=ProductType.KNOWLEDGE,
        area_tolerance=0.40, max_dist_m=3000.0, min_samples_band=5,
        demand_weights={"C1": 0.2, "C2": 1.0, "C3": 0.9, "C4": 0.6,
                        "C5": 0.6, "C6": 1.0, "C7": 0.8},
        turnover_good_pct=4.0, subscription_applicable=False,
        notes=["주거 수요(C1) 비중 최소, 산업·고용(C2)이 핵심 드라이버",
               "청약 제도 비적용 — 청약 전망 모듈을 실행하지 않음",
               "실거래 표본이 희소해 정성 판정 비중이 커짐"]),

    ProductType.RETAIL: Profile(
        product=ProductType.RETAIL,
        area_tolerance=0.50, max_dist_m=800.0, min_samples_band=5,
        demand_weights={"C1": 0.3, "C2": 0.6, "C3": 1.0, "C4": 1.0,
                        "C5": 0.4, "C6": 0.7, "C7": 0.6},
        turnover_good_pct=3.0, subscription_applicable=False,
        notes=["유동·소비(C3·C4)가 지배 변수. 상권 데이터 미확보 시 판정 신뢰도 낮음",
               "청약 제도 비적용",
               "임대료 기반 수익 환원이 주 평가 방식 — 본 모델은 보조 참고용"]),
}


def get(product: ProductType | str) -> Profile:
    if isinstance(product, str):
        product = ProductType(product)
    return _PROFILES[product]


def applicability_note(p: Profile) -> str:
    """리포트에 표기할 프로파일 적용 안내."""
    base = f"상품 유형: {p.product.value} (프로파일 {p.product.name.lower()})"
    if not p.subscription_applicable:
        base += " — 청약 전망 미적용"
    return base
