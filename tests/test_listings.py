"""매물·호가 커넥터(P1-2) 테스트."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system.alerts import scan_market
from report_system.connectors.listings import ListingsFormatError, load

CSV_OK = """asof,listings,ask_ppsm,traded_ppsm
2026-05-25,210,10900000,10400000
2026-06-25,240,11100000,10380000
2026-07-25,268,11300000,10350000
"""

CSV_MIXED = """asof,listings,ask_ppsm,traded_ppsm
2026-05-25,210,10900000,10400000
bad-date,100,1,1
2026-06-25,-5,10900000,10400000
2026-06-25,240,11100000,10380000
2026-12-25,999,12000000,10000000
"""

CSV_MISSING_COL = """asof,listings
2026-05-25,210
"""


def write(tmp: Path, name: str, text: str) -> str:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return str(p)


class TestListingsLoad(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_csv_loads_sorted(self):
        r = load(write(self.tmp, "a.csv", CSV_OK))
        self.assertEqual(len(r.snapshots), 3)
        self.assertEqual([s.asof.month for s in r.snapshots], [5, 6, 7])
        self.assertEqual(r.skipped, [])

    def test_latest_pair(self):
        r = load(write(self.tmp, "a.csv", CSV_OK))
        prev, cur = r.latest_pair
        self.assertEqual(prev.asof, date(2026, 6, 25))
        self.assertEqual(cur.listings, 268)

    def test_bad_rows_skipped_with_reasons(self):
        r = load(write(self.tmp, "b.csv", CSV_MIXED), until=date(2026, 7, 25))
        self.assertEqual(len(r.snapshots), 2)          # 정상 2건만
        self.assertEqual(len(r.skipped), 3)            # 날짜오류·음수·미래
        self.assertTrue(any("파싱 실패" in s for s in r.skipped))
        self.assertTrue(any("값 범위" in s for s in r.skipped))
        self.assertTrue(any("이후 관측" in s for s in r.skipped))

    def test_future_rows_excluded_by_until(self):
        r = load(write(self.tmp, "c.csv", CSV_OK), until=date(2026, 6, 1))
        self.assertEqual(len(r.snapshots), 1)
        self.assertIsNone(r.latest_pair)

    def test_missing_column_raises(self):
        with self.assertRaises(ListingsFormatError) as ctx:
            load(write(self.tmp, "d.csv", CSV_MISSING_COL))
        self.assertIn("ask_ppsm", str(ctx.exception))

    def test_missing_file_raises(self):
        with self.assertRaises(ListingsFormatError):
            load(str(self.tmp / "nope.csv"))

    def test_json_format(self):
        data = [{"asof": "2026-06-25", "listings": 240,
                 "ask_ppsm": 11100000, "traded_ppsm": 10380000},
                {"asof": "2026-07-25", "listings": 268,
                 "ask_ppsm": 11300000, "traded_ppsm": 10350000}]
        p = write(self.tmp, "e.json", json.dumps(data, ensure_ascii=False))
        r = load(p)
        self.assertEqual(len(r.snapshots), 2)
        self.assertIsNotNone(r.latest_pair)

    def test_thousands_separator_tolerated(self):
        csv_txt = ('asof,listings,ask_ppsm,traded_ppsm\n'
                   '2026-07-25,"1,200","11,300,000","10,350,000"\n')
        r = load(write(self.tmp, "f.csv", csv_txt))
        self.assertEqual(r.snapshots[0].listings, 1200)


CSV_SURGE = """asof,listings,ask_ppsm,traded_ppsm
2026-06-25,200,10500000,10300000
2026-07-25,280,11400000,10200000
"""


class TestListingsFeedEarlyWarning(unittest.TestCase):
    """파일에서 읽은 스냅숏이 조기경보를 실제로 발생시키는지 확인."""

    def test_normal_variation_does_not_alert(self):
        # 매물 +11.7%, 갭 +2.2%p — 둘 다 임계 미만이므로 경보가 없어야 정상
        with tempfile.TemporaryDirectory() as tmp:
            r = load(write(Path(tmp), "a.csv", CSV_OK))
            prev, cur = r.latest_pair
            self.assertEqual(scan_market(prev, cur), [])

    def test_surge_and_gap_alerts(self):
        # 매물 +40%, 갭 1.9%→11.8% — 두 경보 모두 발생해야 한다
        with tempfile.TemporaryDirectory() as tmp:
            r = load(write(Path(tmp), "b.csv", CSV_SURGE))
            prev, cur = r.latest_pair
            alerts = scan_market(prev, cur)
            self.assertTrue(any("매물량 급증" in a.message for a in alerts))
            self.assertTrue(any("호가-실거래 갭" in a.message for a in alerts))
            self.assertIn("가격 판정", alerts[0].refresh_targets)


if __name__ == "__main__":
    unittest.main(verbosity=2)
