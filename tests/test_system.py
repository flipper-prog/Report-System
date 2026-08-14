"""리포트 시스템 단위·통합 테스트 (stdlib unittest)."""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.affordability import annuity_monthly, max_loan_by_dsr, simulate
from report_system.claims import lint
from report_system.ledger import ForecastLedger
from report_system.models import (AdGrade, Claim, ClaimGrade, DatasetMeta,
                                  SubscriptionRecord, Transaction, TypeSpec)
from report_system.pipeline import FatalInputError, run
from report_system.pricing import MIN_SAMPLES_BAND, quality_adjusted_bands
from report_system.quality import grade
from report_system.subscription import predict
from report_system.supply import probability_adjusted
from report_system.transactions import clean
from report_system.validation import has_fatal, validate_site, validate_transactions


class TestValidation(unittest.TestCase):
    def test_unit_mismatch_is_fatal(self):
        site = sd.build_site()
        site.total_units += 5
        self.assertTrue(has_fatal(validate_site(site, sd.ASOF)))

    def test_future_leak_is_fatal(self):
        txs = [Transaction("C-OLD1", sd.ASOF + timedelta(days=1), 84.9, 5, 800_000_000)]
        self.assertTrue(has_fatal(validate_transactions(txs, sd.ASOF)))

    def test_clean_site_passes(self):
        self.assertFalse(has_fatal(validate_site(sd.build_site(), sd.ASOF)))


class TestCleaning(unittest.TestCase):
    def test_removes_polluted_samples(self):
        comps = sd.build_comparables()
        txs = sd.build_transactions(comps)
        cr = clean(txs)
        self.assertGreaterEqual(len(cr.removed["취소"]), 1)
        self.assertGreaterEqual(len(cr.removed["중복"]), 1)
        self.assertGreaterEqual(len(cr.removed["이상(고저가)"]), 1)
        self.assertGreaterEqual(len(cr.removed["특수 의심"]), 1)
        self.assertNotIn(True, [t.canceled for t in cr.kept])


class TestPricing(unittest.TestCase):
    def test_rollup_when_samples_scarce(self):
        site = sd.build_site()
        comps = sd.build_comparables()
        few = [Transaction("C-OLD1", sd.ASOF - timedelta(days=30 * i), 84.9, 10,
                           900_000_000) for i in range(1, MIN_SAMPLES_BAND - 2)]
        bands = quality_adjusted_bands(site, comps, few, sd.ASOF)
        t84 = [b for b in bands if b.type_name == "84A"]
        self.assertTrue(all(b.rolled_up for b in t84))

    def test_presale_priority_weighting(self):
        """분양권 거래는 분포 산출에서 가중되지만, 근거의 양은 부풀리지 않는다 (P1-1)."""
        site = sd.build_site()
        comps = sd.build_comparables()
        txs = [Transaction("C-PRS1", sd.ASOF - timedelta(days=30 * i), 84.9, 10,
                           900_000_000) for i in range(1, 5)]
        bands = quality_adjusted_bands(site, comps, txs, sd.ASOF)
        t84 = next(b for b in bands if b.type_name == "84A" and b.level == "타입")
        # 근거의 양은 실제 거래 건수로만 센다 — 가중은 분포에만 반영한다.
        # 종전에는 4건이 8건으로 세어져 최소 표본 게이트를 그대로 통과했다.
        self.assertEqual(t84.n, 4)
        self.assertEqual(t84.n_weighted, 8)   # 4건 × 가중 2 (분포 산출용)
        self.assertTrue(t84.rolled_up)
        self.assertIn("참고치", t84.note)
        self.assertIn("유효표본 8", t84.note)


class TestSupply(unittest.TestCase):
    def test_probability_adjustment_and_window(self):
        sa = probability_adjusted(sd.build_supply(), window_months=36)
        self.assertEqual(sa.nominal_units, 900 + 1200 + 2000)   # 장기(60개월)는 제외
        self.assertLess(sa.adjusted_units, sa.nominal_units)


class TestAffordability(unittest.TestCase):
    def test_monthly_increases_with_rate(self):
        self.assertLess(annuity_monthly(4e8, 0.03), annuity_monthly(4e8, 0.055))

    def test_dsr_loan_decreases_with_rate(self):
        self.assertGreater(max_loan_by_dsr(80_000_000, 0.4, 0.03),
                           max_loan_by_dsr(80_000_000, 0.4, 0.055))

    def test_eligible_share_monotonic_in_rate(self):
        t = TypeSpec("84A", 84.9, 100, 830_000_000, 24_000_000)
        res = simulate(t, sd.build_incomes())
        shares = [s["eligible_share"] for s in res.scenarios]
        self.assertGreaterEqual(shares[0], shares[-1])


class TestSubscription(unittest.TestCase):
    def test_interval_sane(self):
        fc = predict(sd.build_subscription_history(), "샘플권역", 2.0, 800)
        self.assertTrue(fc.ok)
        self.assertLessEqual(fc.lo, fc.mid)
        self.assertLessEqual(fc.mid, fc.hi)
        self.assertGreaterEqual(fc.n_cases, 5)

    def test_refuses_without_cases(self):
        fc = predict([], "샘플권역", 2.0, 800)
        self.assertFalse(fc.ok)
        self.assertIn("정성 판정", fc.reason)

    def test_price_gap_direction(self):
        """가격 갭이 클수록(비쌀수록) 전망 중위 경쟁률은 낮아야 한다."""
        hist = sd.build_subscription_history()
        cheap = predict(hist, "샘플권역", -6.0, 800)
        rich = predict(hist, "샘플권역", +10.0, 800)
        self.assertTrue(cheap.ok and rich.ok)
        self.assertGreater(cheap.mid, rich.mid)


class TestLedger(unittest.TestCase):
    def test_seal_resolve_coverage(self):
        led = ForecastLedger()
        fid = led.seal("subscription", "S1", 2.0, 8.0, 0.6, "m0", "2026-07-25", {})
        self.assertTrue(led.verify_seal(fid))
        self.assertTrue(led.resolve(fid, 5.0))
        fid2 = led.seal("subscription", "S2", 2.0, 4.0, 0.6, "m0", "2026-07-25", {})
        self.assertFalse(led.resolve(fid2, 9.0))
        rep = led.coverage("subscription", 0.6)
        self.assertEqual((rep.resolved, rep.hits), (2, 1))

    def test_immutability_enforced(self):
        import sqlite3
        led = ForecastLedger()
        fid = led.seal("price", "S1", 1.0, 2.0, 0.6, "m0", "2026-07-25", {})
        with self.assertRaises(sqlite3.IntegrityError):
            led.conn.execute("UPDATE forecasts SET lo=0 WHERE id=?", (fid,))
        with self.assertRaises(sqlite3.IntegrityError):
            led.conn.execute("DELETE FROM forecasts WHERE id=?", (fid,))


class TestClaims(unittest.TestCase):
    def test_banned_expression_blocked(self):
        res = lint([Claim("무조건 오르는 단지", ClaimGrade.INFERENCE, AdGrade.CONDITIONAL)])
        self.assertFalse(res.ok)

    def test_forecast_cannot_be_allowed(self):
        res = lint([Claim("경쟁률 15대 1 전망 구간", ClaimGrade.FORECAST, AdGrade.ALLOWED)])
        self.assertFalse(res.ok)

    def test_valid_calculation_passes(self):
        res = lint([Claim("총취득원가는 8.8억원입니다", ClaimGrade.CALCULATION,
                          AdGrade.ALLOWED, evidence=["calc:1"])])
        self.assertTrue(res.ok)


class TestQuality(unittest.TestCase):
    def test_license_zero_is_grade_d(self):
        m = DatasetMeta("L6 유동", 25, 25, 20, 15, 0)
        self.assertEqual(grade(m).value, "D")


class TestPipeline(unittest.TestCase):
    def _run(self):
        comps = sd.build_comparables()
        return run(
            site=sd.build_site(), comps=comps,
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

    def test_end_to_end(self):
        result = self._run()
        md = result.markdown
        for section in ["결론 요약", "데이터 커버리지", "거래 데이터 정제",
                        "품질조정 가격 밴드", "실부담 시뮬레이션", "청약 수요 전망",
                        "확률조정 공급", "촉매카드", "표현 린트"]:
            self.assertIn(section, md)
        # 검토 단계 호재의 '신설 확정' 문장은 린트에 차단되어야 한다
        self.assertFalse(result.inputs.lint.ok is True and
                         not result.inputs.lint.blocked)
        self.assertTrue(any("신설 확정" in c.text for c, _ in result.inputs.lint.blocked))
        # 청약 전망은 봉인 ID를 가진다
        self.assertTrue(result.forecast_id)

    def test_fatal_input_stops_pipeline(self):
        comps = sd.build_comparables()
        site = sd.build_site()
        site.total_units += 1
        with self.assertRaises(FatalInputError):
            run(site=site, comps=comps,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
