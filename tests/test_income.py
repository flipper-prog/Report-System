"""소득·구매력 커넥터(L5) 테스트.

실부담 시뮬레이션은 소득 분포 위에 서 있다. 중심값이 설정에 적어 넣은 추정치면
'구매 가능 가구 비율'은 사실상 입력한 사람이 정한 값이 된다. 이 테스트는 중심이
실측으로 고정되는지, 그리고 남은 가정(산포)이 계속 드러나는지를 확인한다.
"""
from __future__ import annotations

import json
import statistics
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.connectors import income
from report_system.connectors.migration import KosisApiError
from report_system.ledger import ForecastLedger
from report_system.pipeline import run

CSV = """period,median_income,mean_income,n_filers
2022,41200000,52800000,412000
2024,45800000,59100000,423000
2023,43500000,55900000,418000
"""


class TestIncomeFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, text, name="i.csv"):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def test_uses_latest_year_regardless_of_row_order(self):
        s = income.load(self._write(CSV))
        self.assertEqual(s.period, "2024")
        self.assertEqual(s.median_income, 45_800_000)
        self.assertEqual(s.n_filers, 423_000)

    def test_summary_states_sigma_is_an_assumption(self):
        s = income.load(self._write(CSV))
        self.assertIn("산포 가정", s.summary())
        self.assertTrue(any("산포" in x and "가정" in x for x in s.limitations))

    def test_spatial_limitation_always_present(self):
        s = income.load(self._write(CSV))
        self.assertTrue(any("시군구 단위" in x for x in s.limitations))

    def test_unit_error_is_rejected(self):
        """만원 단위로 잘못 넣으면 조용히 쓰지 않고 거부한다."""
        with self.assertRaises(income.IncomeFormatError) as ctx:
            income.load(self._write("period,median_income\n2024,4580\n"))
        self.assertIn("상식 범위", str(ctx.exception))

    def test_implausible_mean_median_ratio_flagged_not_fatal(self):
        s = income.load(self._write(
            "period,median_income,mean_income\n2024,45800000,320000000\n"))
        self.assertEqual(s.median_income, 45_800_000)
        self.assertTrue(any("평균/중위" in x for x in s.limitations))

    def test_missing_column_raises(self):
        with self.assertRaises(income.IncomeFormatError):
            income.load(self._write("period,mean_income\n2024,50000000\n"))

    def test_missing_file_raises(self):
        with self.assertRaises(income.IncomeFormatError):
            income.load(str(self.tmp / "none.csv"))

    def test_empty_file_raises(self):
        with self.assertRaises(income.IncomeFormatError):
            income.load(self._write("period,median_income\n"))

    def test_json_supported(self):
        p = self.tmp / "i.json"
        p.write_text(json.dumps([{"period": "2024", "median_income": 45800000}]),
                     encoding="utf-8")
        self.assertEqual(income.load(str(p)).median_income, 45_800_000)


class TestSampling(unittest.TestCase):
    def test_sample_median_matches_measured_median(self):
        s = income.IncomeStats("2024", 45_800_000, sigma=0.45)
        vals = s.sample(4000, seed=3)
        self.assertAlmostEqual(statistics.median(vals) / s.median_income, 1.0,
                               delta=0.05)

    def test_sigma_controls_spread(self):
        tight = income.IncomeStats("2024", 45_800_000, sigma=0.15).sample(3000, seed=5)
        wide = income.IncomeStats("2024", 45_800_000, sigma=0.75).sample(3000, seed=5)
        self.assertLess(statistics.pstdev(tight), statistics.pstdev(wide))

    def test_sampling_is_deterministic(self):
        s = income.IncomeStats("2024", 45_800_000)
        self.assertEqual(s.sample(50, seed=11), s.sample(50, seed=11))

    def test_no_negative_or_absurdly_low_samples(self):
        s = income.IncomeStats("2024", 45_800_000, sigma=0.9)
        self.assertTrue(all(v >= 45_800_000 * 0.3 for v in s.sample(2000, seed=2)))


class TestKosis(unittest.TestCase):
    def _fetcher(self, rows):
        class F:
            def get(self, source, url, params):
                return json.dumps(rows).encode()
        return F()

    def test_picks_latest_year_and_maps_items(self):
        rows = [{"PRD_DE": "2023", "ITM_ID": "M", "DT": "43500000"},
                {"PRD_DE": "2024", "ITM_ID": "M", "DT": "45,800,000"},
                {"PRD_DE": "2024", "ITM_ID": "A", "DT": "59100000"},
                {"PRD_DE": "2024", "ITM_ID": "N", "DT": "423000"}]
        s = income.fetch_kosis(self._fetcher(rows), "k",
                               {"orgId": "133", "tblId": "T",
                                "items": {"median": "M", "mean": "A",
                                          "filers": "N"}})
        self.assertEqual(s.period, "2024")
        self.assertEqual(s.median_income, 45_800_000)
        self.assertEqual(s.mean_income, 59_100_000)
        self.assertEqual(s.n_filers, 423_000)

    def test_requires_median_item_code(self):
        with self.assertRaises(KosisApiError):
            income.fetch_kosis(None, "k", {"items": {"mean": "A"}})

    def test_no_matching_items_raises(self):
        rows = [{"PRD_DE": "2024", "ITM_ID": "X", "DT": "1"}]
        with self.assertRaises(KosisApiError):
            income.fetch_kosis(self._fetcher(rows), "k",
                               {"items": {"median": "M"}})

    def test_error_response_raises(self):
        class F:
            def get(self, source, url, params):
                return json.dumps({"err": "20", "errMsg": "인증키 오류"}).encode()
        with self.assertRaises(KosisApiError):
            income.fetch_kosis(F(), "k", {"items": {"median": "M"}})


class TestIntegration(unittest.TestCase):
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

    def test_report_states_measured_center(self):
        res = self._run(income_stats=sd.build_income_stats())
        self.assertIn("소득 분포 중심", res.markdown)
        self.assertIn("산포 가정", res.markdown)

    def test_coverage_upgrades_from_substitute_to_conditional(self):
        a = self._run()
        b = self._run(income_stats=sd.build_income_stats())
        rows_a = {r.layer.split()[0]: r for r in a.inputs.coverage_rows}
        rows_b = {r.layer.split()[0]: r for r in b.inputs.coverage_rows}
        self.assertEqual(rows_a["L5"].coverage.value, "대체 가능")
        self.assertEqual(rows_b["L5"].coverage.value, "조건부")
        self.assertIn("산포", rows_b["L5"].note)

    def test_evidence_limitation_changes_with_measured_income(self):
        a = self._run()
        b = self._run(income_stats=sd.build_income_stats())
        ea = next(e for e in a.inputs.evidence.items if "구매 가능" in e.metric)
        eb = next(e for e in b.inputs.evidence.items if "구매 가능" in e.metric)
        self.assertTrue(any("교체 권고" in x for x in ea.limitations))
        self.assertFalse(any("교체 권고" in x for x in eb.limitations))
        self.assertTrue(any("소득 분포 중심" in x for x in eb.limitations))
        # 남은 가정은 두 경우 모두 드러나야 한다
        for e in (ea, eb):
            self.assertTrue(any("자기자본" in x for x in e.limitations))

    def test_without_income_stats_report_unchanged(self):
        res = self._run()
        self.assertNotIn("소득 분포 중심", res.markdown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
