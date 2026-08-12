"""경쟁 현장 모니터링(P2-6) 및 정제 룰 버전화(P2-3) 테스트."""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.competitor import (STALE_DAYS, CompetitorSnapshot, diff,
                                      freshness, scan)
from report_system.ledger import ForecastLedger
from report_system.pipeline import run
from report_system.transactions import RULES_VERSION, clean

ASOF = date(2026, 7, 25)


def snap(**kw) -> CompetitorSnapshot:
    base = dict(name="경쟁A", asof=ASOF, price_per_m2=10_000_000,
                remaining_units=100, incentives=["중도금 무이자"])
    base.update(kw)
    return CompetitorSnapshot(**base)  # type: ignore[arg-type]


class TestCompetitorDiff(unittest.TestCase):
    def test_price_cut_detected(self):
        alerts = diff(snap(), snap(price_per_m2=9_500_000))
        self.assertTrue(any("인하" in a.message for a in alerts))
        self.assertIn("게시 중 광고 문구", alerts[0].refresh_targets)

    def test_small_price_move_ignored(self):
        self.assertEqual(diff(snap(), snap(price_per_m2=10_100_000)), [])

    def test_incentive_added_and_removed(self):
        added = diff(snap(), snap(incentives=["중도금 무이자", "발코니 무상"]))
        self.assertTrue(any("혜택 추가" in a.message for a in added))
        removed = diff(snap(), snap(incentives=[]))
        self.assertTrue(any("혜택 축소" in a.message for a in removed))

    def test_remaining_units_surge_means_competitor_stalling(self):
        alerts = diff(snap(), snap(remaining_units=130))
        self.assertTrue(any("판매 정체" in a.message for a in alerts))

    def test_remaining_units_drop_flags_demand_shift(self):
        alerts = diff(snap(), snap(remaining_units=70))
        self.assertTrue(any("소진" in a.message for a in alerts))
        self.assertIn("수요 판정", alerts[0].refresh_targets)


class TestFreshness(unittest.TestCase):
    def test_stale_snapshot_warns(self):
        old = snap(asof=ASOF - timedelta(days=STALE_DAYS + 5))
        alerts = freshness([old], ASOF)
        self.assertEqual(len(alerts), 1)
        self.assertIn("재수집 필요", alerts[0].message)

    def test_fresh_snapshot_silent(self):
        self.assertEqual(freshness([snap()], ASOF), [])

    def test_scan_combines_diff_and_freshness(self):
        old = [snap()]
        new = [snap(price_per_m2=9_000_000,
                    asof=ASOF - timedelta(days=STALE_DAYS + 1))]
        alerts = scan(old, new, ASOF)
        self.assertTrue(any("인하" in a.message for a in alerts))
        self.assertTrue(any("재수집" in a.message for a in alerts))

    def test_unknown_competitor_only_freshness(self):
        alerts = scan([], [snap()], ASOF)
        self.assertEqual(alerts, [])


class TestRuleVersioning(unittest.TestCase):
    def test_clean_result_carries_version(self):
        cr = clean(sd.build_transactions(sd.build_comparables()))
        self.assertEqual(cr.rules_version, RULES_VERSION)

    def test_report_shows_rule_version(self):
        comps = sd.build_comparables()
        res = run(site=sd.build_site(), comps=comps,
                  txs=sd.build_transactions(comps),
                  sub_history=sd.build_subscription_history(),
                  supply_items=sd.build_supply(),
                  catalyst_plans_old=sd.build_catalysts(),
                  catalyst_plans_new=sd.build_catalysts(),
                  dataset_meta=sd.build_dataset_meta(),
                  incomes=sd.build_incomes(),
                  feedback=sd.build_feedback(),
                  listings=sd.build_listing_snapshots(),
                  asof=sd.ASOF, ledger=ForecastLedger())
        self.assertIn(RULES_VERSION, res.markdown)
        self.assertIn("정제 룰 버전", res.markdown)


class TestPipelineCompetitorWiring(unittest.TestCase):
    def test_competitor_alerts_reach_report(self):
        comps = sd.build_comparables()
        res = run(site=sd.build_site(), comps=comps,
                  txs=sd.build_transactions(comps),
                  sub_history=sd.build_subscription_history(),
                  supply_items=sd.build_supply(),
                  catalyst_plans_old=sd.build_catalysts(),
                  catalyst_plans_new=sd.build_catalysts(),
                  dataset_meta=sd.build_dataset_meta(),
                  incomes=sd.build_incomes(),
                  feedback=sd.build_feedback(),
                  listings=sd.build_listing_snapshots(),
                  asof=sd.ASOF, ledger=ForecastLedger(),
                  competitors_old=[snap()],
                  competitors_new=[snap(price_per_m2=9_400_000)])
        self.assertTrue(any(a.category == "경쟁 현장" for a in res.inputs.alerts))
        self.assertIn("경쟁 현장", res.markdown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
