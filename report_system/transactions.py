"""거래 정제 (부록 A.9): 취소·중복·이상·특수관계 의심 거래의 제거.

정제 룰은 코드로 고정되며(P2-3), 정제 전후 요약이 리포트에 자동 표기된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from .models import Transaction

# 정제 룰 버전 — 룰 변경 시 반드시 증가시키고 리포트·백테스트 결과에 표기한다.
# (동일 버전이면 동일 입력에 동일 정제 결과가 보장된다)
RULES_VERSION = "clean-1.1"
RULES_SUMMARY = (
    "취소거래 제거 · 완전중복 제거 · 단지×면적버킷 **동시기(±6개월)** 기준 "
    "MAD z>3.5 이상치 제거 · 동시기 중위 대비 60% 미만 저가 특수거래 의심 제외")

MAD_Z_THRESHOLD = 3.5      # 로버스트 이상치 기준
SPECIAL_LOW_RATIO = 0.6    # 동시기 중위가 대비 이 비율 미만이면 특수거래 의심
AREA_BUCKET_M2 = 10.0      # 면적 버킷 폭

#: 비교 기준을 잡는 동시기 창(앞뒤 각각, 개월).
#:
#: 전 기간 중위값을 기준으로 쓰면 **필터의 효과가 시점과 상관**된다.
#:   - 상승장: 최근의 진짜 특수거래(동시기 절반 가격)가 전 기간 중위 대비로는
#:     0.6배를 넘어 살아남는다. 가장 최근 데이터에 오염이 남는다.
#:   - 하락장: 정상적인 최근 저가 거래가 통째로 '특수 의심'으로 지워진다.
#:     하락 신호를 담은 근거를 분석이 스스로 삭제하는 셈이다.
#: 그래서 각 거래를 **같은 시기의 같은 단지·면적대** 거래와만 비교한다.
LOCAL_WINDOW_MONTHS = 6
#: 동시기 표본이 이보다 적으면 국소 기준이 오히려 불안정하므로 그룹 전체
#: 기준으로 되돌린다. 되돌린 건수는 정제 요약에 남긴다.
MIN_LOCAL_SAMPLES = 5


@dataclass
class CleanResult:
    kept: list[Transaction]
    removed: dict[str, list[Transaction]] = field(default_factory=dict)
    rules_version: str = RULES_VERSION
    #: 동시기(±6개월) 기준으로 판정된 거래 수
    local_judged: int = 0
    #: 동시기 표본 부족으로 그룹 전체 기준을 쓴 거래 수
    fallback_judged: int = 0

    @property
    def summary(self) -> dict[str, int]:
        out = {reason: len(v) for reason, v in self.removed.items()}
        out["사용"] = len(self.kept)
        return out

    @property
    def basis_note(self) -> str:
        total = self.local_judged + self.fallback_judged
        if not total:
            return ""
        if not self.fallback_judged:
            return "판정 기준: 전 건 동시기(±6개월) 비교"
        return (f"판정 기준: 동시기 비교 {self.local_judged:,}건 · "
                f"동시기 표본 부족으로 전 기간 기준 적용 {self.fallback_judged:,}건 "
                "[LIMITATION]")


def _bucket(tx: Transaction) -> tuple[str, int]:
    return (tx.complex_id, int(tx.area_m2 // AREA_BUCKET_M2))


def _month_key(tx: Transaction) -> int:
    return tx.trade_date.year * 12 + tx.trade_date.month


def _center(vals: list[float]) -> tuple[float, float]:
    """중위값과 MAD. MAD가 0이면 나눗셈 방어를 위해 1.0으로 대체한다."""
    med = median(vals)
    return med, (median([abs(v - med) for v in vals]) or 1.0)


def clean(txs: list[Transaction]) -> CleanResult:
    removed: dict[str, list[Transaction]] = {"취소": [], "중복": [], "이상(고저가)": [], "특수 의심": []}

    # 1) 취소 거래
    alive = []
    for t in txs:
        (removed["취소"] if t.canceled else alive).append(t)  # type: ignore[attr-defined]

    # 2) 중복 (단지·일자·면적·층·가격 동일)
    seen: set[tuple] = set()
    dedup: list[Transaction] = []
    for t in alive:
        key = (t.complex_id, t.trade_date, round(t.area_m2, 1), t.floor, t.price)
        if key in seen:
            removed["중복"].append(t)
        else:
            seen.add(key)
            dedup.append(t)

    # 3) 그룹별 로버스트 이상치(MAD z) + 특수 의심(저가)
    groups: dict[tuple, list[Transaction]] = {}
    for t in dedup:
        groups.setdefault(_bucket(t), []).append(t)

    kept: list[Transaction] = []
    local_judged = 0
    fallback_judged = 0
    for _, g in groups.items():
        ppsm = {id(t): t.price / t.area_m2 for t in g}
        whole = _center(list(ppsm.values()))

        by_month: dict[int, list[float]] = {}
        for t in g:
            by_month.setdefault(_month_key(t), []).append(ppsm[id(t)])

        # 월별로 한 번씩만 동시기 기준을 계산한다 (거래마다 재계산하지 않는다)
        ref: dict[int, tuple[float, float, bool]] = {}
        for m in by_month:
            window = [v for k in range(m - LOCAL_WINDOW_MONTHS,
                                       m + LOCAL_WINDOW_MONTHS + 1)
                      for v in by_month.get(k, ())]
            if len(window) >= MIN_LOCAL_SAMPLES:
                med, mad = _center(window)
                ref[m] = (med, mad, True)
            else:
                ref[m] = (*whole, False)

        for t in g:
            p = ppsm[id(t)]
            med, mad, is_local = ref[_month_key(t)]
            if is_local:
                local_judged += 1
            else:
                fallback_judged += 1
            z = 0.6745 * (p - med) / mad
            if p < med * SPECIAL_LOW_RATIO:
                removed["특수 의심"].append(t)
            elif abs(z) > MAD_Z_THRESHOLD:
                removed["이상(고저가)"].append(t)
            else:
                kept.append(t)

    # 전순서로 정렬한다. 거래일만으로 정렬하면 같은 날 거래의 순서가 입력
    # 순서에 남아, 수집 순서가 바뀔 때 산출물이 흔들릴 여지가 생긴다.
    kept.sort(key=lambda t: (t.trade_date, t.complex_id, t.area_m2, t.floor,
                             t.price))
    return CleanResult(kept=kept, removed=removed,
                       local_judged=local_judged,
                       fallback_judged=fallback_judged)
