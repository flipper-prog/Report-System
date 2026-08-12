"""백테스트 하네스 (제안서 5.4.2 / 부록 E.2).

원칙: 과거 시점(cutoff)에서 **그 시점에 존재했던 정보만으로** 산출한 구간을
이후 실현값과 대조한다. 운영 코드와 동일한 함수를 호출하여 "리포트에 나가는
구간"과 "검증되는 구간"이 같음을 보장한다.

산출: 명목 신뢰수준 대비 실제 적중률(coverage) — E.2의 상시 관리 지표.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .models import Comparable, Site, SubscriptionRecord, Transaction
from .pricing import (MAX_DIST_M, MIN_SAMPLES_BAND, adjusted_ppsm,
                      quality_adjusted_bands)
from .subscription import CONFIDENCE as SUB_CONFIDENCE
from .subscription import predict
from .transactions import clean

PRICE_BAND_NOMINAL = 0.50   # 운영 밴드는 q25~q75 → 명목 50%


@dataclass
class BacktestFold:
    cutoff: date
    key: str                 # 타입명 또는 사례 식별자
    lo: float
    hi: float
    actual: float
    hit: bool


@dataclass
class BacktestReport:
    name: str
    nominal: float
    folds: list[BacktestFold] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.folds)

    @property
    def coverage(self) -> float | None:
        return sum(f.hit for f in self.folds) / self.n if self.n else None

    def verdict(self) -> str:
        if self.n < 10:
            return f"표본 {self.n}건 — 판정 유보 (10건 이상 누적 필요)"
        c = self.coverage or 0.0
        gap = c - self.nominal
        if gap < -0.10:
            return (f"적중률 {c:.0%} < 명목 {self.nominal:.0%} — "
                    f"구간 폭 재보정 또는 정성 전환 필요 (E.2 조치)")
        if gap > 0.20:
            return (f"적중률 {c:.0%} ≫ 명목 {self.nominal:.0%} — "
                    f"구간이 과도하게 넓어 정보량 손실 가능")
        return f"적중률 {c:.0%} (명목 {self.nominal:.0%}) — 정합"

    def as_markdown(self) -> str:
        rows = [f"**{self.name}** — {self.verdict()}", "",
                "| cutoff | 대상 | 구간 | 실현 | 결과 |",
                "|--------|------|------|------|------|"]
        for f in self.folds[:20]:
            rows.append(f"| {f.cutoff} | {f.key} | {f.lo:,.0f}~{f.hi:,.0f} | "
                        f"{f.actual:,.0f} | {'적중' if f.hit else '이탈'} |")
        if self.n > 20:
            rows.append(f"| … | (총 {self.n}건 중 20건 표시) | | | |")
        if self.skipped:
            rows += ["", "*건너뛴 fold: " + "; ".join(self.skipped[:5]) + "*"]
        return "\n".join(rows)


# ── 가격 밴드 백테스트 ───────────────────────────────────────────────────────

def _add_months(d: date, m: int) -> date:
    y, mo = divmod((d.year * 12 + d.month - 1) + m, 12)
    return date(y, mo + 1, min(d.day, 28))


def backtest_price_bands(site: Site, comps: dict[str, Comparable],
                         txs: list[Transaction], cutoffs: list[date],
                         horizon_months: int = 6) -> BacktestReport:
    rep = BacktestReport("가격 밴드 (품질조정 q25~q75)", PRICE_BAND_NOMINAL)
    kept = clean(txs).kept

    for cutoff in cutoffs:
        past = [t for t in kept if t.trade_date <= cutoff]
        future_end = _add_months(cutoff, horizon_months)
        future = [t for t in kept if cutoff < t.trade_date <= future_end]
        if len(past) < MIN_SAMPLES_BAND or not future:
            rep.skipped.append(f"{cutoff}: 과거 {len(past)}건/미래 {len(future)}건")
            continue

        bands = quality_adjusted_bands(site, comps, past, cutoff)
        for t in site.types:
            band = next((b for b in bands
                         if b.type_name == t.name and b.level == "타입"
                         and not b.rolled_up), None)
            if band is None:
                continue
            # 밴드는 '개별 비교거래의 분포'이므로 검증도 개별 거래 단위로 수행한다.
            # (미래 거래의 중위값을 쓰면 표본평균의 낮은 분산 때문에 적중률이
            #  구조적으로 과대평가된다 — 명목 수준과 비교 불가능해짐)
            for tx in future:
                comp = comps.get(tx.complex_id)
                if comp is None or comp.dist_m > MAX_DIST_M:
                    continue
                if not (t.area_m2 * 0.8 <= tx.area_m2 <= t.area_m2 * 1.2):
                    continue
                actual = adjusted_ppsm(tx, comp, cutoff, t.floors)
                rep.folds.append(BacktestFold(
                    cutoff, f"{t.name}/{comp.name}", band.q25, band.q75, actual,
                    band.q25 <= actual <= band.q75))
    return rep


# ── 청약 전망 백테스트 (leave-one-out, 시점 분리) ────────────────────────────

def backtest_subscription(history: list[SubscriptionRecord],
                          min_train: int = 8) -> BacktestReport:
    rep = BacktestReport("청약 경쟁률 구간", SUB_CONFIDENCE)
    ordered = sorted(history, key=lambda r: r.open_date)

    for i, target in enumerate(ordered):
        train = [r for r in ordered[:i] if r.open_date < target.open_date]
        if len(train) < min_train:
            continue
        fc = predict(train, target.region,
                     price_gap_pct=target.price_gap_pct if target.price_gap_pct is not None else 0.0,
                     concurrent_supply=target.concurrent_supply if target.concurrent_supply is not None else 0)
        if not fc.ok:
            rep.skipped.append(f"{target.open_date} {target.complex_id}: {fc.reason[:30]}")
            continue
        actual = target.competition_rate
        rep.folds.append(BacktestFold(
            target.open_date, target.complex_id, fc.lo, fc.hi, actual,
            fc.lo <= actual <= fc.hi))
    return rep


def quarterly_cutoffs(start: date, end: date) -> list[date]:
    out: list[date] = []
    d = date(start.year, ((start.month - 1) // 3) * 3 + 1, 1)
    while d < end:
        if d > start:
            out.append(d)
        d = _add_months(d, 3)
    return out
