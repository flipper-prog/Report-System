"""판정 강건성 검사 테스트.

검사 도구가 판정 함수를 복제하면 검사는 무의미해진다. 따라서 여기서 확인할
것은 두 가지다 — (1) 교란이 실제로 운영 판정 함수를 다시 태우는가,
(2) 흔들 수 없는 가정을 조용히 빼지 않고 '검사 안 함'으로 남기는가.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.affordability import simulate
from report_system.catalyst import STAGE_FEASIBILITY, assess
from report_system.ledger import ForecastLedger
from report_system.models import MaturityStage, SupplyStage
from report_system.pipeline import run
from report_system.pricing import DEFAULT_COEF
from report_system.profiles import get as get_profile
from report_system.robustness import (MAX_LOG_CAP, Perturbation,
                                      RobustnessReport, STRICT_TURNOVER,
                                      TIGHT_EQUITY_MULTIPLE, _shift_coef,
                                      analyze)
from report_system.supply import STAGE_REALIZATION, probability_adjusted
from report_system.transactions import clean
from report_system.verdicts import (catalyst_verdict, demand_verdict,
                                    price_verdict, supply_verdict)


def _p(**kw) -> Perturbation:
    base = dict(verdict="① 현재 가격 위치", assumption="연식 조정계수",
                change="0.010 → 0.005", base_direction="긍정",
                new_direction="긍정", base_strength="중", new_strength="중")
    base.update(kw)
    return Perturbation(**base)


class TestPerturbation(unittest.TestCase):
    def test_flip_detected(self):
        c = _p(base_direction="긍정", new_direction="부정")
        self.assertTrue(c.flipped)
        self.assertIn("뒤집힘", c.result_label)
        self.assertIn("긍정 → 부정", c.result_label)

    def test_same_direction_is_not_flip(self):
        self.assertFalse(_p().flipped)
        self.assertEqual(_p().token, "방향 유지")

    def test_weakened_reported_separately(self):
        c = _p(base_strength="강", new_strength="약")
        self.assertFalse(c.flipped)
        self.assertTrue(c.weakened)
        self.assertIn("강도 하락", c.result_label)

    def test_strengthened_is_not_weakened(self):
        c = _p(base_strength="약", new_strength="강")
        self.assertFalse(c.weakened)
        self.assertEqual(c.token, "방향 유지")

    def test_flip_takes_precedence_over_strength(self):
        c = _p(base_direction="긍정", new_direction="중립",
               base_strength="강", new_strength="약")
        self.assertTrue(c.flipped)
        self.assertFalse(c.weakened)
        self.assertEqual(c.token, "뒤집힘")


class TestReportAggregation(unittest.TestCase):
    def test_empty_report_is_not_claimed_robust(self):
        r = RobustnessReport()
        self.assertEqual(r.label, "강건성 미검사")
        self.assertEqual(r.fragile, [])
        self.assertIn("미실행", r.summary())

    def test_fragile_dedup_preserves_order(self):
        r = RobustnessReport(checks=[
            _p(verdict="④", new_direction="부정"),
            _p(verdict="①", new_direction="부정"),
            _p(verdict="④", new_direction="중립"),
        ])
        self.assertEqual(r.fragile, ["④", "①"])

    def test_no_flip_labeled_robust(self):
        r = RobustnessReport(checks=[_p(), _p()])
        self.assertEqual(r.label, "가정 변화에 강건")
        self.assertIn("방향이 뒤집힌 판정은 없습니다", r.as_markdown())

    def test_fragile_markdown_carries_limitation_and_cause(self):
        r = RobustnessReport(checks=[_p(new_direction="부정")])
        md = r.as_markdown()
        self.assertIn("[LIMITATION]", md)
        self.assertIn("연식 조정계수", md)
        self.assertIn("| 뒤집힘 |", md)   # HTML 배지로 강조되는 단일 토큰

    def test_skipped_assumptions_are_listed_not_dropped(self):
        r = RobustnessReport(checks=[_p()],
                             skipped=[("촉매 실현 가능성", "평가 대상 없음")])
        md = r.as_markdown()
        self.assertIn("검사하지 않은 가정", md)
        self.assertIn("촉매 실현 가능성", md)
        self.assertIn("평가 대상 없음", md)

    def test_tested_verdicts_dedup(self):
        r = RobustnessReport(checks=[_p(verdict="①"), _p(verdict="①"),
                                     _p(verdict="②")])
        self.assertEqual(r.tested_verdicts, ["①", "②"])


class TestCoefShift(unittest.TestCase):
    """계수를 흔들 때 상한도 함께 움직여야 교란이 상한에 눌리지 않는다."""

    def test_age_cap_scales_with_rate(self):
        c = _shift_coef(DEFAULT_COEF, age_factor=3.0)
        self.assertAlmostEqual(c.age_per_year, DEFAULT_COEF.age_per_year * 3.0)
        self.assertGreater(c.age_cap, DEFAULT_COEF.age_cap)

    def test_age_cap_bounded(self):
        c = _shift_coef(DEFAULT_COEF, age_factor=100.0)
        self.assertLessEqual(c.age_cap, MAX_LOG_CAP)

    def test_time_delta_applied_with_cap(self):
        c = _shift_coef(DEFAULT_COEF, time_delta=0.02)
        self.assertAlmostEqual(c.time_per_year, DEFAULT_COEF.time_per_year + 0.02)
        self.assertGreater(c.time_cap, 0.0)
        self.assertLessEqual(c.time_cap, MAX_LOG_CAP)

    def test_source_marked_as_perturbed(self):
        self.assertIn("교란", _shift_coef(DEFAULT_COEF, age_factor=0.5).source)

    def test_zero_factor_keeps_original_cap(self):
        # 계수가 0이면 상한 재계산이 무의미하므로 원래 상한을 유지한다
        c = _shift_coef(DEFAULT_COEF, age_factor=0.0)
        self.assertEqual(c.age_cap, DEFAULT_COEF.age_cap)


class TestOverrideParameters(unittest.TestCase):
    """교란은 운영 함수의 파라미터로만 들어간다 (로직 복제 금지)."""

    def test_supply_realization_override_changes_result(self):
        items = sd.build_supply()
        base = probability_adjusted(items)
        low = probability_adjusted(items, realization={
            s: max(0.05, r - 0.2) for s, r in STAGE_REALIZATION.items()})
        self.assertLess(low.adjusted_units, base.adjusted_units)
        self.assertEqual(low.nominal_units, base.nominal_units)

    def test_supply_override_does_not_mutate_default(self):
        probability_adjusted(sd.build_supply(),
                             realization={s: 0.1 for s in SupplyStage})
        self.assertEqual(STAGE_REALIZATION[SupplyStage.PERMIT], 0.55)

    def test_catalyst_feasibility_override(self):
        plan = sd.build_catalysts()[0]
        base = assess(plan)
        lifted = assess(plan, feasibility={
            s: min(1.0, f + 0.1) for s, f in STAGE_FEASIBILITY.items()})
        self.assertGreater(lifted.feasibility, base.feasibility)
        self.assertEqual(STAGE_FEASIBILITY[MaturityStage.IDEA], 0.10)

    def test_equity_multiple_override_lowers_eligibility(self):
        t = sd.build_site().types[0]
        incomes = sd.build_incomes()
        base = simulate(t, incomes)
        lean = simulate(t, incomes, equity_multiple=TIGHT_EQUITY_MULTIPLE)
        self.assertLessEqual(lean.scenarios[1]["eligible_share"],
                             base.scenarios[1]["eligible_share"])


class TestAnalyze(unittest.TestCase):
    def setUp(self):
        self.comps = sd.build_comparables()
        self.txs = clean(sd.build_transactions(self.comps)).kept
        self.site = sd.build_site()
        self.profile = get_profile(self.site.product_type)
        self.supply = sd.build_supply()
        self.plans = sd.build_catalysts()
        self.incomes = sd.build_incomes()
        self.afford = [simulate(t, self.incomes) for t in self.site.types]
        from report_system.subscription import predict
        self.sub = predict(sd.build_subscription_history(), self.site.region,
                           0.0, 500)
        from report_system.liquidity import analyze as liq
        self.liq = liq(self.txs, self.comps, sd.ASOF)
        self.sa = probability_adjusted(self.supply)
        self.base = [
            price_verdict([]),
            demand_verdict(self.afford, self.sub),
            supply_verdict(self.sa, self.site.total_units, self.liq),
            catalyst_verdict([assess(p) for p in self.plans]),
        ]

    def _analyze(self, **kw):
        base = dict(base_verdicts=self.base, site=self.site, comps=self.comps,
                    txs=self.txs, asof=sd.ASOF, profile=self.profile,
                    coef=DEFAULT_COEF, incomes=self.incomes,
                    sub_forecast=self.sub, supply_items=self.supply,
                    catalyst_plans=self.plans, liquidity=self.liq)
        base.update(kw)
        return analyze(**base)

    def test_all_four_verdicts_are_tested(self):
        r = self._analyze()
        self.assertEqual(len(r.tested_verdicts), 4)
        self.assertEqual(r.skipped, [])

    def test_deterministic(self):
        a, b = self._analyze(), self._analyze()
        self.assertEqual([c.result_label for c in a.checks],
                         [c.result_label for c in b.checks])

    def test_no_transactions_skips_price_assumptions(self):
        r = self._analyze(txs=[])
        names = [n for n, _ in r.skipped]
        self.assertIn("품질조정 계수", names)
        self.assertIn("환금성 양호 기준", names)
        self.assertNotIn("① 현재 가격 위치", r.tested_verdicts)

    def test_no_incomes_skips_demand_assumptions(self):
        r = self._analyze(incomes=[])
        self.assertIn("금융 규제·자기자본 가정", [n for n, _ in r.skipped])
        self.assertNotIn("② 수요 지속성", r.tested_verdicts)

    def test_no_catalysts_skips_feasibility(self):
        r = self._analyze(catalyst_plans=[])
        self.assertIn("촉매 실현 가능성", [n for n, _ in r.skipped])

    def test_no_supply_still_tests_liquidity_threshold(self):
        r = self._analyze(supply_items=[])
        self.assertIn("단계별 공급 실현률", [n for n, _ in r.skipped])
        self.assertIn("환금성 양호 기준", [c.assumption for c in r.checks])

    def test_missing_verdict_is_not_invented(self):
        # 판정 자체가 없으면 그 축은 검사도 건너뛴다 (없는 기준선과 비교 금지)
        r = self._analyze(base_verdicts=[self.base[0]])
        self.assertEqual(r.tested_verdicts, ["① 현재 가격 위치"])

    def test_baseline_directions_are_recorded_verbatim(self):
        r = self._analyze()
        by_name = {v.name: v for v in self.base}
        for c in r.checks:
            self.assertEqual(c.base_direction, by_name[c.verdict].direction)
            self.assertEqual(c.base_strength, by_name[c.verdict].strength)

    def test_strict_turnover_is_stricter_than_default(self):
        self.assertGreater(STRICT_TURNOVER, self.liq.good_threshold)

    def test_change_text_is_human_readable(self):
        for c in self._analyze().checks:
            self.assertTrue(c.change.strip())
            self.assertTrue(c.assumption.strip())


class TestSalesPackCaution(unittest.TestCase):
    """분석이 스스로 흔들린다고 밝힌 결론을 상담 문장이 확언으로 쓰면 안 된다."""

    def _pack(self, robustness):
        from report_system.salespack import build
        comps = sd.build_comparables()
        txs = clean(sd.build_transactions(comps)).kept
        site = sd.build_site()
        from report_system.pricing import market_positions, quality_adjusted_bands
        from report_system.subscription import predict
        from report_system.liquidity import analyze as liq
        bands = quality_adjusted_bands(site, comps, txs, sd.ASOF)
        positions = market_positions(site, bands)
        incomes = sd.build_incomes()
        afford = [simulate(t, incomes) for t in site.types]
        sub = predict(sd.build_subscription_history(), site.region, 0.0, 500)
        sa = probability_adjusted(sd.build_supply())
        cards = [assess(p) for p in sd.build_catalysts()]
        verdicts = [price_verdict(positions),
                    demand_verdict(afford, sub),
                    supply_verdict(sa, site.total_units),
                    catalyst_verdict(cards)]
        return build(positions=positions, verdicts=verdicts, afford=afford,
                     sub_forecast=sub, supply=sa, site_units=site.total_units,
                     catalysts=cards, alerts=[], feedback=sd.build_feedback(),
                     liquidity=liq(txs, comps, sd.ASOF),
                     robustness=robustness)

    def test_no_caution_when_robust(self):
        pack = self._pack(RobustnessReport(checks=[_p()]))
        self.assertTrue(all(not a.caution for a in pack.answers))

    def test_caution_applied_to_dependent_axis(self):
        rb = RobustnessReport(checks=[
            _p(verdict="① 현재 가격 위치", new_direction="부정")])
        pack = self._pack(rb)
        q1 = next(a for a in pack.answers if a.code == "Q1")
        self.assertIn("연식 조정계수", q1.caution)
        self.assertIn("Q1", " ".join(pack.notes))
        self.assertIn("단정 금지", pack.as_markdown())

    def test_caution_does_not_leak_to_other_axes(self):
        rb = RobustnessReport(checks=[
            _p(verdict="④ 촉매·실행 가능성", assumption="촉매 실현 가능성",
               new_direction="중립")])
        pack = self._pack(rb)
        by = {a.code: a for a in pack.answers}
        self.assertTrue(by["Q4"].caution)
        self.assertFalse(by["Q1"].caution)
        self.assertFalse(by["Q2"].caution)

    def test_unanswerable_axis_is_not_cautioned(self):
        rb = RobustnessReport(checks=[
            _p(verdict="① 현재 가격 위치", new_direction="부정")])
        pack = self._pack(rb)
        for a in pack.answers:
            if not a.answerable:
                self.assertEqual(a.caution, "")

    def test_none_robustness_is_safe(self):
        pack = self._pack(None)
        self.assertTrue(all(not a.caution for a in pack.answers))


class TestPipelineIntegration(unittest.TestCase):
    def setUp(self):
        comps = sd.build_comparables()
        self.res = run(site=sd.build_site(), comps=comps,
                       txs=sd.build_transactions(comps),
                       sub_history=sd.build_subscription_history(),
                       supply_items=sd.build_supply(),
                       catalyst_plans_old=sd.build_catalysts(),
                       catalyst_plans_new=sd.build_catalysts(),
                       dataset_meta=sd.build_dataset_meta(),
                       incomes=sd.build_incomes(), feedback=sd.build_feedback(),
                       listings=sd.build_listing_snapshots(),
                       asof=sd.ASOF, ledger=ForecastLedger())

    def test_report_contains_section(self):
        self.assertIn("판정 강건성 검사", self.res.markdown)

    def test_robustness_attached_to_inputs(self):
        self.assertIsNotNone(self.res.inputs.robustness)
        self.assertTrue(self.res.inputs.robustness.checks)

    def test_fragile_verdicts_flagged_in_summary(self):
        rb = self.res.inputs.robustness
        if rb.fragile:
            self.assertIn("가정 의존 판정", self.res.markdown)

    def test_perturbation_does_not_alter_headline_verdicts(self):
        # 강건성 검사는 관측일 뿐 — 본 판정을 바꾸면 안 된다
        rb = self.res.inputs.robustness
        by_name = {v.name: v for v in self.res.inputs.verdicts}
        for c in rb.checks:
            self.assertEqual(c.base_direction, by_name[c.verdict].direction)


if __name__ == "__main__":
    unittest.main()
