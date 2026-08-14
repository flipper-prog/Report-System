"""품질조정 가격 밴드 (제안서 5.5).

- 비교 거래의 연식·층·분양권 여부를 조정하여 ㎡당 가격을 표준화한다.
- 신축 분양 현장의 우선 비교군은 분양권·입주권 실거래다. (P1-1)
- 타입·층 구간 단위 밴드를 산출하되, 표본 미달 시 상위 구간으로 롤업하고
  그 사실을 결과에 표기한다. (P1-5)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from statistics import quantiles
from typing import Optional

from .acquisition import total_ppsm
from .models import (Comparable, Site, Transaction, TypeSpec,
                     total_acquisition_cost)

MIN_SAMPLES_BAND = 8          # 구간별 최소 표본 (P1-5)
AGE_ADJ_PER_YEAR = 0.010      # 연식 1년당 조정률(구축일수록 상향 조정)
AGE_ADJ_CAP = 0.20
FLOOR_LOW_ADJ = +0.03         # 저층 거래는 상향 조정(표준층 환산)
FLOOR_HIGH_ADJ = -0.02        # 고층 거래는 하향 조정
PRESALE_WEIGHT = 2            # 분양권 거래 가중(비교군 우선, P1-1)
MAX_DIST_M = 2000.0
TIME_ADJ_PER_YEAR = 0.0       # 시점수정 연간 변화율 (기본: 미적용)
TIME_ADJ_CAP = 0.30           # 시점수정 상한(로그, 절댓값)


@dataclass(frozen=True)
class Coefficients:
    """품질조정 계수 묶음.

    기본값은 문헌·경험 기반 초기값이며, `calibrate.py` 가 실데이터 회귀로
    추정한 값이 백테스트에서 더 나을 때만 교체된다. `source` 는 리포트에
    '예시값'인지 '교정값'인지를 밝히기 위해 함께 다닌다.
    """
    #: 계수는 모두 **로그 스케일**이다. 조정은 exp(계수)를 곱하는 형태이며,
    #: 교정 엔진의 로그선형 회귀와 동일한 함수 형태를 갖도록 맞춘 것이다.
    #: (선형 1+x 형태를 쓰면 추정식과 적용식이 달라져 계수가 클수록 어긋난다)
    age_per_year: float = AGE_ADJ_PER_YEAR
    age_cap: float = AGE_ADJ_CAP        # 연식 조정 상한(로그) — 외삽 방지
    floor_low: float = FLOOR_LOW_ADJ
    floor_high: float = FLOOR_HIGH_ADJ
    #: 시점수정 — 과거 거래를 기준일 시세로 끌어올리거나 내리는 연간 변화율.
    #: 기본 0.0(시점수정 없음). 조정이 정밀해질수록 밴드가 좁아져 시점 차이가
    #: 그대로 드러나므로, 교정 시 회귀의 시간 계수로 이 값을 채운다.
    time_per_year: float = TIME_ADJ_PER_YEAR
    time_cap: float = TIME_ADJ_CAP      # 시점수정 상한(로그, 절댓값)
    source: str = "초기값(미교정)"

    def as_dict(self) -> dict:
        return {"age_per_year": self.age_per_year, "age_cap": self.age_cap,
                "floor_low": self.floor_low, "floor_high": self.floor_high,
                "time_per_year": self.time_per_year, "time_cap": self.time_cap,
                "source": self.source}

    @staticmethod
    def from_dict(d: dict) -> "Coefficients":
        base = DEFAULT_COEF
        return Coefficients(
            age_per_year=float(d.get("age_per_year", base.age_per_year)),
            age_cap=float(d.get("age_cap", base.age_cap)),
            floor_low=float(d.get("floor_low", base.floor_low)),
            floor_high=float(d.get("floor_high", base.floor_high)),
            time_per_year=float(d.get("time_per_year", base.time_per_year)),
            time_cap=float(d.get("time_cap", base.time_cap)),
            source=str(d.get("source", "외부 지정")))


DEFAULT_COEF = Coefficients()


@dataclass
class Band:
    level: str                # "타입·층구간" | "타입" | "현장" (롤업 수준)
    type_name: str
    floor_band: Optional[str]  # "저층"/"기준층"/"상층" 또는 None
    q25: float
    q50: float
    q75: float
    n: int
    rolled_up: bool
    note: str = ""


@dataclass
class MarketPosition:
    type_name: str
    subject_ppsm: float       # 총취득원가 기준 ㎡당
    band: Band
    label: str                # "밴드 하단(가격 경쟁력)" 등


def _floor_band(t: TypeSpec, floor: int) -> str:
    lo, hi = t.floors
    span = max(hi - lo + 1, 1)
    if floor <= lo + max(2, span // 5) - 1:
        return "저층"
    if floor >= hi - max(1, span // 5) + 1:
        return "상층"
    return "기준층"


def _adjust_ppsm(tx: Transaction, comp: Comparable, asof: date,
                 subject_floors: tuple[int, int],
                 coef: Coefficients = DEFAULT_COEF,
                 tax_base: "float | None" = None) -> float:
    # 비교 거래도 현장과 같은 '총취득원가' 기준으로 환산한다. 비교단지를 사는
    # 사람도 취득세를 내므로, 한쪽에만 세금을 얹으면 현장이 실제보다 비싸
    # 보이고 판정 ①이 '밴드 상단' 쪽으로 계통적으로 기운다.
    ppsm = total_ppsm(tx.price, tx.area_m2, tax_base)
    # 시점수정: 과거 거래를 기준일(asof) 시세 수준으로 환산한다.
    # 감정평가의 시점수정과 같은 역할이며, 계수가 0이면 아무 일도 하지 않는다.
    if coef.time_per_year:
        years = max(0.0, (asof - tx.trade_date).days / 365.25)
        shift = coef.time_per_year * years
        ppsm *= math.exp(max(-coef.time_cap, min(coef.time_cap, shift)))
    # 연식 조정: 그 거래를 신축(현장) 수준으로 환산한다.
    # 연식은 **거래 시점** 기준으로 잰다. 가격을 결정한 것은 거래 당시의 연식이며,
    # 기준일 기준으로 재면 '경과 연수'가 연식 조정에 섞여 들어가 같은 단지 안에서도
    # 오래된 거래일수록 조정값이 계통적으로 어긋난다. 시간 경과분은 위의 시점수정이
    # 따로 담당한다 — 두 효과를 분리해야 회귀 추정치와 적용식이 일치한다.
    age = max(tx.trade_date.year - comp.built_year, 0)
    ppsm *= math.exp(min(age * coef.age_per_year, coef.age_cap))
    # 층 조정 → 기준층 환산
    lo, hi = subject_floors
    span = max(hi - lo + 1, 1)
    if tx.floor <= lo + max(2, span // 5) - 1:
        ppsm *= math.exp(coef.floor_low)
    elif tx.floor >= hi - max(1, span // 5) + 1:
        ppsm *= math.exp(coef.floor_high)
    return ppsm


def adjusted_ppsm(tx: Transaction, comp: Comparable, asof: date,
                  subject_floors: tuple[int, int],
                  coef: Coefficients = DEFAULT_COEF,
                  tax_base: "float | None" = None) -> float:
    """공개 래퍼 — 백테스트가 운영과 동일한 조정식을 사용하도록 노출."""
    return _adjust_ppsm(tx, comp, asof, subject_floors, coef, tax_base)


def _quantile3(vals: list[float]) -> tuple[float, float, float]:
    if len(vals) == 1:
        return vals[0], vals[0], vals[0]
    q = quantiles(vals, n=4, method="inclusive")
    return q[0], q[1], q[2]


def quality_adjusted_bands(
    site: Site,
    comps: dict[str, Comparable],
    txs: list[Transaction],
    asof: date,
    profile=None,
    coef: Coefficients = DEFAULT_COEF,
    tax_base: "float | None" = None,
) -> list[Band]:
    """타입별(가능하면 층구간별) 품질조정 가격 밴드.

    profile(상품 프로파일)이 주어지면 면적 허용치·거리·최소 표본을 상품에 맞게
    적용한다. 미지정 시 아파트 기준 상수를 사용한다.
    """
    tol = profile.area_tolerance if profile else 0.20
    max_dist = profile.max_dist_m if profile else MAX_DIST_M
    min_samples = profile.min_samples_band if profile else MIN_SAMPLES_BAND
    bands: list[Band] = []

    for t in site.types:
        # 면적 유사 + 거리 조건 비교 거래 수집. 분양권은 가중 반복. (P1-1)
        samples: list[tuple[str, float]] = []  # (floor_band, adjusted_ppsm)
        for tx in txs:
            comp = comps.get(tx.complex_id)
            if comp is None or comp.dist_m > max_dist:
                continue
            if not (t.area_m2 * (1 - tol) <= tx.area_m2 <= t.area_m2 * (1 + tol)):
                continue
            adj = _adjust_ppsm(tx, comp, asof, t.floors, coef, tax_base)
            weight = PRESALE_WEIGHT if comp.is_presale_right else 1
            fb = _floor_band(t, tx.floor)
            samples.extend([(fb, adj)] * weight)

        by_fb: dict[str, list[float]] = {}
        for fb, v in samples:
            by_fb.setdefault(fb, []).append(v)

        made_fb_level = False
        for fb in ("저층", "기준층", "상층"):
            vals = by_fb.get(fb, [])
            if len(vals) >= min_samples:
                q25, q50, q75 = _quantile3(vals)
                bands.append(Band("타입·층구간", t.name, fb, q25, q50, q75, len(vals), rolled_up=False))
                made_fb_level = True

        all_vals = [v for _, v in samples]
        if len(all_vals) >= min_samples:
            q25, q50, q75 = _quantile3(all_vals)
            bands.append(Band(
                "타입", t.name, None, q25, q50, q75, len(all_vals),
                rolled_up=not made_fb_level,
                note="층구간 표본 미달로 타입 수준 롤업" if not made_fb_level else ""))
        elif all_vals:
            # 타입 수준도 미달 → 현장 수준 롤업은 아래에서 일괄 처리
            bands.append(Band(
                "타입", t.name, None, *_quantile3(all_vals), len(all_vals),
                rolled_up=True,
                note=f"표본 {len(all_vals)}건(<{min_samples}) — 참고치. 수치 제시 대신 정성 판단 권고"))

    return bands


def market_positions(site: Site, bands: list[Band],
                     tax_base: "float | None" = None) -> list[MarketPosition]:
    """총취득원가 기준 ㎡당 가격의 밴드 내 위치 판정."""
    out: list[MarketPosition] = []
    for t in site.types:
        band = next(
            (b for b in bands if b.type_name == t.name and b.level == "타입"), None)
        if band is None:
            continue
        subject_ppsm = total_acquisition_cost(t, tax_base) / t.area_m2
        if subject_ppsm <= band.q25:
            label = "밴드 하단(가격 경쟁력)"
        elif subject_ppsm <= band.q75:
            label = "밴드 내"
        else:
            label = "밴드 상단(가격 저항 위험)"
        out.append(MarketPosition(t.name, subject_ppsm, band, label))
    return out
