"""전월세 커넥터(E01-R)와 전세가율 분석 테스트."""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.connectors import rent
from report_system.jeonse import MIN_SAMPLES, analyze
from report_system.ledger import ForecastLedger
from report_system.models import RentRecord, Transaction
from report_system.pipeline import run
from report_system.verdicts import price_verdict

ASOF = date(2026, 7, 25)


def _xml(items: str, total: int, result_code: str = "00") -> bytes:
    return (f"<response><header><resultCode>{result_code}</resultCode>"
            f"<resultMsg>OK</resultMsg></header><body><items>{items}</items>"
            f"<totalCount>{total}</totalCount></body></response>").encode()


NEW_ITEM = """<item>
  <aptNm>래미안</aptNm><umdNm>역삼동</umdNm>
  <excluUseAr>84.9</excluUseAr><floor>12</floor>
  <deposit>70,000</deposit><monthlyRent>0</monthlyRent>
  <dealYear>2026</dealYear><dealMonth>6</dealMonth><dealDay>15</dealDay>
  <buildYear>2015</buildYear><contractType>신규</contractType>
</item>"""

LEGACY_ITEM = """<item>
  <아파트>자이</아파트><법정동>대치동</법정동>
  <전용면적>59.9</전용면적><층>7</층>
  <보증금액>30,000</보증금액><월세금액>120</월세금액>
  <년>2026</년><월>5</월><일>3</일>
  <건축년도>2010</건축년도><계약구분>갱신</계약구분>
</item>"""

NO_DEPOSIT_ITEM = """<item>
  <aptNm>보증금없음</aptNm><excluUseAr>59.9</excluUseAr>
  <deposit>0</deposit><monthlyRent>150</monthlyRent>
  <dealYear>2026</dealYear><dealMonth>5</dealMonth><dealDay>1</dealDay>
</item>"""


class TestRentConnector(unittest.TestCase):
    def test_new_tag_parsing(self):
        rows, total = rent.parse_response(_xml(NEW_ITEM, 1))
        self.assertEqual(total, 1)
        r = rows[0]
        self.assertEqual(r.apt_nm, "래미안")
        self.assertEqual(r.deposit_won, 700_000_000)      # 만원 단위 환산
        self.assertEqual(r.monthly_rent_won, 0)
        self.assertTrue(r.is_jeonse)
        self.assertFalse(r.renewal)
        self.assertEqual(r.deal_date, date(2026, 6, 15))

    def test_legacy_tag_and_renewal(self):
        rows, _ = rent.parse_response(_xml(LEGACY_ITEM, 1))
        r = rows[0]
        self.assertEqual(r.apt_nm, "자이")
        self.assertEqual(r.deposit_won, 300_000_000)
        self.assertEqual(r.monthly_rent_won, 1_200_000)
        self.assertFalse(r.is_jeonse)
        self.assertTrue(r.renewal)

    def test_zero_deposit_dropped(self):
        rows, _ = rent.parse_response(_xml(NO_DEPOSIT_ITEM, 1))
        self.assertEqual(rows, [])

    def test_error_response_raises(self):
        bad = (b"<response><cmmMsgHeader><returnReasonCode>30</returnReasonCode>"
               b"<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>"
               b"</cmmMsgHeader></response>")
        with self.assertRaises(rent.RentApiError):
            rent.parse_response(bad)

    def test_result_code_failure_raises(self):
        with self.assertRaises(rent.RentApiError):
            rent.parse_response(_xml(NEW_ITEM, 1, result_code="99"))

    def test_to_records_filters_unknown_complexes(self):
        rows, _ = rent.parse_response(_xml(NEW_ITEM + LEGACY_ITEM, 2))
        recs = rent.to_records(rows, {"래미안": "C01"})
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].complex_id, "C01")
        self.assertAlmostEqual(recs[0].deposit_ppsm, 700_000_000 / 84.9)

    def test_fetch_range_excludes_future(self):
        class F:
            def __init__(self):
                self.months = []

            def get(self, source, url, params):
                self.months.append(params["DEAL_YMD"])
                future = NEW_ITEM.replace("<dealMonth>6</dealMonth>",
                                          "<dealMonth>12</dealMonth>")
                return _xml(NEW_ITEM + future, 2)

        f = F()
        rows = rent.fetch_range(f, "k", "11680", date(2026, 7, 25), months=3)
        self.assertEqual(f.months, ["202607", "202606", "202605"])
        self.assertTrue(all(r.deal_date <= date(2026, 7, 25) for r in rows))


# ── 전세가율 분석 ────────────────────────────────────────────────────────────

def _rents(n, ppsm, months_ago_range, monthly=0, renewal=False, area=84.9):
    out = []
    for i in range(n):
        d = ASOF - timedelta(days=30 * (months_ago_range[0] + i % max(
            1, months_ago_range[1] - months_ago_range[0])))
        out.append(RentRecord("C01", d, area, 10, int(ppsm * area), monthly, renewal))
    return out


def _sales(n, ppsm, months_ago_range, area=84.9):
    out = []
    for i in range(n):
        d = ASOF - timedelta(days=30 * (months_ago_range[0] + i % max(
            1, months_ago_range[1] - months_ago_range[0])))
        out.append(Transaction("C01", d, area, 10, int(ppsm * area)))
    return out


class TestJeonseAnalysis(unittest.TestCase):
    def test_ratio_computed_and_labeled(self):
        rents = _rents(20, 7_000_000, (1, 11))
        sales = _sales(20, 10_000_000, (1, 11))
        r = analyze(rents, sales, ASOF)
        self.assertAlmostEqual(r.ratio_pct, 70.0, delta=0.5)
        self.assertEqual(r.label, "하방 지지 두터움")

    def test_thin_buffer_labeled(self):
        r = analyze(_rents(20, 5_000_000, (1, 11)),
                    _sales(20, 10_000_000, (1, 11)), ASOF)
        self.assertAlmostEqual(r.ratio_pct, 50.0, delta=0.5)
        self.assertEqual(r.label, "하방 완충 얇음")

    def test_ratio_components_match_headline(self):
        """표에 실리는 ㎡단가로 전세가율을 검산할 수 있어야 한다."""
        r = analyze(_rents(20, 6_000_000, (1, 11)),
                    _sales(20, 10_000_000, (1, 11)), ASOF)
        self.assertAlmostEqual(r.jeonse_ppsm / r.sale_ppsm * 100, r.ratio_pct,
                               places=6)

    def test_renewal_excluded(self):
        rents = _rents(12, 7_000_000, (1, 11)) + \
            _rents(30, 3_000_000, (1, 11), renewal=True)
        r = analyze(rents, _sales(20, 10_000_000, (1, 11)), ASOF)
        self.assertEqual(r.n_jeonse, 12)
        self.assertEqual(r.n_renewal_excluded, 30)
        self.assertAlmostEqual(r.ratio_pct, 70.0, delta=1.0)   # 갱신에 오염되지 않음
        self.assertTrue(any("갱신" in x for x in r.limitations))

    def test_insufficient_jeonse_samples_returns_no_number(self):
        r = analyze(_rents(MIN_SAMPLES - 1, 7_000_000, (1, 11)),
                    _sales(20, 10_000_000, (1, 11)), ASOF)
        self.assertIsNone(r.ratio_pct)
        self.assertEqual(r.label, "판정 불가")
        self.assertTrue(any("LIMITATION" in x for x in r.limitations))
        self.assertIn("LIMITATION", r.as_rationale())

    def test_insufficient_sale_samples_returns_no_number(self):
        r = analyze(_rents(20, 7_000_000, (1, 11)),
                    _sales(MIN_SAMPLES - 1, 10_000_000, (1, 11)), ASOF)
        self.assertIsNone(r.ratio_pct)

    def test_canceled_sales_excluded_from_denominator(self):
        sales = _sales(20, 10_000_000, (1, 11))
        for t in _sales(20, 30_000_000, (1, 11)):
            t.canceled = True
            sales.append(t)
        r = analyze(_rents(20, 7_000_000, (1, 11)), sales, ASOF)
        self.assertEqual(r.n_sale, 20)
        self.assertAlmostEqual(r.ratio_pct, 70.0, delta=0.5)

    def test_future_records_excluded(self):
        rents = _rents(20, 7_000_000, (1, 11))
        rents.append(RentRecord("C01", ASOF + timedelta(days=40), 84.9, 10,
                                999_000_000))
        r = analyze(rents, _sales(20, 10_000_000, (1, 11)), ASOF)
        self.assertEqual(r.n_jeonse, 20)

    def test_rising_trend_detected(self):
        rents = _rents(12, 7_500_000, (1, 5)) + _rents(12, 6_000_000, (7, 11))
        sales = _sales(12, 10_000_000, (1, 5)) + _sales(12, 10_000_000, (7, 11))
        r = analyze(rents, sales, ASOF)
        self.assertGreater(r.ratio_delta_pp, 2.0)
        self.assertIn("상승", r.trend_label)
        self.assertEqual(r.ratio_window_months, 6)

    def test_falling_trend_detected(self):
        rents = _rents(12, 5_500_000, (1, 5)) + _rents(12, 7_000_000, (7, 11))
        sales = _sales(12, 10_000_000, (1, 5)) + _sales(12, 10_000_000, (7, 11))
        r = analyze(rents, sales, ASOF)
        self.assertLess(r.ratio_delta_pp, -2.0)
        self.assertIn("하락", r.trend_label)

    def test_conversion_rate(self):
        jeonse = _rents(20, 10_000_000, (1, 11))
        # 전세 849,000,000 / 보증금 400,000,000 → 갭 449,000,000
        # 월세 2,000,000 × 12 = 24,000,000 → 약 5.3%
        monthly = [RentRecord("C01", ASOF - timedelta(days=30 * (i % 10 + 1)),
                              84.9, 10, 400_000_000, 2_000_000)
                   for i in range(10)]
        r = analyze(jeonse + monthly, _sales(20, 14_000_000, (1, 11)), ASOF)
        self.assertAlmostEqual(r.conversion_rate_pct, 5.3, delta=0.3)

    def test_conversion_rate_skipped_when_deposit_above_jeonse(self):
        jeonse = _rents(20, 6_000_000, (1, 11))
        monthly = [RentRecord("C01", ASOF - timedelta(days=30), 84.9, 10,
                              900_000_000, 1_000_000) for _ in range(5)]
        r = analyze(jeonse + monthly, _sales(20, 10_000_000, (1, 11)), ASOF)
        self.assertIsNone(r.conversion_rate_pct)
        self.assertTrue(any("전세 수준 이상" in x for x in r.limitations))

    def test_coverage_of_subject_price(self):
        r = analyze(_rents(20, 7_000_000, (1, 11)),
                    _sales(20, 10_000_000, (1, 11)), ASOF)
        self.assertAlmostEqual(r.coverage_of(14_000_000), 50.0, delta=0.5)
        self.assertIsNone(r.coverage_of(0))


# ── 판정·파이프라인 통합 ─────────────────────────────────────────────────────

class TestVerdictAndPipeline(unittest.TestCase):
    def _run(self, **kw):
        comps = sd.build_comparables()
        return run(site=sd.build_site(), comps=comps,
                   txs=sd.build_transactions(comps),
                   sub_history=sd.build_subscription_history(),
                   supply_items=sd.build_supply(),
                   catalyst_plans_old=sd.build_catalysts(),
                   catalyst_plans_new=sd.build_catalysts(),
                   dataset_meta=sd.build_dataset_meta(),
                   incomes=sd.build_incomes(),
                   feedback=sd.build_feedback(),
                   listings=sd.build_listing_snapshots(),
                   asof=sd.ASOF, ledger=ForecastLedger(), **kw)

    def test_thin_buffer_escalates_negative_price_verdict(self):
        from report_system.pricing import Band, MarketPosition
        band = Band(level="타입", type_name="84A", floor_band=None,
                    q25=9_000_000, q50=10_000_000, q75=11_000_000, n=40,
                    rolled_up=False)
        pos = [MarketPosition(type_name="84A", subject_ppsm=12_000_000,
                              band=band, label="밴드 상단")]
        thin = analyze(_rents(20, 5_000_000, (1, 11)),
                       _sales(20, 10_000_000, (1, 11)), ASOF)
        v = price_verdict(pos, jeonse=thin)
        self.assertEqual(v.direction, "부정")
        self.assertEqual(v.strength, "강")
        self.assertTrue(any("하방 위험 강도 상향" in r for r in v.rationale))

    def test_verdict_unchanged_without_jeonse(self):
        res = self._run()
        v1 = next(v for v in res.inputs.verdicts if v.name.startswith("①"))
        self.assertFalse(any("전세" in r for r in v1.rationale))
        self.assertNotIn("## 4-2.", res.markdown)

    def test_pipeline_includes_jeonse_section(self):
        comps = sd.build_comparables()
        res = self._run(rents=sd.build_rents(comps))
        self.assertIn("전세 기반 하방 점검", res.markdown)
        v1 = next(v for v in res.inputs.verdicts if v.name.startswith("①"))
        self.assertTrue(any("전세가율" in r for r in v1.rationale))
        rows = {r.layer.split()[0]: r for r in res.inputs.coverage_rows}
        self.assertIn("전월세 연동됨", rows["L11"].note)

    def test_coverage_note_when_rent_missing(self):
        res = self._run()
        rows = {r.layer.split()[0]: r for r in res.inputs.coverage_rows}
        self.assertIn("전월세 미수집", rows["L11"].note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
