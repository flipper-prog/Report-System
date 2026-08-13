"""전세가율·전월세전환율 분석 (L11 완성분).

매매 실거래만 보면 가격의 **하방**을 알 수 없다. 매매가에는 미래 기대가 섞여
있지만, 전세는 실거주 수요가 지금 지불하는 금액이므로 기대가 섞이지 않는다.
따라서 전세가율(전세 ㎡단가 / 매매 ㎡단가)은 가격이 어디까지 밀릴 수 있는지를
가늠하는 완충 두께로 읽는다.

산출 항목
  · 전세가율          — 하방 지지 두께
  · 전세가율 추세      — 실수요가 강해지는지 약해지는지 (선행 신호)
  · 전월세전환율      — 월세 시장의 요구 수익률. 높을수록 전세 대비 월세 부담이 큼
  · 분양가 전세 충당율 — 총취득원가 중 전세보증금으로 회수 가능한 비율

분석 규칙
  1) 갱신 계약은 제외한다. 갱신요구권·상한제의 영향으로 신규 체결가와 가격
     논리가 다르기 때문이다.
  2) 표본이 MIN_SAMPLES 미만이면 수치를 내지 않고 '판정 불가'로 둔다.
  3) 매매·전세 표본은 동일 기간·동일 비교단지에서만 대응시킨다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from statistics import median
from typing import Optional

from .models import RentRecord, Transaction

MIN_SAMPLES = 8

#: 전세가율 판정 기준(%) — 전국 아파트 분포의 통상 구간
RATIO_STRONG = 70.0
RATIO_WEAK = 55.0

#: 전세가율 추세 판정 기준(%p)
TREND_BAND = 2.0


@dataclass
class JeonseResult:
    n_jeonse: int = 0
    n_monthly: int = 0
    n_renewal_excluded: int = 0
    n_sale: int = 0
    jeonse_ppsm: Optional[float] = None
    sale_ppsm: Optional[float] = None
    ratio_pct: Optional[float] = None
    ratio_prev_pct: Optional[float] = None
    conversion_rate_pct: Optional[float] = None
    window_months: int = 12
    #: 전세가율이 실제로 계산된 기간(개월)과 그 기간의 표본 수.
    #: 추세 산출이 가능하면 후반부(최근 절반)가 '현재'가 되므로 전체 창과 다르다.
    ratio_window_months: int = 12
    n_ratio_jeonse: int = 0
    n_ratio_sale: int = 0
    limitations: list[str] = field(default_factory=list)

    # ── 파생 ────────────────────────────────────────────────────────────────
    @property
    def ratio_delta_pp(self) -> Optional[float]:
        if self.ratio_pct is None or self.ratio_prev_pct is None:
            return None
        return self.ratio_pct - self.ratio_prev_pct

    @property
    def trend_label(self) -> str:
        d = self.ratio_delta_pp
        if d is None:
            return "추세 판정 불가"
        if d >= TREND_BAND:
            return "전세가율 상승 (실수요 강화)"
        if d <= -TREND_BAND:
            return "전세가율 하락 (실수요 약화)"
        return "전세가율 보합"

    @property
    def label(self) -> str:
        if self.ratio_pct is None:
            return "판정 불가"
        if self.ratio_pct >= RATIO_STRONG:
            return "하방 지지 두터움"
        if self.ratio_pct < RATIO_WEAK:
            return "하방 완충 얇음"
        return "하방 지지 보통"

    def coverage_of(self, subject_ppsm: float) -> Optional[float]:
        """총취득원가 ㎡단가 대비 전세보증금으로 충당되는 비율(%)."""
        if self.jeonse_ppsm is None or subject_ppsm <= 0:
            return None
        return self.jeonse_ppsm / subject_ppsm * 100

    def summary(self) -> str:
        if self.ratio_pct is None:
            return (f"전세 표본 {self.n_jeonse}건 — {MIN_SAMPLES}건 미만으로 "
                    "전세가율 미산출")
        parts = [f"전세가율 {self.ratio_pct:.0f}% "
                 f"(최근 {self.ratio_window_months}개월, {self.label})",
                 self.trend_label]
        if self.conversion_rate_pct is not None:
            parts.append(f"전월세전환율 {self.conversion_rate_pct:.1f}%")
        parts.append(f"표본 전세 {self.n_jeonse}건 · 월세 {self.n_monthly}건 · "
                     f"매매 {self.n_sale}건")
        return " · ".join(parts)

    def as_rationale(self) -> str:
        if self.ratio_pct is None:
            return f"전세 기반 하방 점검: {self.summary()} [LIMITATION]"
        return f"전세 기반 하방 점검: {self.summary()}"


def _within(d: date, asof: date, months: int) -> bool:
    """asof 기준 months개월 이내인지.

    월 단위로만 비교하면 기준일과 같은 달의 '기준일 이후' 거래가 통과한다
    (asof 7/25, 거래 7/31 → 월차 0). 미래 정보 누출이므로 날짜로 먼저 막는다.
    """
    if d > asof:
        return False
    delta = (asof.year - d.year) * 12 + (asof.month - d.month)
    return 0 <= delta < months


def _median_ppsm(rents: list[RentRecord]) -> Optional[float]:
    vals = [r.deposit_ppsm for r in rents if r.deposit_ppsm > 0]
    return median(vals) if vals else None


def _conversion_rate(monthlies: list[RentRecord],
                     jeonse_ppsm: float) -> Optional[float]:
    """전월세전환율(%) = 월세×12 / (전세 환산 보증금 − 실제 보증금) × 100.

    전세 환산 보증금은 동일 면적의 전세 ㎡단가로 추정한다. 분모가 0 이하인
    (보증금이 전세 수준 이상인) 건은 전환 개념이 성립하지 않아 제외한다.
    """
    rates: list[float] = []
    for r in monthlies:
        implied_jeonse = jeonse_ppsm * r.area_m2
        gap = implied_jeonse - r.deposit
        if gap <= 0 or r.monthly_rent <= 0:
            continue
        rates.append(r.monthly_rent * 12 / gap * 100)
    return median(rates) if rates else None


def analyze(rents: list[RentRecord], sale_txs: list[Transaction],
            asof: date, window_months: int = 12) -> JeonseResult:
    """정제된 매매 거래와 전월세 거래로 전세 기반 지표를 산출한다.

    sale_txs 는 정제(transactions.clean)를 통과한 거래를 넘긴다 — 전세가율의
    분모가 이상거래에 오염되면 지표 전체가 무의미해지기 때문이다.
    """
    res = JeonseResult(window_months=window_months)

    fresh = [r for r in rents if not r.renewal and _within(r.deal_date, asof, window_months)]
    res.n_renewal_excluded = sum(
        1 for r in rents if r.renewal and _within(r.deal_date, asof, window_months))
    jeonse = [r for r in fresh if r.is_jeonse]
    monthly = [r for r in fresh if not r.is_jeonse]
    res.n_jeonse, res.n_monthly = len(jeonse), len(monthly)

    sales = [t for t in sale_txs
             if not t.canceled and t.area_m2 > 0 and _within(t.trade_date, asof, window_months)]
    res.n_sale = len(sales)

    if res.n_renewal_excluded:
        res.limitations.append(
            f"갱신 계약 {res.n_renewal_excluded}건 제외 — 갱신요구권·상한제로 "
            "신규 체결과 가격 논리가 다름")

    if len(jeonse) < MIN_SAMPLES:
        res.limitations.append(
            f"전세 표본 {len(jeonse)}건 < 최소 {MIN_SAMPLES}건 — 전세가율 미산출 "
            "[LIMITATION]")
        return res
    if len(sales) < MIN_SAMPLES:
        res.limitations.append(
            f"동일 기간 매매 표본 {len(sales)}건 < 최소 {MIN_SAMPLES}건 — "
            "전세가율 미산출 [LIMITATION]")
        return res

    # 직전 동일 기간과 비교해 추세를 본다. 표본이 충분하면 후반부(최근)를
    # '현재'로 삼는다 — 이때 표에 실리는 ㎡단가도 같은 기간의 값이어야
    # 독자가 전세가율을 직접 검산할 수 있다.
    half = max(1, window_months // 2)
    recent_j = [r for r in jeonse if _within(r.deal_date, asof, half)]
    prev_j = [r for r in jeonse if not _within(r.deal_date, asof, half)]
    recent_s = [t for t in sales if _within(t.trade_date, asof, half)]
    prev_s = [t for t in sales if not _within(t.trade_date, asof, half)]
    min_half = max(2, MIN_SAMPLES // 2)

    full_jeonse_ppsm = _median_ppsm(jeonse)
    if (len(recent_j) >= min_half and len(prev_j) >= min_half
            and len(recent_s) >= min_half and len(prev_s) >= min_half):
        res.jeonse_ppsm = _median_ppsm(recent_j)
        res.sale_ppsm = median([t.price / t.area_m2 for t in recent_s])
        res.ratio_window_months = half
        res.n_ratio_jeonse, res.n_ratio_sale = len(recent_j), len(recent_s)
        pj, ps = _median_ppsm(prev_j), median([t.price / t.area_m2 for t in prev_s])
        if pj and ps:
            res.ratio_prev_pct = pj / ps * 100
    else:
        res.jeonse_ppsm = full_jeonse_ppsm
        res.sale_ppsm = median([t.price / t.area_m2 for t in sales])
        res.ratio_window_months = window_months
        res.n_ratio_jeonse, res.n_ratio_sale = len(jeonse), len(sales)
        res.limitations.append("전·후반 표본 부족 — 전세가율 추세는 산출하지 않음")

    if res.jeonse_ppsm and res.sale_ppsm:
        res.ratio_pct = res.jeonse_ppsm / res.sale_ppsm * 100

    if full_jeonse_ppsm and monthly:
        res.conversion_rate_pct = _conversion_rate(monthly, full_jeonse_ppsm)
        if res.conversion_rate_pct is None:
            res.limitations.append("월세 표본의 보증금이 전세 수준 이상 — 전환율 미산출")
    elif not monthly:
        res.limitations.append("월세 표본 없음 — 전월세전환율 미산출")

    return res
