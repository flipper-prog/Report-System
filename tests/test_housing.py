"""주택건설실적 커넥터(L13) 테스트 — 인허가 추세와 공급 목록 교차검증."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.connectors import housing
from report_system.connectors.migration import KosisApiError
from report_system.ledger import ForecastLedger
from report_system.pipeline import run


def _csv(rows, header="period,permit,start,sale,done"):
    return header + "\n" + "\n".join(rows) + "\n"


FLAT = _csv([f"2025-{m:02d},500,390,310,275" for m in range(1, 13)]
            + [f"2026-{m:02d},510,398,316,280" for m in range(1, 8)])
SURGE = _csv([f"2025-{m:02d},300,234,186,165" for m in range(1, 13)]
             + [f"2026-{m:02d},600,468,372,330" for m in range(1, 13)])
DROP = _csv([f"2025-{m:02d},800,624,496,440" for m in range(1, 13)]
            + [f"2026-{m:02d},300,234,186,165" for m in range(1, 13)])


class TestHousingFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, text, name="h.csv"):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def test_loads_all_fields(self):
        s = housing.load(self._write(FLAT))
        self.assertEqual(len(s.points), 19)
        for f in ("permit", "start", "sale", "done"):
            self.assertTrue(s.has(f))
        self.assertIn("인허가", s.summary())

    def test_surge_labeled(self):
        s = housing.load(self._write(SURGE))
        self.assertGreater(s.yoy_pct("permit"), 30)
        self.assertIn("급증", s.label)
        self.assertIn("공급 압력", s.label)

    def test_drop_labeled(self):
        s = housing.load(self._write(DROP))
        self.assertLess(s.yoy_pct("permit"), -30)
        self.assertIn("감소", s.label)

    def test_yoy_needs_two_full_years(self):
        s = housing.load(self._write(_csv([f"2026-{m:02d},500,0,0,0"
                                           for m in range(1, 8)])))
        self.assertIsNone(s.yoy_pct("permit"))
        self.assertEqual(s.label, "추세 판정 불가")

    def test_permit_only_file_notes_missing_fields(self):
        s = housing.load(self._write(
            _csv([f"2026-{m:02d},500" for m in range(1, 8)], "period,permit")))
        self.assertTrue(s.has("permit"))
        self.assertFalse(s.has("start"))
        self.assertTrue(any("착공" in x for x in s.limitations))

    def test_missing_required_column_raises(self):
        with self.assertRaises(housing.HousingFormatError):
            housing.load(self._write("period,start\n2026-01,100\n"))

    def test_missing_file_raises(self):
        with self.assertRaises(housing.HousingFormatError):
            housing.load(str(self.tmp / "none.csv"))

    def test_bad_rows_skipped(self):
        s = housing.load(self._write(
            "period,permit\n2026-01,100\nbad,50\n2026-02,-3\n"))
        self.assertEqual(len(s.points), 1)
        self.assertEqual(len(s.skipped), 2)

    def test_same_period_rows_merged(self):
        s = housing.load(self._write(
            "period,permit,start\n2026-01,100,80\n2026-01,50,40\n"))
        self.assertEqual(len(s.points), 1)
        self.assertEqual(s.points[0].permit, 150)
        self.assertEqual(s.points[0].start, 120)

    def test_future_periods_excluded(self):
        s = housing.load(self._write(FLAT), until="2025-06")
        self.assertEqual(len(s.points), 6)
        self.assertTrue(any("이후" in x for x in s.skipped))

    def test_json_supported(self):
        p = self.tmp / "h.json"
        p.write_text(json.dumps([{"period": "2026-06", "permit": 100},
                                 {"period": "2026-07", "permit": 140}]),
                     encoding="utf-8")
        self.assertEqual(len(housing.load(str(p)).points), 2)

    def test_scope_limitation_always_present(self):
        s = housing.load(self._write(FLAT))
        self.assertTrue(any("시군구 단위" in x for x in s.limitations))


class TestPipelineCheck(unittest.TestCase):
    """설정 공급 목록 vs 시군구 인허가 실적 교차검증."""

    def setUp(self):
        self.s = sd.build_housing()

    def test_declared_below_district_total_is_normal(self):
        msg = self.s.pipeline_check(4_100)
        self.assertIn("정상", msg)
        self.assertNotIn("확인 필요", msg)

    def test_declared_above_district_total_is_flagged(self):
        msg = self.s.pipeline_check(30_000)
        self.assertIn("확인 필요", msg)
        self.assertIn("중복 집계", msg)

    def test_returns_none_without_data(self):
        self.assertIsNone(housing.HousingSeries().pipeline_check(1000))
        self.assertIsNone(self.s.pipeline_check(0))


class TestKosis(unittest.TestCase):
    def test_parses_item_codes(self):
        rows = [{"PRD_DE": "202606", "ITM_ID": "P", "DT": "1,200"},
                {"PRD_DE": "202606", "ITM_ID": "S", "DT": "900"},
                {"PRD_DE": "202607", "ITM_ID": "P", "DT": "1300"},
                {"PRD_DE": "202607", "ITM_ID": "X", "DT": "999"}]

        class F:
            def get(self, source, url, params):
                return json.dumps(rows).encode()

        s = housing.fetch_kosis(F(), "k", {"orgId": "116", "tblId": "T",
                                           "items": {"permit": "P", "start": "S"}})
        self.assertEqual([p.period for p in s.points], ["2026-06", "2026-07"])
        self.assertEqual(s.points[0].permit, 1200)
        self.assertEqual(s.points[0].start, 900)
        self.assertEqual(s.points[1].start, 0)      # 매핑 밖 코드는 무시

    def test_requires_permit_item_code(self):
        with self.assertRaises(KosisApiError):
            housing.fetch_kosis(None, "k", {"items": {"start": "S"}})

    def test_error_response_raises(self):
        class F:
            def get(self, source, url, params):
                return json.dumps({"err": "20", "errMsg": "인증키 오류"}).encode()

        with self.assertRaises(KosisApiError):
            housing.fetch_kosis(F(), "k", {"items": {"permit": "P"}})


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

    def test_appears_in_supply_verdict_and_report(self):
        res = self._run(housing=sd.build_housing())
        v3 = next(v for v in res.inputs.verdicts if v.name.startswith("③"))
        joined = " ".join(v3.rationale)
        self.assertIn("주택건설실적(L13)", joined)
        self.assertIn("공급 목록 교차검증", joined)
        self.assertIn("L13 주택건설실적", res.markdown)

    def test_permit_surge_blocks_positive_supply_verdict(self):
        """공급 부담이 낮아도 인허가가 급증하면 긍정으로 닫지 않는다."""
        from report_system.models import SupplyItem, SupplyStage
        from report_system.supply import probability_adjusted
        from report_system.verdicts import supply_verdict

        light = probability_adjusted([SupplyItem("소규모", 50, SupplyStage.PERMIT, 30)])
        base = supply_verdict(light, site_units=460)
        self.assertEqual(base.direction, "긍정")

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "h.csv"
            p.write_text(SURGE, encoding="utf-8")
            surged = housing.load(str(p))
        v = supply_verdict(light, site_units=460, housing=surged)
        self.assertEqual(v.direction, "중립")
        self.assertTrue(any("인허가 급증" in r for r in v.rationale))

    def test_coverage_table_upgrades_l13(self):
        without = self._run()
        with_h = self._run(housing=sd.build_housing())
        rows_a = {r.layer.split()[0]: r for r in without.inputs.coverage_rows}
        rows_b = {r.layer.split()[0]: r for r in with_h.inputs.coverage_rows}
        self.assertEqual(rows_a["L13"].coverage.value, "조건부")
        self.assertEqual(rows_b["L13"].coverage.value, "확보 가능")
        self.assertIn("교차검증", rows_b["L13"].note)

    def test_evidence_registers_layer(self):
        res = self._run(housing=sd.build_housing())
        by = {e.metric: e for e in res.inputs.evidence.items}
        self.assertIn("주택건설실적 추세", by)
        self.assertNotEqual(by["주택건설실적 추세"].value, "미산출")

    def test_missing_layer_registered_as_uncollected(self):
        res = self._run()
        by = {e.metric: e for e in res.inputs.evidence.items}
        self.assertEqual(by["주택건설실적 추세"].value, "미산출")


if __name__ == "__main__":
    unittest.main(verbosity=2)
