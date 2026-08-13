"""신고지연 보정 — 최근 월은 아직 다 들어오지 않았다.

부동산 거래신고는 계약일로부터 30일 이내에 하면 되고, 신고분이 공개 자료에
반영되기까지 며칠이 더 걸린다. 그래서 **수집 시점의 최근 1~2개월 거래는 항상
과소집계**되어 있다. 이 사실을 무시하면 두 가지가 조용히 망가진다.

  1) 추세 — 월별 집계의 마지막 점은 표본이 얇아 흔들리는데, OLS에서 끝점은
     레버리지가 가장 크다. 미완결 월 하나가 추세 방향을 바꿀 수 있다.
  2) 거래량 지표(환금성 회전율) — 분자만 덜 차고 분모(기간)는 그대로이므로
     회전율이 체계적으로 낮게 나온다. 임계값 근처에서 '환금성 취약' 오판정.

그리고 백테스트에는 더 미묘한 문제가 있다. 과거 cutoff 시점에서 "그때 있었던
정보"라며 쓰는 데이터에는, 실제로는 그때 아직 신고되지 않았을 거래가 섞여
있다. 지금 돌아보면 다 들어와 있기 때문이다. 이는 백테스트를 낙관 쪽으로
편향시키므로, 백테스트도 같은 지연을 적용해 잘라 낸다.

원칙은 **제외하되 밝힌다** — 잘라 낸 월과 그 사유를 리포트에 남긴다.
"""
from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median

from .models import Transaction

#: 부동산거래신고법상 신고기한(계약일로부터).
REPORT_LAG_DAYS = 30
#: 신고분이 공개 자료에 반영되기까지의 처리 지연(보수적 가정).
PROCESSING_DAYS = 5
#: 완결로 간주하기까지의 총 대기일.
SETTLE_DAYS = REPORT_LAG_DAYS + PROCESSING_DAYS

#: 완결 월인데도 직전 완결 월들의 중위 건수 대비 이 비율에 못 미치면
#: '거래량 급감' 후보다. 시장 냉각일 수도 있으므로 제외하지 않고 알리기만 한다.
LOW_VOLUME_RATIO = 0.6
#: 급감 판정에 쓰는 비교 기준 월 수.
BASELINE_MONTHS = 6
#: 비율만으로 판정하면 월 5~10건 규모에서는 **무작위 변동이 그대로 경고가 된다**.
#: 건수를 포아송으로 보고 기준 중위(λ)에서 이 배수의 표준편차(√λ)보다 아래일
#: 때만 표시한다. 비율 조건과 함께 둘 다 만족해야 한다.
LOW_VOLUME_SIGMA = 2.0


def month_key(d: date) -> int:
    return d.year * 12 + d.month


def month_label(mk: int) -> str:
    y, m = divmod(mk - 1, 12)
    return f"{y}-{m + 1:02d}"


def month_end(mk: int) -> date:
    y, m = divmod(mk - 1, 12)
    y, m = y, m + 1
    return date(y, m, calendar.monthrange(y, m)[1])


def settle_date(asof: date, settle_days: int = SETTLE_DAYS) -> date:
    """이 날짜 이전의 계약은 신고가 마감되었다고 본다."""
    return asof - timedelta(days=settle_days)


@dataclass
class MonthStatus:
    key: int
    n: int
    complete: bool
    note: str = ""

    @property
    def label(self) -> str:
        return month_label(self.key)


@dataclass
class LagAssessment:
    """월별 완결 여부 판정 결과."""
    asof: date
    settle_days: int
    settle_on: date
    months: list[MonthStatus] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @property
    def provisional(self) -> list[int]:
        return [m.key for m in self.months if not m.complete]

    @property
    def complete_months(self) -> list[int]:
        return [m.key for m in self.months if m.complete]

    @property
    def last_complete(self) -> int | None:
        c = self.complete_months
        return max(c) if c else None

    @property
    def dropped_trades(self) -> int:
        return sum(m.n for m in self.months if not m.complete)

    @property
    def low_volume(self) -> list[MonthStatus]:
        return [m for m in self.months if m.complete and m.note]

    def summary(self) -> str:
        if not self.months:
            return "거래 없음 — 신고지연 판정 불가"
        if not self.provisional:
            return (f"신고 마감 기준일 {self.settle_on} — 미완결 월 없음 "
                    f"(완결 {len(self.complete_months)}개월)")
        labels = ", ".join(month_label(k) for k in sorted(self.provisional))
        return (f"신고 마감 기준일 {self.settle_on} — 미완결 {labels} "
                f"({self.dropped_trades:,}건)은 추세·회전율 산출에서 제외")

    def as_markdown(self) -> str:
        L = [f"**{self.summary()}**", "",
             f"- 신고기한 {REPORT_LAG_DAYS}일 + 공개 반영 {PROCESSING_DAYS}일 = "
             f"{self.settle_days}일 경과분까지 완결로 간주",
             ""]
        if self.months:
            L += ["| 월 | 거래 | 상태 |", "|----|------|------|"]
            for m in self.months[-14:]:
                state = "완결" if m.complete else "미완결(제외)"
                if m.note:
                    state += f" · {m.note}"
                L.append(f"| {m.label} | {m.n:,}건 | {state} |")
            if len(self.months) > 14:
                L.append(f"| … | (총 {len(self.months)}개월 중 최근 14개월) | |")
            L.append("")
        if self.low_volume:
            L.append("- 완결 월 중 거래량이 직전 기준 대비 크게 낮은 달이 있습니다. "
                     "시장 냉각일 수도, 수집 누락일 수도 있으므로 원자료 확인이 "
                     "필요합니다. [LIMITATION]")
            L.append("")
        for lim in self.limitations:
            L.append(f"- {lim}")
        if self.limitations:
            L.append("")
        return "\n".join(L)


def assess(txs: list[Transaction], asof: date,
           settle_days: int = SETTLE_DAYS) -> LagAssessment:
    """월별로 신고 마감 여부를 판정한다.

    판정은 법정 신고기한이라는 **결정적 규칙**으로 한다. 거래량이 적다는
    관찰만으로 월을 빼면, 실제 시장 냉각을 데이터 결함으로 오인해 하락 신호를
    지워 버리게 된다. 거래량 급감은 별도 표시로만 남긴다.
    """
    on = settle_date(asof, settle_days)
    res = LagAssessment(asof=asof, settle_days=settle_days, settle_on=on)

    buckets: dict[int, int] = {}
    for t in txs:
        if t.canceled:
            continue
        buckets[month_key(t.trade_date)] = buckets.get(month_key(t.trade_date), 0) + 1
    if not buckets:
        res.limitations.append("거래 표본 없음 — 신고지연 보정 미적용")
        return res

    for mk in sorted(buckets):
        # 그 달의 마지막 계약일까지 신고가 마감되었어야 '완결'이다.
        res.months.append(MonthStatus(mk, buckets[mk], month_end(mk) <= on))

    # 완결 월 중 거래량 급감 표시 (제외는 하지 않는다)
    complete = [m for m in res.months if m.complete]
    for i, m in enumerate(complete):
        base = [c.n for c in complete[max(0, i - BASELINE_MONTHS):i]]
        if len(base) < 3:
            continue
        lam = median(base)
        floor = lam - LOW_VOLUME_SIGMA * math.sqrt(lam)
        if m.n < lam * LOW_VOLUME_RATIO and m.n < floor:
            m.note = f"거래량 급감(직전 중위 {lam:.0f}건 대비)"

    if res.provisional:
        res.limitations.append(
            f"미완결 {len(res.provisional)}개월 {res.dropped_trades:,}건은 "
            "추세·회전율에서 제외됩니다. 가격 밴드에는 실제 관측이므로 "
            "포함되며, 신고지연이 가격과 상관될 경우 밴드에 잔여 편의가 "
            "있을 수 있습니다 [LIMITATION]")
    return res


def complete_only(txs: list[Transaction], asof: date,
                  settle_days: int = SETTLE_DAYS) -> list[Transaction]:
    """신고가 마감된 계약만 남긴다 (백테스트의 실시간 재현용).

    과거 cutoff 시점을 재현할 때 '지금은 있지만 그때는 없었던' 거래를 쓰면
    백테스트가 실제보다 좋게 나온다. 같은 지연을 적용해 그 낙관을 제거한다.

    보수적인 절단이다 — 실제로는 기한 전에 신고된 거래도 보였을 것이므로
    가용 정보를 다소 과소평가한다. 검증 도구에서 편의의 방향은 낙관이 아니라
    비관이어야 하므로 의도된 선택이다.
    """
    on = settle_date(asof, settle_days)
    return [t for t in txs if t.trade_date <= on]
