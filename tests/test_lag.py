"""신고지연 보정 테스트.

거래신고는 계약일로부터 30일 이내이므로, 수집 시점의 최근 월은 항상 덜 차
있다. 여기서 확인할 것 — (1) 완결 판정이 법정 기한이라는 결정적 규칙으로만
이뤄지는가, (2) 미완결 월이 추세·회전율에서 실제로 빠지는가, (3) 거래량이 적은
달을 무작위 변동만으로 '이상'이라 부르지 않는가.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.backtest import backtest_price_bands, quarterly_cutoffs
from report_system.lag import (BASELINE_MONTHS, LOW_VOLUME_RATIO, PROCESSING_DAYS,
                               REPORT_LAG_DAYS, SETTLE_DAYS, assess,
                               complete_only, month_end, month_key, month_label,
                               settle_date)
from report_system.ledger import ForecastLedger
from report_system.liquidity import analyze as analyze_liquidity
from report_system.models import Comparable, Transaction
from report_system.pipeline import run
from report_system.timeseries import monthly_trend
from report_system.transactions import clean


def _tx(d: date, price: int = 500_000_000, area: float = 84.0,
        cid: str = "C1", floor: int = 10) -> Transaction:
    return Transaction(complex_id=cid, trade_date=d, area_m2=area,
                       floor=floor, price=price)


class TestMonthUtils(unittest.TestCase):
    def test_month_key_label_roundtrip(self):
        for d in (date(2026, 1, 1), date(2026, 12, 31), date(2025, 6, 15)):
            self.assertEqual(month_label(month_key(d)), f"{d.year}-{d.month:02d}")

    def test_month_end_december(self):
        self.assertEqual(month_end(month_key(date(2026, 12, 5))), date(2026, 12, 31))

    def test_month_end_february_leap(self):
        self.assertEqual(month_end(month_key(date(2024, 2, 3))), date(2024, 2, 29))

    def test_settle_days_composition(self):
        self.assertEqual(SETTLE_DAYS, REPORT_LAG_DAYS + PROCESSING_DAYS)
        self.assertEqual(settle_date(date(2026, 7, 25)),
                         date(2026, 7, 25) - timedelta(days=SETTLE_DAYS))


class TestAssess(unittest.TestCase):
    def test_recent_month_is_provisional(self):
        asof = date(2026, 7, 25)          # 마감 기준일 2026-06-20
        txs = [_tx(date(2026, 5, 10)), _tx(date(2026, 6, 10)),
               _tx(date(2026, 7, 10))]
        r = assess(txs, asof)
        self.assertEqual([month_label(k) for k in r.provisional],
                         ["2026-06", "2026-07"])
        self.assertEqual(month_label(r.last_complete), "2026-05")

    def test_month_fully_past_deadline_is_complete(self):
        # 2026-05-31 계약도 신고까지 35일 → 2026-07-05 이후면 완결
        r = assess([_tx(date(2026, 5, 31))], date(2026, 7, 6))
        self.assertEqual(r.provisional, [])

    def test_boundary_month_not_yet_settled(self):
        r = assess([_tx(date(2026, 5, 31))], date(2026, 7, 4))
        self.assertEqual(len(r.provisional), 1)

    def test_dropped_trade_count(self):
        asof = date(2026, 7, 25)
        txs = [_tx(date(2026, 7, 1)), _tx(date(2026, 7, 2)),
               _tx(date(2026, 3, 1))]
        self.assertEqual(assess(txs, asof).dropped_trades, 2)

    def test_canceled_excluded_from_counts(self):
        t = _tx(date(2026, 3, 1))
        t.canceled = True
        r = assess([t, _tx(date(2026, 3, 2))], date(2026, 7, 25))
        self.assertEqual(r.months[0].n, 1)

    def test_empty_input_is_safe(self):
        r = assess([], date(2026, 7, 25))
        self.assertEqual(r.months, [])
        self.assertIsNone(r.last_complete)
        self.assertIn("표본 없음", r.limitations[0])
        self.assertIn("판정 불가", r.summary())

    def test_no_provisional_reported_plainly(self):
        r = assess([_tx(date(2025, 1, 5))], date(2026, 7, 25))
        self.assertIn("미완결 월 없음", r.summary())
        self.assertEqual(r.limitations, [])

    def test_limitation_names_residual_band_bias(self):
        r = assess([_tx(date(2026, 7, 1))], date(2026, 7, 25))
        self.assertTrue(any("밴드" in l for l in r.limitations))

    def test_markdown_renders_without_error(self):
        md = assess([_tx(date(2026, m, 5)) for m in range(1, 8)],
                    date(2026, 7, 25)).as_markdown()
        self.assertIn("신고 마감 기준일", md)
        self.assertIn("미완결(제외)", md)


class TestLowVolumeFlag(unittest.TestCase):
    """월 5~10건 규모에서 무작위 변동을 '이상'이라 부르지 않아야 한다."""

    def _months(self, counts, start=(2024, 1)):
        y, m = start
        out = []
        for i, c in enumerate(counts):
            mk = y * 12 + m + i
            yy, mm = divmod(mk - 1, 12)
            for _ in range(c):
                out.append(_tx(date(yy, mm + 1, 5)))
        return out

    def test_small_sample_noise_not_flagged(self):
        # 8,9,8,7,9 → 4건: 비율로는 급감이지만 포아송 변동 범위 안
        r = assess(self._months([8, 9, 8, 7, 9, 4]), date(2026, 7, 25))
        self.assertEqual(r.low_volume, [])

    def test_genuine_collapse_is_flagged(self):
        r = assess(self._months([60, 62, 58, 61, 59, 5]), date(2026, 7, 25))
        self.assertEqual([m.label for m in r.low_volume], ["2024-06"])

    def test_flag_needs_minimum_baseline_months(self):
        # 기준 월이 3개 미만이면 판정하지 않는다
        r = assess(self._months([60, 3]), date(2026, 7, 25))
        self.assertEqual(r.low_volume, [])

    def test_flagged_month_is_not_excluded(self):
        # 시장 냉각일 수 있으므로 표시만 하고 데이터에서 빼지 않는다
        r = assess(self._months([60, 62, 58, 61, 59, 5]), date(2026, 7, 25))
        self.assertEqual(r.provisional, [])
        self.assertEqual(len(r.complete_months), 6)

    def test_ratio_and_sigma_both_required(self):
        # 큰 표본에서 비율 조건만 아슬아슬하게 만족 → 미표시
        base = 100
        r = assess(self._months([base] * 5 + [int(base * (LOW_VOLUME_RATIO + 0.05))]),
                   date(2026, 7, 25))
        self.assertEqual(r.low_volume, [])

    def test_baseline_window_bounded(self):
        self.assertGreaterEqual(BASELINE_MONTHS, 3)


class TestTrendExclusion(unittest.TestCase):
    def _series(self):
        # 12개월 완만한 상승 + 마지막 월은 표본 1건의 이상치
        txs = []
        for i in range(12):
            mk = 2025 * 12 + 7 + i
            y, m = divmod(mk - 1, 12)
            for _ in range(10):
                txs.append(_tx(date(y, m + 1, 5),
                               price=int(500_000_000 * (1 + 0.003 * i))))
        mk = 2025 * 12 + 19
        y, m = divmod(mk - 1, 12)
        txs.append(_tx(date(y, m + 1, 5), price=300_000_000))  # 미완결 월 이상치
        return txs, mk

    def test_excluded_month_removed_from_series(self):
        txs, last = self._series()
        t = monthly_trend(txs, exclude_months={last})
        self.assertNotIn(last, t.months)
        self.assertEqual(t.excluded_months, [last])

    def test_incomplete_endpoint_flips_trend_direction(self):
        """끝점 레버리지 — 미완결 월 하나가 추세 부호를 바꿀 수 있다."""
        txs, last = self._series()
        naive = monthly_trend(txs)
        corrected = monthly_trend(txs, exclude_months={last})
        self.assertLess(naive.slope_pct_per_year, 0.0)
        self.assertGreater(corrected.slope_pct_per_year, 0.0)

    def test_no_exclusion_is_identity(self):
        txs, _ = self._series()
        self.assertEqual(monthly_trend(txs).months,
                         monthly_trend(txs, exclude_months=set()).months)

    def test_empty_after_exclusion_is_safe(self):
        txs, last = self._series()
        t = monthly_trend(txs, exclude_months={m for m in
                                               {month_key(x.trade_date) for x in txs}})
        self.assertEqual(t.n_months, 0)
        self.assertEqual(t.slope_pct_per_year, 0.0)
        self.assertTrue(t.excluded_months)


class TestLiquidityWindow(unittest.TestCase):
    def _fixture(self):
        comps = {"C1": Comparable(id="C1", name="비교1", built_year=2018,
                                  units=500, brand_tier=2, dist_m=300)}
        # 2024-02 ~ 2026-05 는 월 10건(완결), 2026-06·07 은 신고 진행 중
        txs = []
        for i in range(28):
            mk = 2024 * 12 + 2 + i
            y, m = divmod(mk - 1, 12)
            for _ in range(10):
                txs.append(_tx(date(y, m + 1, 5)))
        for d, n in ((date(2026, 6, 5), 3), (date(2026, 7, 5), 2)):
            txs += [_tx(d) for _ in range(n)]
        return comps, txs

    def test_partial_month_depresses_turnover(self):
        comps, txs = self._fixture()
        asof = date(2026, 7, 25)
        naive = analyze_liquidity(txs, comps, asof)
        fixed = analyze_liquidity(txs, comps, asof,
                                  end_month=assess(txs, asof).last_complete)
        self.assertGreater(fixed.turnover_pct_year, naive.turnover_pct_year)

    def test_window_end_recorded_as_limitation(self):
        comps, txs = self._fixture()
        asof = date(2026, 7, 25)
        r = analyze_liquidity(txs, comps, asof,
                              end_month=assess(txs, asof).last_complete)
        self.assertTrue(any("미완결" in l for l in r.limitations))

    def test_default_behaviour_unchanged(self):
        comps, txs = self._fixture()
        asof = date(2026, 7, 25)
        a = analyze_liquidity(txs, comps, asof)
        b = analyze_liquidity(txs, comps, asof,
                              end_month=asof.year * 12 + asof.month)
        self.assertEqual(a.n_trades, b.n_trades)
        self.assertEqual(a.limitations, b.limitations)

    def test_future_months_never_counted(self):
        comps, txs = self._fixture()
        txs.append(_tx(date(2027, 1, 5)))
        asof = date(2026, 7, 25)
        r = analyze_liquidity(txs, comps, asof,
                              end_month=assess(txs, asof).last_complete)
        self.assertEqual(r.turnover_pct_year,
                         analyze_liquidity(txs[:-1], comps, asof,
                                           end_month=assess(txs[:-1], asof).last_complete
                                           ).turnover_pct_year)


class TestBacktestRealtime(unittest.TestCase):
    def test_settle_truncation_shrinks_training_set(self):
        comps = sd.build_comparables()
        txs = sd.build_transactions(comps)
        kept = clean(txs).kept
        cuts = quarterly_cutoffs(min(t.trade_date for t in kept), sd.ASOF)
        site = sd.build_site()
        realtime = backtest_price_bands(site, comps, txs, cuts)
        oracle = backtest_price_bands(site, comps, txs, cuts, settle_days=0)
        # 학습 표본이 실제로 달라져야 한다 — 같은 구간이 나오면 절단이 무의미
        self.assertLessEqual(realtime.n, oracle.n)
        self.assertGreater(
            sum(1 for a, b in zip(realtime.folds, oracle.folds)
                if (a.lo, a.hi) != (b.lo, b.hi)), 0)
        self.assertNotEqual(realtime.coverage, oracle.coverage)

    def test_complete_only_helper(self):
        txs = [_tx(date(2026, 7, 20)), _tx(date(2026, 5, 1))]
        self.assertEqual(len(complete_only(txs, date(2026, 7, 25))), 1)

    def test_complete_only_is_conservative_not_lossy_for_old_data(self):
        old = [_tx(date(2024, 1, 1))] * 5
        self.assertEqual(len(complete_only(old, date(2026, 7, 25))), 5)


class TestPipelineIntegration(unittest.TestCase):
    def setUp(self):
        comps = sd.build_comparables()
        self.res = run(site=sd.build_site(), comps=comps,
                       txs=sd.build_transactions(comps),
                       sub_history=sd.build_subscription_history(),
                       supply_items=sd.build_supply(),
                       catalyst_plans_old=sd.build_catalysts(),
                       catalyst_plans_new=sd.build_catalysts(),
                       dataset_meta=sd.build_dataset_meta(),
                       incomes=sd.build_incomes(), feedback=sd.build_feedback(),
                       listings=sd.build_listing_snapshots(),
                       asof=sd.ASOF, ledger=ForecastLedger())

    def test_lag_attached_and_reported(self):
        self.assertIsNotNone(self.res.inputs.lag)
        self.assertIn("신고지연 보정", self.res.markdown)

    def test_trend_excludes_provisional_months(self):
        lag = self.res.inputs.lag
        trend = self.res.inputs.trend
        self.assertEqual(sorted(trend.excluded_months), sorted(lag.provisional))
        for mk in lag.provisional:
            self.assertNotIn(mk, trend.months)

    def test_evidence_records_lag(self):
        md = self.res.inputs.evidence.as_markdown()
        self.assertIn("신고지연 보정", md)


if __name__ == "__main__":
    unittest.main()
