"""시계열 분석 (제안서 5.4.2).

- 월별 ㎡당 가격 집계와 추세(OLS 기울기) 산출
- 구조적 변화와 일시 효과의 분리를 위한 최근/전체 추세 비교
- 미래 정보 누출 방지: 모든 함수는 cutoff 이하 데이터만 받는다는 전제로 동작
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import median

from .models import Transaction


def month_key(d: date) -> int:
    return d.year * 12 + d.month


@dataclass
class TrendResult:
    months: list[int]              # month_key 목록 (오름차순)
    values: list[float]            # 월별 중위 ㎡당 가격
    slope_per_month: float         # 원/㎡ per month
    slope_pct_per_year: float      # 연 %
    recent_slope_pct_per_year: float
    n_months: int

    @property
    def regime_shift(self) -> bool:
        """최근 추세가 전체 추세와 부호가 다르면 국면 전환 신호."""
        if self.n_months < 12:
            return False
        return (self.slope_pct_per_year > 0) != (self.recent_slope_pct_per_year > 0)


def _ols_slope(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def monthly_trend(txs: list[Transaction], recent_months: int = 6) -> TrendResult:
    buckets: dict[int, list[float]] = {}
    for t in txs:
        if t.canceled or t.area_m2 <= 0:
            continue
        buckets.setdefault(month_key(t.trade_date), []).append(t.price / t.area_m2)

    months = sorted(buckets)
    values = [median(buckets[m]) for m in months]
    if not months:
        return TrendResult([], [], 0.0, 0.0, 0.0, 0)

    xs = [float(m - months[0]) for m in months]
    slope = _ols_slope(xs, values)
    level = median(values)
    pct_year = (slope * 12 / level * 100) if level else 0.0

    k = min(recent_months, len(months))
    r_slope = _ols_slope(xs[-k:], values[-k:]) if k >= 2 else 0.0
    r_level = median(values[-k:]) if k else level
    r_pct_year = (r_slope * 12 / r_level * 100) if r_level else 0.0

    return TrendResult(months, values, slope, pct_year, r_pct_year, len(months))
