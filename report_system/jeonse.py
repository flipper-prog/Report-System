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

#: 계층(단지 × 면적대) 매칭 기준.
#:
#: 전세와 매매를 각각 풀링한 뒤 중위끼리 나누면 **두 표본의 구성 차이가 그대로
#: 비율에 들어간다**. 전세는 소형·저가 단지에서, 매매는 대형·고가 단지에서 더
#: 많이 나오는 것이 흔한 패턴이고, 전세가율은 소형일수록 높으므로 풀링 비율이
#: 실제보다 높게 나온다 — 즉 하방 완충을 실제보다 두텁게 보고한다.
#: 그래서 같은 단지·같은 면적대끼리 짝지어 비율을 내고, 그 비율들을 합친다.
AREA_BUCKET_M2 = 10.0
#: 한 계층이 성립하려면 전세·매매 각각 이만큼은 있어야 한다.
MIN_STRATUM = 3
#: 짝지어진 표본이 전체의 이 비율에 못 미치면 계층화 결과를 대표값으로 쓰지 않는다.
MIN_PAIRED_SHARE = 0.30


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
    #: 단지×면적대 짝짓기로 산출했는지. False면 풀링(구성 차이 위험 있음).
    paired: bool = False
    #: 계층 합산 가중치가 현장 타입 구성이었는지 (아니면 표본 수)
    weighted_by_subject: bool = False
    strata: list["Stratum"] = field(default_factory=list)
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


def _stratum(complex_id: str, area_m2: float) -> tuple[str, int]:
    return (complex_id, int(area_m2 // AREA_BUCKET_M2))


def _weighted_median(pairs: list[tuple[float, float]]) -> float:
    """(값, 가중치)의 가중 중위값.

    오름차순으로 누적하다 절반을 **넘거나 같아지는** 지점의 값을 취하므로,
    가중치가 정확히 반반으로 갈리면 낮은 쪽이 선택된다. 전세가율은 하방 완충
    두께를 재는 지표이므로, 동점에서 보수적인(얇은) 쪽을 택하는 것이 맞다.
    """
    ordered = sorted(pairs)
    total = sum(w for _, w in ordered)
    acc = 0.0
    for v, w in ordered:
        acc += w
        if acc * 2 >= total:
            return v
    return ordered[-1][0]


@dataclass
class Stratum:
    """단지 × 면적대 하나에서 짝지어 낸 전세가율."""
    complex_id: str
    area_lo: float
    n_jeonse: int
    n_sale: int
    ratio_pct: float

    @property
    def label(self) -> str:
        return (f"{self.complex_id} {self.area_lo:.0f}~"
                f"{self.area_lo + AREA_BUCKET_M2:.0f}㎡")


def _paired_ratio(jeonse: list[RentRecord], sales: list[Transaction],
                  area_weights: "dict[int, int] | None" = None):
    """같은 단지·면적대끼리 짝지어 전세가율을 산출한다.

    area_weights: 면적대(버킷) → 현장 세대수. 지표가 답해야 할 질문은
    "**이 현장의** 분양가가 밀릴 때 전세가 어디까지 받쳐 주는가"이므로,
    계층을 합칠 때는 비교 표본의 구성이 아니라 **현장의 타입 구성**으로
    가중하는 것이 맞다. 미지정이거나 현장 면적대와 겹치는 계층이 없으면
    짝지어진 표본 수로 가중한다.

    반환: (ratio_pct, jeonse_ppsm, sale_ppsm, n_j, n_s, strata, by_subject)
    또는 None. None 이면 짝지을 계층이 없다는 뜻이고, 호출부가 풀링으로 되돌린다.
    """
    j_by: dict[tuple, list[RentRecord]] = {}
    s_by: dict[tuple, list[Transaction]] = {}
    for r in jeonse:
        if r.deposit_ppsm > 0:
            j_by.setdefault(_stratum(r.complex_id, r.area_m2), []).append(r)
    for t in sales:
        if t.area_m2 > 0:
            s_by.setdefault(_stratum(t.complex_id, t.area_m2), []).append(t)

    strata: list[Stratum] = []
    matched_j: list[RentRecord] = []
    matched_s: list[Transaction] = []
    for key in sorted(j_by.keys() & s_by.keys()):
        js, ss = j_by[key], s_by[key]
        if len(js) < MIN_STRATUM or len(ss) < MIN_STRATUM:
            continue
        jp = _median_ppsm(js)
        sp = median([t.price / t.area_m2 for t in ss])
        if not jp or not sp:
            continue
        strata.append(Stratum(key[0], key[1] * AREA_BUCKET_M2,
                              len(js), len(ss), jp / sp * 100))
        matched_j += js
        matched_s += ss
    if not strata:
        return None

    by_subject = False
    weights = [(s.ratio_pct, float(min(s.n_jeonse, s.n_sale))) for s in strata]
    if area_weights:
        subj = [(s.ratio_pct,
                 float(area_weights.get(int(s.area_lo // AREA_BUCKET_M2), 0)))
                for s in strata]
        if sum(w for _, w in subj) > 0:
            weights = [(v, w) for v, w in subj if w > 0]
            by_subject = True

    ratio = _weighted_median(weights)
    return (ratio, _median_ppsm(matched_j),
            median([t.price / t.area_m2 for t in matched_s]),
            len(matched_j), len(matched_s), strata, by_subject)


def _mix_gap(jeonse: list[RentRecord], sales: list[Transaction]) -> str:
    """풀링으로 되돌릴 때, 두 표본의 구성이 얼마나 다른지를 밝힌다."""
    if not jeonse or not sales:
        return ""
    ja = median([r.area_m2 for r in jeonse])
    sa = median([t.area_m2 for t in sales])
    parts = [f"전세 표본 중위 {ja:.0f}㎡ vs 매매 표본 중위 {sa:.0f}㎡"]
    jc, sc = {r.complex_id for r in jeonse}, {t.complex_id for t in sales}
    if jc != sc:
        parts.append(f"단지 구성 상이(전세 {len(jc)}곳 · 매매 {len(sc)}곳, "
                     f"공통 {len(jc & sc)}곳)")
    return " · ".join(parts)


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
            asof: date, window_months: int = 12,
            subject_types: "list[tuple[float, int]] | None" = None) -> JeonseResult:
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

    # 현장 타입 구성 → 면적대별 세대수. 계층을 합칠 때의 가중치가 된다.
    area_weights: dict[int, int] = {}
    for area, units in (subject_types or []):
        if area > 0 and units > 0:
            b = int(area // AREA_BUCKET_M2)
            area_weights[b] = area_weights.get(b, 0) + units

    def _ratio_of(js: list[RentRecord], ss: list[Transaction]):
        """계층 매칭을 우선하고, 성립하지 않으면 풀링으로 되돌린다."""
        paired = _paired_ratio(js, ss, area_weights)
        if paired is not None:
            ratio, jp, sp, nj, ns, strata, by_subject = paired
            if nj >= len(js) * MIN_PAIRED_SHARE:
                return ratio, jp, sp, nj, ns, strata, True, by_subject
        jp = _median_ppsm(js)
        sp = median([t.price / t.area_m2 for t in ss]) if ss else None
        ratio = (jp / sp * 100) if (jp and sp) else None
        return ratio, jp, sp, len(js), len(ss), [], False, False

    if (len(recent_j) >= min_half and len(prev_j) >= min_half
            and len(recent_s) >= min_half and len(prev_s) >= min_half):
        cur = _ratio_of(recent_j, recent_s)
        res.ratio_window_months = half
        prev = _ratio_of(prev_j, prev_s)
        res.ratio_prev_pct = prev[0]
    else:
        cur = _ratio_of(jeonse, sales)
        res.ratio_window_months = window_months
        res.limitations.append("전·후반 표본 부족 — 전세가율 추세는 산출하지 않음")

    (res.ratio_pct, res.jeonse_ppsm, res.sale_ppsm,
     res.n_ratio_jeonse, res.n_ratio_sale, res.strata,
     res.paired, res.weighted_by_subject) = cur

    if res.paired:
        how = ("현장 타입 구성으로 가중" if res.weighted_by_subject
               else "짝지어진 표본 수로 가중 — 현장 면적대와 겹치는 계층 없음")
        res.limitations.append(
            f"전세가율은 단지×면적대 {len(res.strata)}개 계층에서 짝지어 산출 "
            f"(구성 차이 제거, {how})")
    else:
        gap = _mix_gap(jeonse, sales)
        res.limitations.append(
            "짝지을 계층(단지×면적대, 각 3건 이상) 부족 — 전세·매매 표본을 각각 "
            "풀링해 산출. 두 표본의 구성이 다르면 비율에 그 차이가 섞인다"
            + (f" ({gap})" if gap else "") + " [LIMITATION]")

    if full_jeonse_ppsm and monthly:
        res.conversion_rate_pct = _conversion_rate(monthly, full_jeonse_ppsm)
        if res.conversion_rate_pct is None:
            res.limitations.append("월세 표본의 보증금이 전세 수준 이상 — 전환율 미산출")
    elif not monthly:
        res.limitations.append("월세 표본 없음 — 전월세전환율 미산출")

    return res
