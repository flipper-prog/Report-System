"""경쟁 현장 스냅숏 적재·연동 테스트.

`competitor.py` 는 오래 전부터 구현·테스트돼 있었으나 실제 실행 경로에 한 번도
연결되지 않아 사실상 죽어 있었다. 이 테스트는 파일 → 회차 짝짓기 → 변화 감지 →
리포트까지가 실제로 이어지는지 확인한다.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.competitor import scan
from report_system.connectors import competitors as cx
from report_system.ledger import ForecastLedger
from report_system.pipeline import run

CSV = """name,asof,price_per_m2,remaining_units,incentives,note
A현장,2026-06-20,10800000,142,"중도금 무이자",
B현장,2026-06-20,10250000,86,,
A현장,2026-07-18,10400000,196,"중도금 무이자,이사비 지원",인하 확인
B현장,2026-07-18,10300000,58,,
"""


class TestLoad(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, text, name="c.csv"):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def test_splits_latest_and_previous_rounds(self):
        r = cx.load(self._write(CSV))
        self.assertEqual({s.asof for s in r.latest}, {date(2026, 7, 18)})
        self.assertEqual({s.asof for s in r.previous}, {date(2026, 6, 20)})
        self.assertTrue(r.comparable)
        self.assertIn("2개 비교", r.summary())

    def test_incentives_parsed_as_list(self):
        r = cx.load(self._write(CSV))
        a = next(s for s in r.latest if s.name == "A현장")
        self.assertEqual(a.incentives, ["중도금 무이자", "이사비 지원"])
        b = next(s for s in r.latest if s.name == "B현장")
        self.assertEqual(b.incentives, [])

    def test_single_round_is_not_comparable(self):
        one = "\n".join(CSV.split("\n")[:3]) + "\n"
        r = cx.load(self._write(one))
        self.assertTrue(r.latest)
        self.assertEqual(r.previous, [])
        self.assertFalse(r.comparable)
        self.assertIn("변화 감지 불가", r.summary())
        self.assertTrue(any("직전 회차가 없거나" in x for x in r.limitations))

    def test_name_mismatch_between_rounds_flagged(self):
        txt = ("name,asof,price_per_m2,remaining_units\n"
               "구A현장,2026-06-20,10800000,142\n"
               "신A현장,2026-07-18,10400000,196\n")
        r = cx.load(self._write(txt))
        self.assertFalse(r.comparable)
        self.assertTrue(any("현장명을 동일하게" in x for x in r.limitations))

    def test_future_snapshots_excluded(self):
        r = cx.load(self._write(CSV), until=date(2026, 6, 30))
        self.assertEqual({s.asof for s in r.latest}, {date(2026, 6, 20)})
        self.assertTrue(any("이후 수집" in x for x in r.skipped))

    def test_bad_rows_skipped(self):
        txt = ("name,asof,price_per_m2,remaining_units\n"
               "정상,2026-07-18,10400000,196\n"
               ",2026-07-18,10400000,196\n"
               "가격오류,2026-07-18,0,10\n"
               "날짜오류,not-a-date,10400000,10\n")
        r = cx.load(self._write(txt))
        self.assertEqual(len(r.latest), 1)
        self.assertEqual(len(r.skipped), 3)

    def test_missing_column_raises(self):
        with self.assertRaises(cx.CompetitorFormatError):
            cx.load(self._write("name,asof\nA,2026-07-18\n"))

    def test_missing_file_raises(self):
        with self.assertRaises(cx.CompetitorFormatError):
            cx.load(str(self.tmp / "none.csv"))

    def test_json_supported(self):
        p = self.tmp / "c.json"
        p.write_text(json.dumps([
            {"name": "A", "asof": "2026-07-18", "price_per_m2": 1e7,
             "remaining_units": 10}]), encoding="utf-8")
        self.assertEqual(len(cx.load(str(p)).latest), 1)

    def test_manual_collection_limitation_always_present(self):
        r = cx.load(self._write(CSV))
        self.assertTrue(any("수기 수집" in x for x in r.limitations))


class TestDetection(unittest.TestCase):
    def test_file_path_detects_same_changes_as_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.csv"
            p.write_text(CSV, encoding="utf-8")
            r = cx.load(str(p))
        msgs = [a.message for a in scan(r.previous, r.latest, date(2026, 7, 25))]
        self.assertTrue(any("분양가 인하" in m for m in msgs))
        self.assertTrue(any("혜택 추가" in m for m in msgs))
        self.assertTrue(any("잔여 세대 증가" in m for m in msgs))
        self.assertTrue(any("잔여 세대 감소" in m for m in msgs))

    def test_sample_builder_produces_all_alert_kinds(self):
        old, new = sd.build_competitors()
        msgs = [a.message for a in scan(old, new, sd.ASOF)]
        for token in ("분양가 인하", "혜택 추가", "잔여 세대 증가",
                      "잔여 세대 감소", "재수집 필요"):
            self.assertTrue(any(token in m for m in msgs), token)


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

    def test_alerts_reach_the_report(self):
        old, new = sd.build_competitors()
        res = self._run(competitors_old=old, competitors_new=new)
        self.assertIn("경쟁 현장", res.markdown)
        self.assertIn("분양가 인하", res.markdown)

    def test_evidence_registers_change_count(self):
        old, new = sd.build_competitors()
        res = self._run(competitors_old=old, competitors_new=new)
        ev = next(e for e in res.inputs.evidence.items if e.metric == "경쟁 현장 변동")
        self.assertNotEqual(ev.value, "미산출")
        self.assertTrue(any("수기 수집" in x for x in ev.limitations))

    def test_without_snapshots_registered_as_missing(self):
        res = self._run()
        ev = next(e for e in res.inputs.evidence.items if e.metric == "경쟁 현장 변동")
        self.assertEqual(ev.value, "미산출")
        self.assertTrue(any("회차가 1개" in x for x in ev.limitations))


if __name__ == "__main__":
    unittest.main(verbosity=2)
