"""분양가 결정 시뮬레이터 테스트.

핵심은 두 가지다. (1) 가격을 올리면 지표들이 경제적으로 납득 가능한 방향으로
움직이는가. (2) 권고가 '규칙'으로 재현되는가 — 조건을 바꾸면 답도 바뀌고,
조건을 만족하는 후보가 없으면 권고하지 않는가.
"""
from __future__ import annotations

import sys
import unittest
from statistics import median
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.ledger import ForecastLedger
from report_system.models import SubscriptionRecord
from report_system.pipeline import run
from report_system.price_decision import (DEFAULT_STEPS, SHORTFALL_LIMIT,
                                          sweep)
from report_system.pricing import quality_adjusted_bands
from report_system.transactions import clean


def _fixture():
    comps = sd.build_comparables()
    txs = clean(sd.build_transactions(comps)).kept
    site = sd.build_site()
    bands = quality_adjusted_bands(site, comps, txs, sd.ASOF)
    mkt = median(t.price / t.area_m2 for t in txs)
    return site, bands, mkt, txs


class TestSweep(unittest.TestCase):
    def setUp(self):
        self.site, self.bands, self.mkt, _ = _fixture()
        self.incomes = sd.build_incomes()
        self.hist = sd.build_subscription_history()

    def _sweep(self, **kw):
        return sweep(self.site, self.bands, self.mkt, self.incomes, self.hist,
                     900, **kw)

    def test_one_option_per_step(self):
        dec = self._sweep()
        self.assertEqual(len(dec.options), len(DEFAULT_STEPS))
        self.assertAlmostEqual(dec.options[0].multiplier, -0.10, places=6)
        self.assertAlmostEqual(dec.options[-1].multiplier, +0.10, places=6)

    def test_revenue_rises_monotonically_with_price(self):
        rev = [o.total_revenue for o in self._sweep().options]
        self.assertEqual(rev, sorted(rev))

    def test_gap_and_ppsm_rise_with_price(self):
        opts = self._sweep().options
        self.assertEqual([o.gap_pct for o in opts],
                         sorted(o.gap_pct for o in opts))
        self.assertEqual([o.subject_ppsm for o in opts],
                         sorted(o.subject_ppsm for o in opts))

    def test_affordability_falls_as_price_rises(self):
        opts = self._sweep().options
        shares = [o.eligible_share for o in opts]
        self.assertTrue(all(s is not None for s in shares))
        self.assertGreaterEqual(shares[0], shares[-1])

    def test_shortfall_risk_rises_as_price_rises(self):
        opts = [o for o in self._sweep().options if o.shortfall is not None]
        self.assertGreater(len(opts), 4)
        self.assertLessEqual(opts[0].shortfall, opts[-1].shortfall)

    def test_band_label_moves_to_upper_at_high_prices(self):
        opts = self._sweep().options
        self.assertTrue(opts[0].within_band)
        self.assertFalse(opts[-1].within_band)


class TestRecommendation(unittest.TestCase):
    def setUp(self):
        self.site, self.bands, self.mkt, _ = _fixture()
        self.incomes = sd.build_incomes()
        self.hist = sd.build_subscription_history()

    def _sweep(self, **kw):
        return sweep(self.site, self.bands, self.mkt, self.incomes, self.hist,
                     900, **kw)

    def test_recommends_highest_price_meeting_both_conditions(self):
        dec = self._sweep()
        self.assertIsNotNone(dec.recommended)
        r = dec.recommended
        self.assertTrue(r.within_band)
        self.assertLessEqual(r.shortfall, SHORTFALL_LIMIT)
        # 더 높은 가격 중 두 조건을 만족하는 후보가 없어야 한다
        higher = [o for o in dec.options if o.multiplier > r.multiplier
                  and o.within_band and o.shortfall is not None
                  and o.shortfall <= SHORTFALL_LIMIT]
        self.assertEqual(higher, [])

    def test_stricter_threshold_never_recommends_higher_price(self):
        loose = self._sweep(shortfall_limit=0.40)
        strict = self._sweep(shortfall_limit=0.05)
        self.assertIsNotNone(loose.recommended)
        if strict.recommended is not None:
            self.assertLessEqual(strict.recommended.multiplier,
                                 loose.recommended.multiplier)

    def test_criteria_and_limitations_always_stated(self):
        dec = self._sweep()
        self.assertIn("미달 위험", dec.criteria)
        self.assertTrue(dec.reason)
        self.assertTrue(any("조건을 바꾸면" in x for x in dec.limitations))

    def test_no_recommendation_when_subscription_samples_missing(self):
        dec = sweep(self.site, self.bands, self.mkt, self.incomes, [], 900)
        self.assertIsNone(dec.recommended)
        self.assertIn("미달 위험을 산출할 수 없음", dec.reason)

    def test_no_recommendation_when_every_option_is_above_band(self):
        """모든 후보가 밴드 상단이면 권고하지 않고 그 사실을 말한다."""
        dec = sweep(self.site, self.bands, self.mkt, self.incomes, self.hist,
                    900, steps=(1.05, 1.075, 1.10))
        self.assertTrue(all(not o.within_band for o in dec.options))
        self.assertIsNone(dec.recommended)
        self.assertIn("재검토", dec.reason)

    def test_extreme_prices_report_sample_shortage_not_infeasibility(self):
        """가격 갭이 극단이면 유사 사례가 없어진다 — 다른 사유로 구분해 말한다."""
        dec = sweep(self.site, self.bands, self.mkt, self.incomes, self.hist,
                    900, steps=(1.30, 1.40, 1.50))
        self.assertTrue(all(o.shortfall is None for o in dec.options))
        self.assertIsNone(dec.recommended)
        self.assertIn("미달 위험을 산출할 수 없음", dec.reason)

    def test_markdown_marks_the_recommended_row(self):
        md = self._sweep().as_markdown()
        self.assertEqual(md.count("←권고"), 1)
        self.assertIn("판단 기준", md)
        self.assertIn("결과", md)

    def test_shortfall_dominated_history_blocks_all_prices(self):
        """모든 유사 사례가 미달이면 어떤 가격도 권고하지 않는다."""
        bad = [SubscriptionRecord(
            complex_id=f"B-{i}", open_date=sd.ASOF.replace(year=2025),
            units=400, applicants=40, region="샘플권역",
            price_gap_pct=float(i % 20 - 10), concurrent_supply=900,
            sold_out_in_order=False) for i in range(40)]
        dec = sweep(self.site, self.bands, self.mkt, self.incomes, bad, 900)
        self.assertIsNone(dec.recommended)


class TestPipelineIntegration(unittest.TestCase):
    def _run(self, **kw):
        comps = sd.build_comparables()
        return run(site=sd.build_site(), comps=comps,
                   txs=sd.build_transactions(comps),
                   sub_history=sd.build_subscription_history(),
                   supply_items=sd.build_supply(),
                   catalyst_plans_old=sd.build_catalysts(),
                   catalyst_plans_new=sd.build_catalysts(),
                   dataset_meta=sd.build_dataset_meta(),
                   incomes=sd.build_incomes(), feedback=sd.build_feedback(),
                   listings=sd.build_listing_snapshots(),
                   asof=sd.ASOF, ledger=ForecastLedger(), **kw)

    def test_section_present_and_marked_forecast(self):
        res = self._run()
        self.assertIn("6-5. 분양가 결정 시뮬레이션 [FORECAST]", res.markdown)
        self.assertIn("←권고", res.markdown)

    def test_evidence_registers_recommendation_with_criteria(self):
        res = self._run()
        ev = next(e for e in res.inputs.evidence.items if e.metric == "분양가 권고")
        self.assertIn("미달 위험", ev.method)
        self.assertTrue(any("조건을 바꾸면" in x for x in ev.limitations))

    def test_skipped_for_products_without_subscription(self):
        site = sd.build_site()
        site.product_type = "지식산업센터"
        comps = sd.build_comparables()
        res = run(site=site, comps=comps, txs=sd.build_transactions(comps),
                  sub_history=sd.build_subscription_history(),
                  supply_items=sd.build_supply(),
                  catalyst_plans_old=sd.build_catalysts(),
                  catalyst_plans_new=sd.build_catalysts(),
                  dataset_meta=sd.build_dataset_meta(),
                  incomes=sd.build_incomes(), feedback=sd.build_feedback(),
                  listings=sd.build_listing_snapshots(),
                  asof=sd.ASOF, ledger=ForecastLedger())
        self.assertIsNone(res.inputs.price_decision)
        self.assertNotIn("6-5.", res.markdown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
