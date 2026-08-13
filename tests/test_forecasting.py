"""시계열·시나리오·백테스트·모델카드·커버리지표 테스트."""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import coverage_table as ct
from report_system import sample_data as sd
from report_system.backtest import (backtest_price_bands,
                                    backtest_subscription, quarterly_cutoffs)
from report_system.catalyst import assess
from report_system.ledger import ForecastLedger
from report_system.models import (CatalystPlan, MaturityStage,
                                  SubscriptionRecord, Transaction)
from report_system.modelcard import detect_drift, price_band_card
from report_system.scenarios import build as build_scen
from report_system.timeseries import monthly_trend


def _linear_txs(start: date, months: int, base: float, growth_per_month: float,
                per_month: int = 6) -> list[Transaction]:
    out = []
    for i in range(months):
        d = date(start.year + (start.month - 1 + i) // 12,
                 (start.month - 1 + i) % 12 + 1, 15)
        ppsm = base + growth_per_month * i
        for j in range(per_month):
            out.append(Transaction("C-OLD1", d, 84.0, 10, int(ppsm * 84.0 * (1 + 0.002 * j))))
    return out


class TestTimeseries(unittest.TestCase):
    def test_rising_trend_detected(self):
        tr = monthly_trend(_linear_txs(date(2024, 1, 1), 24, 10_000_000, 50_000))
        self.assertEqual(tr.n_months, 24)
        self.assertGreater(tr.slope_pct_per_year, 0)

    def test_regime_shift_flag(self):
        up = _linear_txs(date(2023, 1, 1), 18, 10_000_000, 60_000)
        down = _linear_txs(date(2024, 7, 1), 8, 11_000_000, -90_000)
        tr = monthly_trend(up + down)
        self.assertTrue(tr.regime_shift)

    def test_empty_input_is_safe(self):
        tr = monthly_trend([])
        self.assertEqual((tr.n_months, tr.slope_pct_per_year), (0, 0.0))


class TestScenarios(unittest.TestCase):
    def _cards(self, stage=MaturityStage.DESIGN):
        return [assess(CatalystPlan("K", "n", stage, 1_000_000_000_000,
                                    250_000_000_000, 700, 12))]

    def test_ordering_low_base_high(self):
        s = build_scen(10_000_000, trend_pct_year=3.0, supply_ratio=2.0,
                       cards=self._cards())
        self.assertLess(s.low.annual_pct, s.base.annual_pct)
        self.assertLess(s.base.annual_pct, s.high.annual_pct)
        self.assertLess(s.low.price_ppsm, s.high.price_ppsm)

    def test_supply_burden_lowers_all_legs(self):
        light = build_scen(10_000_000, 3.0, 0.5, self._cards())
        heavy = build_scen(10_000_000, 3.0, 8.0, self._cards())
        self.assertLess(heavy.base.annual_pct, light.base.annual_pct)

    def test_review_stage_catalyst_contributes_less(self):
        weak = build_scen(10_000_000, 3.0, 2.0, self._cards(MaturityStage.IDEA))
        strong = build_scen(10_000_000, 3.0, 2.0, self._cards(MaturityStage.OPEN))
        self.assertLess(weak.high.annual_pct, strong.high.annual_pct)

    def test_limitation_always_present(self):
        s = build_scen(10_000_000, 1.0, 1.0, [])
        self.assertTrue(any("LIMITATION" in x for x in s.limitations))


class TestBacktest(unittest.TestCase):
    def test_price_band_coverage_near_nominal(self):
        site = sd.build_site()
        comps = sd.build_comparables()
        txs = sd.build_transactions(comps)
        cuts = quarterly_cutoffs(date(2024, 1, 1), sd.ASOF)
        rep = backtest_price_bands(site, comps, txs, cuts)
        self.assertGreater(rep.n, 20)
        cov = rep.coverage or 0
        # 명목 50% 대비 ±25%p 이내면 방법론적으로 정합
        self.assertGreater(cov, 0.25)
        self.assertLess(cov, 0.75)

    def test_subscription_backtest_runs_and_is_time_split(self):
        hist = sd.build_subscription_history()
        rep = backtest_subscription(hist, min_train=6)
        self.assertGreaterEqual(rep.n, 1)
        for f in rep.folds:
            self.assertLessEqual(f.lo, f.hi)

    def test_verdict_thresholds(self):
        rep = backtest_price_bands(sd.build_site(), sd.build_comparables(),
                                   sd.build_transactions(sd.build_comparables()),
                                   quarterly_cutoffs(date(2024, 1, 1), sd.ASOF))
        self.assertIn("적중률", rep.verdict())

    def test_no_future_leak_in_folds(self):
        """구간은 cutoff 이전 데이터로만 만들어져야 한다."""
        site, comps = sd.build_site(), sd.build_comparables()
        txs = sd.build_transactions(comps)
        cuts = quarterly_cutoffs(date(2024, 1, 1), sd.ASOF)
        rep = backtest_price_bands(site, comps, txs, cuts, horizon_months=6)
        for f in rep.folds:
            self.assertIn(f.cutoff, cuts)


class TestDriftAndCards(unittest.TestCase):
    def test_drift_report_shape(self):
        site, comps = sd.build_site(), sd.build_comparables()
        rep = backtest_price_bands(site, comps, sd.build_transactions(comps),
                                   quarterly_cutoffs(date(2023, 7, 1), sd.ASOF))
        d = detect_drift(rep)
        self.assertTrue(d.verdict)
        self.assertIn("적중률", d.as_markdown() + d.verdict)

    def test_model_card_contains_limits(self):
        card = price_band_card(100, "2024-01-01 ~ 2026-07-25", None)
        md = card.as_markdown()
        self.assertIn("알려진 한계", md)
        self.assertIn("qab-0.1", md)


class TestCoverageTable(unittest.TestCase):
    def test_reflects_actual_collection(self):
        rows = ct.build(tx_count=120, sub_count=0, supply_items=2,
                        catalyst_items=1, income_model=True)
        by = {r.layer.split()[0]: r for r in rows}
        self.assertEqual(by["L11"].coverage, ct.Coverage.AVAILABLE)
        self.assertEqual(by["L12"].coverage, ct.Coverage.MISSING)   # 수집 0건
        self.assertEqual(by["L5"].coverage, ct.Coverage.SUBSTITUTE)
        self.assertEqual(by["L6"].coverage, ct.Coverage.MISSING)    # 민간 라이선스
        self.assertEqual(len(rows), 15)

    def test_markdown_has_summary(self):
        rows = ct.build(tx_count=10, sub_count=10, supply_items=1,
                        catalyst_items=1, income_model=True)
        md = ct.as_markdown(rows)
        self.assertIn("레이어 15개", md)


class TestScenarioSealing(unittest.TestCase):
    def test_scenario_sealed_in_pipeline(self):
        from report_system.pipeline import run
        comps = sd.build_comparables()
        led = ForecastLedger()
        result = run(site=sd.build_site(), comps=comps,
                     txs=sd.build_transactions(comps),
                     sub_history=sd.build_subscription_history(),
                     supply_items=sd.build_supply(),
                     catalyst_plans_old=sd.build_catalysts(),
                     catalyst_plans_new=sd.build_catalysts(),
                     dataset_meta=sd.build_dataset_meta(),
                     incomes=sd.build_incomes(),
                     feedback=sd.build_feedback(),
                     listings=sd.build_listing_snapshots(),
                     asof=sd.ASOF, ledger=led)
        self.assertTrue(result.inputs.scenario_id)
        self.assertTrue(led.verify_seal(result.inputs.scenario_id))
        # 시나리오는 확률 구간이 아니므로 청약 적중률 집계에 섞이지 않는다
        self.assertEqual(led.coverage("subscription", 0.6).resolved, 0)
        self.assertIn("조건부 가격 시나리오", result.markdown)
        self.assertIn("모델 카드", result.markdown)
        self.assertIn("커버리지표", result.markdown)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestLedgerAudit(unittest.TestCase):
    """'봉인된다'는 주장이 실제로 성립하는지 감사한다."""

    def _ledger(self, n: int = 3):
        from report_system.ledger import ForecastLedger
        led = ForecastLedger()
        ids = [led.seal("subscription", f"S{i}", 4.0 + i, 9.0 + i, 0.6,
                        "m-1", "2026-07-25", {"i": i}) for i in range(n)]
        return led, ids

    def test_all_seals_intact_and_triggers_live(self):
        from report_system.ledger import audit
        led, ids = self._ledger()
        led.resolve(ids[0], 6.0)
        rep = audit(led)
        self.assertEqual(rep.total, 3)
        self.assertEqual(rep.tampered, [])
        self.assertIn("forecasts_no_update", rep.triggers)
        self.assertIn("forecasts_no_delete", rep.triggers)
        self.assertTrue(rep.trigger_test.startswith("차단"))
        self.assertTrue(rep.ok)

    def test_resolution_state_recorded(self):
        from report_system.ledger import audit
        led, ids = self._ledger()
        led.resolve(ids[1], 100.0)          # 구간 밖
        rows = {r.forecast_id: r for r in audit(led).rows}
        self.assertTrue(rows[ids[1]].resolved)
        self.assertFalse(rows[ids[1]].hit)
        self.assertFalse(rows[ids[0]].resolved)
        self.assertIsNone(rows[ids[0]].hit)

    def test_tampered_payload_is_detected(self):
        """트리거를 우회해 본문만 바꿔치기해도 해시 재계산에서 걸린다."""
        from report_system.ledger import audit
        led, ids = self._ledger(1)
        led.conn.execute("DROP TRIGGER forecasts_no_update")
        led.conn.execute("UPDATE forecasts SET payload='{\"조작\":1}' WHERE id=?",
                         (ids[0],))
        led.conn.commit()
        rep = audit(led)
        self.assertEqual(len(rep.tampered), 1)
        self.assertEqual(rep.tampered[0].forecast_id, ids[0])
        self.assertFalse(rep.ok)
        self.assertIn("변조 의심", rep.as_markdown())

    def test_missing_trigger_makes_audit_fail(self):
        from report_system.ledger import audit
        led, _ = self._ledger(1)
        led.conn.execute("DROP TRIGGER forecasts_no_update")
        led.conn.commit()
        rep = audit(led)
        self.assertEqual(rep.tampered, [])          # 본문은 멀쩡
        self.assertIn("통과됨", rep.trigger_test)    # 그러나 불변 보장이 깨졌다
        self.assertFalse(rep.ok)

    def test_empty_ledger_is_reported_not_crashed(self):
        from report_system.ledger import ForecastLedger, audit
        rep = audit(ForecastLedger())
        self.assertEqual(rep.total, 0)
        self.assertIn("시도할 대상 없음", rep.trigger_test)


class TestReissue(unittest.TestCase):
    """동일 분석 재실행 — 운영에서 늘 일어난다."""

    def _seal(self, led, **kw):
        args = dict(kind="subscription", target="S1", lo=5.0, hi=9.0,
                    confidence=0.6, model_version="m-1",
                    data_asof="2026-07-25", payload={"a": 1})
        args.update(kw)
        return led.seal(**args)

    def test_identical_reissue_returns_same_id_without_error(self):
        from report_system.ledger import ForecastLedger
        led = ForecastLedger()
        a = self._seal(led)
        b = self._seal(led)          # 같은 초에 같은 내용 → 같은 ID
        self.assertEqual(a, b)
        self.assertEqual(len(led.history("subscription")), 1)

    def test_reissue_does_not_disturb_recorded_outcome(self):
        """실적이 이미 대조된 봉인을 재발행해도 기록이 흐트러지지 않는다."""
        from report_system.ledger import ForecastLedger, audit
        led = ForecastLedger()
        fid = self._seal(led)
        led.resolve(fid, 7.0)
        self._seal(led)
        rows = audit(led).rows
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].resolved)
        self.assertTrue(rows[0].hit)
        self.assertTrue(rows[0].intact)

    def test_different_content_still_creates_new_seal(self):
        from report_system.ledger import ForecastLedger
        led = ForecastLedger()
        a = self._seal(led)
        b = self._seal(led, lo=6.0)          # 구간이 달라지면 다른 봉인
        self.assertNotEqual(a, b)
        self.assertEqual(len(led.history("subscription")), 2)
