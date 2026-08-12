"""실행 이력 저장소 및 판정 '변화' 속성 테스트."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.ledger import ForecastLedger
from report_system.pipeline import run
from report_system.runstore import RunSnapshot, RunStore, describe_change


def _snap(asof: str, direction: str = "중립", strength: str = "중",
          confidence: str = "보통", **metrics) -> RunSnapshot:
    base = {"anchor_ppsm": 10_000_000.0, "supply_ratio": 2.0,
            "sub_mid": 5.0, "turnover": 5.0}
    base.update(metrics)
    return RunSnapshot(
        "S1", asof,
        {"③ 공급·환금성 위험": {"direction": direction, "strength": strength,
                              "confidence": confidence}},
        base)


class TestRunStore(unittest.TestCase):
    def test_save_and_latest(self):
        s = RunStore()
        s.save(_snap("2026-01-01"))
        s.save(_snap("2026-04-01"))
        got = s.latest("S1", "2026-07-01")
        self.assertEqual(got.asof, "2026-04-01")

    def test_latest_excludes_same_or_future_asof(self):
        s = RunStore()
        s.save(_snap("2026-07-01"))
        self.assertIsNone(s.latest("S1", "2026-07-01"))
        self.assertIsNone(s.latest("S1", "2026-06-01"))

    def test_history_ordered(self):
        s = RunStore()
        s.save(_snap("2026-04-01"))
        s.save(_snap("2026-01-01"))
        self.assertEqual([h.asof for h in s.history("S1")],
                         ["2026-01-01", "2026-04-01"])

    def test_other_site_isolated(self):
        s = RunStore()
        s.save(_snap("2026-01-01"))
        self.assertIsNone(s.latest("OTHER", "2026-07-01"))


class TestDescribeChange(unittest.TestCase):
    def test_first_run(self):
        self.assertEqual(
            describe_change("③ 공급·환금성 위험", {"direction": "중립"}, None, {}), "최초")

    def test_direction_and_strength_change(self):
        prev = _snap("2026-01-01", direction="중립", strength="중")
        cur = {"direction": "부정", "strength": "강", "confidence": "보통"}
        out = describe_change("③ 공급·환금성 위험", cur, prev, prev.metrics)
        self.assertIn("방향 중립→부정", out)
        self.assertIn("강도 중→강", out)

    def test_no_change_message(self):
        prev = _snap("2026-01-01")
        cur = {"direction": "중립", "strength": "중", "confidence": "보통"}
        out = describe_change("③ 공급·환금성 위험", cur, prev, prev.metrics)
        self.assertIn("변동 없음", out)

    def test_only_relevant_metrics_reported(self):
        """가격 판정에 공급배수 델타가 섞이지 않아야 한다."""
        prev = _snap("2026-01-01")
        cur_metrics = dict(prev.metrics, supply_ratio=4.0, anchor_ppsm=10_000_000.0)
        out = describe_change("① 현재 가격 위치",
                              {"direction": "중립", "strength": "중",
                               "confidence": "보통"}, prev, cur_metrics)
        self.assertNotIn("공급배수", out)

    def test_supply_verdict_reports_supply_delta(self):
        prev = _snap("2026-01-01")
        cur_metrics = dict(prev.metrics, supply_ratio=4.0)
        out = describe_change("③ 공급·환금성 위험",
                              {"direction": "중립", "strength": "중",
                               "confidence": "보통"}, prev, cur_metrics)
        self.assertIn("공급배수 +100%", out)

    def test_small_delta_ignored(self):
        prev = _snap("2026-01-01")
        cur_metrics = dict(prev.metrics, supply_ratio=2.02)   # +1%
        out = describe_change("③ 공급·환금성 위험",
                              {"direction": "중립", "strength": "중",
                               "confidence": "보통"}, prev, cur_metrics)
        self.assertIn("변동 없음", out)


class TestPipelineChangeAttribute(unittest.TestCase):
    def _run(self, store, asof, supply_units=900):
        comps = sd.build_comparables()
        supply = sd.build_supply()
        supply[0].units = supply_units
        return run(site=sd.build_site(), comps=comps,
                   txs=sd.build_transactions(comps),
                   sub_history=sd.build_subscription_history(),
                   supply_items=supply,
                   catalyst_plans_old=sd.build_catalysts(),
                   catalyst_plans_new=sd.build_catalysts(),
                   dataset_meta=sd.build_dataset_meta(),
                   incomes=sd.build_incomes(),
                   feedback=sd.build_feedback(),
                   listings=sd.build_listing_snapshots(),
                   asof=asof, ledger=ForecastLedger(), store=store)

    def test_first_run_is_initial_then_second_shows_delta(self):
        store = RunStore()
        r1 = self._run(store, date(2026, 7, 25))
        self.assertTrue(all(v.change == "최초" for v in r1.inputs.verdicts))

        r2 = self._run(store, date(2026, 8, 25), supply_units=2600)
        v3 = next(v for v in r2.inputs.verdicts if v.name.startswith("③"))
        self.assertIn("직전(2026-07-25)", v3.change)
        self.assertIn("공급배수", v3.change)
        self.assertEqual(v3.strength, "강")     # 공급 급증 → 강도 상향

    def test_without_store_change_stays_initial(self):
        r = self._run(None, date(2026, 7, 25))
        self.assertTrue(all(v.change == "최초" for v in r.inputs.verdicts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
