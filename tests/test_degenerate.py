"""빈약한 입력 방어 테스트.

이 시스템의 핵심 규칙은 "표본이 지지하지 않는 수치는 내지 않는다"이다. 규칙이
지켜지는지는 정상 입력이 아니라 **빈약한 입력**에서 드러난다. 모르는 것을 0으로
적거나, 근거 없이 계산을 밀어붙이거나, 스택트레이스로 죽는 경우를 모두 막는다.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.affordability import MIN_INCOME_SAMPLES, simulate
from report_system.ledger import ForecastLedger
from report_system.models import TypeSpec
from report_system.pipeline import FatalInputError, run


def _run(**kw):
    comps = kw.pop("comps", sd.build_comparables())
    base = dict(site=sd.build_site(), comps=comps,
                txs=sd.build_transactions(comps),
                sub_history=sd.build_subscription_history(),
                supply_items=sd.build_supply(),
                catalyst_plans_old=sd.build_catalysts(),
                catalyst_plans_new=sd.build_catalysts(),
                dataset_meta=sd.build_dataset_meta(),
                incomes=sd.build_incomes(), feedback=sd.build_feedback(),
                listings=sd.build_listing_snapshots(),
                asof=sd.ASOF, ledger=ForecastLedger())
    base.update(kw)
    return run(**base)


class TestAffordabilityUnknown(unittest.TestCase):
    """모르는 것을 0%로 적지 않는다."""

    def _type(self):
        return TypeSpec("84A", 84.9, 100, 800_000_000, 0, (1, 25))

    def test_empty_income_sample_yields_none_not_zero(self):
        r = simulate(self._type(), [])
        self.assertEqual(r.n_incomes, 0)
        self.assertFalse(r.share_available)
        self.assertTrue(all(s["eligible_share"] is None for s in r.scenarios))
        self.assertIn("미산출", r.note)

    def test_below_minimum_sample_yields_none(self):
        r = simulate(self._type(), [60_000_000] * (MIN_INCOME_SAMPLES - 1))
        self.assertFalse(r.share_available)
        self.assertIsNone(r.scenarios[0]["eligible_share"])

    def test_at_minimum_sample_yields_number(self):
        r = simulate(self._type(), [60_000_000] * MIN_INCOME_SAMPLES)
        self.assertTrue(r.share_available)
        self.assertIsNotNone(r.scenarios[0]["eligible_share"])
        self.assertEqual(r.note, "")

    def test_report_prints_unavailable_not_zero_percent(self):
        res = _run(incomes=[])
        rows = [l for l in res.markdown.split("\n")
                if l.startswith("| 59A | 3.0%")]
        self.assertTrue(rows)
        self.assertIn("미산출", rows[0])
        self.assertNotIn("| 0% |", rows[0])

    def test_demand_verdict_does_not_score_unknown_as_zero(self):
        known = _run()
        unknown = _run(incomes=[])
        v_known = next(v for v in known.inputs.verdicts if v.name.startswith("②"))
        v_unknown = next(v for v in unknown.inputs.verdicts if v.name.startswith("②"))
        self.assertTrue(any("구매 가능 가구 비율 평균" in r for r in v_known.rationale))
        self.assertTrue(any("미산출" in r for r in v_unknown.rationale))
        # 0% 를 점수로 넣었다면 방향이 나빠졌을 것이다 — 그러지 않아야 한다
        self.assertNotEqual(v_unknown.direction, "부정")

    def test_evidence_registers_unknown_as_missing(self):
        res = _run(incomes=[])
        ev = next(e for e in res.inputs.evidence.items if "구매 가능" in e.metric)
        self.assertEqual(ev.value, "미산출")


class TestFatalStop(unittest.TestCase):
    """진행할 수 없으면 스택트레이스가 아니라 '분석 불가'로 멈춘다."""

    def test_zero_usable_transactions_stops_cleanly(self):
        with self.assertRaises(FatalInputError) as ctx:
            _run(txs=[])
        msg = str(ctx.exception)
        self.assertIn("비교 거래 0건", msg)
        self.assertIn("비교단지 설정", msg)

    def test_all_transactions_filtered_out_stops_cleanly(self):
        """입력은 있으나 정제 후 남지 않는 경우도 같은 경로로 멈춘다."""
        txs = [t for t in sd.build_transactions(sd.build_comparables())][:1]
        txs[0].canceled = True
        with self.assertRaises(FatalInputError) as ctx:
            _run(txs=txs)
        self.assertIn("정제 제외", str(ctx.exception))


class TestSparseButUsable(unittest.TestCase):
    """진행은 가능하되 근거가 얇은 입력 — 죽지 않고 한계를 밝힌다."""

    def test_no_subscription_history(self):
        res = _run(sub_history=[])
        self.assertFalse(res.inputs.sub_forecast.ok)
        self.assertEqual(res.forecast_id, "")
        self.assertIn("결론 요약", res.markdown)

    def test_no_supply_items(self):
        res = _run(supply_items=[])
        self.assertIn("확률조정 공급", res.markdown)

    def test_no_catalysts(self):
        res = _run(catalyst_plans_old=[], catalyst_plans_new=[])
        v4 = next(v for v in res.inputs.verdicts if v.name.startswith("④"))
        self.assertIn("평가 대상", " ".join(v4.rationale))

    def test_no_comparable_metadata(self):
        """비교단지 메타가 없으면 밴드가 서지 않지만 리포트는 나온다."""
        res = _run(comps={})
        self.assertEqual(res.inputs.bands, [])
        v1 = next(v for v in res.inputs.verdicts if v.name.startswith("①"))
        self.assertIn("판정 유보", " ".join(v1.rationale))
        self.assertIsNone(res.inputs.price_decision.recommended)


if __name__ == "__main__":
    unittest.main(verbosity=2)
