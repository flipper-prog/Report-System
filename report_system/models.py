"""도메인 모델. 부록 A.12의 핵심 테이블을 파이썬 자료구조로 구현한다."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


# ── 표현 등급 (제안서 5.9.1 / 14.5) ──────────────────────────────────────────

class ClaimGrade(str, Enum):
    FACT = "FACT"
    CALCULATION = "CALCULATION"
    INFERENCE = "INFERENCE"
    FORECAST = "FORECAST"
    LIMITATION = "LIMITATION"


class AdGrade(str, Enum):
    ALLOWED = "사용 가능"
    CONDITIONAL = "조건부 사용"
    FORBIDDEN = "사용 금지"


class DataGrade(str, Enum):
    A = "A"  # 핵심 분석에 직접 사용
    B = "B"  # 보정·보조근거와 함께 제한 사용
    C = "C"  # 탐색·가설용
    D = "D"  # 사용 금지


# ── 현장·상품 ────────────────────────────────────────────────────────────────

@dataclass
class TypeSpec:
    """분양 타입(평형)."""
    name: str                 # 예: "84A"
    area_m2: float            # 전용면적
    units: int
    base_price: int           # 분양가(원)
    option_cost: int = 0      # 유상옵션·발코니 확장 등
    floors: tuple[int, int] = (1, 20)


@dataclass
class Site:
    id: str
    name: str
    address: str
    lat: float
    lng: float
    total_units: int
    types: list[TypeSpec]
    expected_movein: Optional[date] = None
    region: str = ""          # 시장권 식별자
    brand_tier: int = 2       # 1(상위)~3


ACQUISITION_TAX_RATE = 0.033  # 취득세 등 부대비용 간이율(파라미터. 계약 전 확정)


def total_acquisition_cost(t: TypeSpec) -> int:
    """총취득원가 = 분양가 + 옵션 + 취득 부대비용(간이). (제안서 5.5)"""
    base = t.base_price + t.option_cost
    return int(base * (1 + ACQUISITION_TAX_RATE))


# ── 시장 데이터 ──────────────────────────────────────────────────────────────

@dataclass
class Comparable:
    """비교단지."""
    id: str
    name: str
    built_year: int
    units: int
    brand_tier: int
    dist_m: float             # 현장까지 네트워크 거리(m)
    region: str = ""
    is_presale_right: bool = False  # 분양권·입주권 여부 (P1-1)


@dataclass
class Transaction:
    complex_id: str
    trade_date: date
    area_m2: float
    floor: int
    price: int
    canceled: bool = False


@dataclass
class SubscriptionRecord:
    """시장권 청약 이력 1건 (청약홈 공개 데이터 기준)."""
    complex_id: str
    open_date: date
    units: int
    applicants: int
    region: str
    price_gap_pct: float      # 분양가와 주변 품질조정 시세의 격차(%)
    concurrent_supply: int    # 동시 분양·입주 물량(세대)
    sold_out_in_order: bool   # 순위 내 마감 여부

    @property
    def competition_rate(self) -> float:
        return self.applicants / self.units if self.units else 0.0


class SupplyStage(str, Enum):
    PERMIT = "인허가"
    START = "착공"
    SALE = "분양"
    MOVEIN = "입주예정"


@dataclass
class SupplyItem:
    name: str
    units: int
    stage: SupplyStage
    months_to_movein: int


@dataclass
class ListingSnapshot:
    """매물·호가 스냅숏 (P1-2 선행 신호)."""
    asof: date
    listings: int
    ask_ppsm: float           # 호가 ㎡당
    traded_ppsm: float        # 실거래 ㎡당


# ── 개발계획(촉매) ───────────────────────────────────────────────────────────

class MaturityStage(str, Enum):
    """성숙도 라벨 요약(부록 A.13 R0~R10 축약)."""
    IDEA = "검토"             # R0~R2
    PLANNED = "계획반영"       # R3~R4
    FEASIBILITY = "타당성"     # R5
    DESIGN = "설계·인가"       # R6~R7
    CONTRACT = "보상·계약"     # R8
    CONSTRUCTION = "착공"      # R9
    OPEN = "개통"             # R10


@dataclass
class CatalystPlan:
    id: str
    name: str
    stage: MaturityStage
    budget_total: int         # 총사업비(원)
    budget_secured: int       # 확보·집행 예산(원)
    dist_m: float             # 현장과의 거리
    time_saving_min: float    # 개통 시 이동시간 단축(분)
    negatives: list[str] = field(default_factory=list)   # 반대근거
    source_docs: list[str] = field(default_factory=list)  # 공식 원문


# ── 데이터셋 메타(적합성 평가 입력) ──────────────────────────────────────────

@dataclass
class DatasetMeta:
    layer: str                # 예: "L11 실거래"
    spatial_fit: int          # 0~25
    temporal_fit: int         # 0~25
    definition_quality: int   # 0~20
    reproducibility: int      # 0~15
    license_ok: int           # 0~15
    note: str = ""


# ── 근거·주장 ────────────────────────────────────────────────────────────────

@dataclass
class Claim:
    text: str
    grade: ClaimGrade
    ad_grade: AdGrade
    evidence: list[str] = field(default_factory=list)   # 근거 식별자·출처
    counter: list[str] = field(default_factory=list)    # 반대근거 (5.4.4)


# ── 현장 반응 (P1-3) ─────────────────────────────────────────────────────────

@dataclass
class FieldFeedback:
    """상담·거절 기록 집계. CRM(제9장)에서 유입."""
    total_consults: int
    rejections: dict[str, int]        # {"가격": n, "대출": n, "가족협의": n, "경쟁현장": n, "교통": n}
    visitor_home_regions: dict[str, int] = field(default_factory=dict)
