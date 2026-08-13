"""퍼널 병목 진단 테스트 (제안서 10.5).

핵심 규율 두 가지를 확인한다.
  1) 기준선 없이는 병목을 판정하지 않는다 — 전환율 20%가 좋은지 나쁜지는
     기준선 없이 말할 수 없다.
  2) 방문→계약 병목은 광고로 해결되지 않는다 — 조건 문제로 분류되어야 한다.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.funnel import (CONTACT_DELAY_ALERT, MIN_DENOMINATOR,
                                  FunnelSnapshot, diagnose)
from report_system.ledger import ForecastLedger
from report_system.models import FieldFeedback
from report_system.pipeline import run

BM = {"S1": 0.22, "S2": 0.80, "S3": 0.45, "S4": 0.12}


def _snap(**kw):
    base = dict(clicks=4200, leads=980, consulted=760, visited=340,
                contracted=44, spend=180_000_000)
    base.update(kw)
    return FunnelSnapshot(**base)


class TestNoBenchmarkNoVerdict(unittest.TestCase):
    def test_without_benchmarks_no_bottleneck_declared(self):
        d = diagnose(_snap())
        self.assertIsNone(d.bottleneck)
        self.assertIn("기준선 미설정", d.verdict)
        self.assertIn("기준선 없이 말할 수 없습니다", d.verdict)

    def test_rates_still_reported_without_benchmarks(self):
        d = diagnose(_snap())
        self.assertTrue(all(s.measurable for s in d.stages))
        self.assertIn("23.3%", d.as_markdown())

    def test_limitation_explains_how_to_enable(self):
        d = diagnose(_snap())
        self.assertTrue(any("기준선" in x and "입력" in x for x in d.limitations))


class TestBottleneckSelection(unittest.TestCase):
    def test_picks_largest_relative_shortfall(self):
        d = diagnose(_snap(visited=210), BM)          # S3 크게 미달
        self.assertIsNotNone(d.bottleneck)
        self.assertEqual(d.bottleneck.code, "S3")
        self.assertIn("가격 저항", d.cause)

    def test_no_shortfall_no_bottleneck(self):
        good = _snap(clicks=4000, leads=1200, consulted=1100, visited=600,
                     contracted=90)
        d = diagnose(good, BM)
        self.assertIsNone(d.bottleneck)
        self.assertIn("특정 병목이 지목되지 않습니다", d.verdict)

    def test_small_denominator_is_withheld_not_guessed(self):
        d = diagnose(_snap(visited=MIN_DENOMINATOR - 1, contracted=2), BM)
        s4 = next(s for s in d.stages if s.code == "S4")
        self.assertFalse(s4.measurable)
        self.assertIn("판정 유보", s4.note)
        self.assertNotEqual(d.bottleneck, s4)

    def test_zero_denominator_handled(self):
        d = diagnose(FunnelSnapshot(), BM)
        self.assertTrue(all(not s.measurable for s in d.stages))
        self.assertIn("측정 가능한 단계 없음", d.verdict)

    def test_multiple_shortfalls_noted(self):
        d = diagnose(_snap(visited=210, contracted=15), BM)
        self.assertTrue(any("미달 단계가 복수" in x for x in d.limitations))


class TestConditionVsOperation(unittest.TestCase):
    """제안서 3.5 — 운영 문제와 조건 문제의 구분이 실제로 작동하는 지점."""

    def test_visit_to_contract_bottleneck_is_condition_issue(self):
        d = diagnose(_snap(visited=380, contracted=12), BM)
        self.assertEqual(d.bottleneck.code, "S4")
        self.assertTrue(d.is_condition_issue)
        md = d.as_markdown()
        self.assertIn("조건 문제", md)
        self.assertIn("광고 운영으로 해결되지 않습니다", md)
        self.assertIn("시행사 조건 검토 안건", md)

    def test_upstream_bottleneck_is_operational(self):
        d = diagnose(_snap(leads=400), BM)
        self.assertEqual(d.bottleneck.code, "S1")
        self.assertFalse(d.is_condition_issue)
        self.assertIn("운영 문제", d.as_markdown())


class TestCorroboration(unittest.TestCase):
    def test_price_rejections_support_downstream_bottleneck(self):
        fb = FieldFeedback(total_consults=100,
                           rejections={"가격": 50, "대출": 10, "교통": 5})
        d = diagnose(_snap(visited=210), BM, feedback=fb, verdicts=[])
        self.assertTrue(any("가격 비중" in c for c in d.corroboration))

    def test_conflict_with_price_verdict_is_surfaced(self):
        """가격 거절이 많은데 가격 판정이 긍정이면 그 어긋남을 드러낸다."""
        from report_system.verdicts import Verdict
        fb = FieldFeedback(total_consults=100, rejections={"가격": 60})
        v1 = Verdict("① 현재 가격 위치", "긍정", "중", "높음", "최초", [])
        d = diagnose(_snap(visited=210), BM, feedback=fb, verdicts=[v1])
        self.assertTrue(any("가격 판정은 '긍정'" in c for c in d.corroboration))

    def test_no_corroboration_without_feedback(self):
        d = diagnose(_snap(visited=210), BM, feedback=None)
        self.assertEqual(d.corroboration, [])


class TestOperationalFlags(unittest.TestCase):
    def test_contact_delay_classified_as_operations_not_ads(self):
        d = diagnose(_snap(contact_delayed=300), BM, contact_sla_hours=2)
        self.assertTrue(any("광고가 아닌 운영 병목" in f
                            for f in d.operational_flags))
        self.assertIn("2시간", " ".join(d.operational_flags))

    def test_delay_below_threshold_not_flagged(self):
        few = int(980 * CONTACT_DELAY_ALERT) - 1
        d = diagnose(_snap(contact_delayed=few), BM)
        self.assertEqual(d.operational_flags, [])

    def test_followup_volume_without_visits_is_flagged(self):
        d = diagnose(_snap(visited=180, followups_sent=3000), BM)
        self.assertTrue(any("발송량 증가로 해결되지 않음" in f
                            for f in d.operational_flags))

    def test_cost_per_contract_stated_alongside_lead_cost(self):
        d = diagnose(_snap(), BM)
        self.assertTrue(any("계약당 비용" in x for x in d.limitations))


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

    def test_section_appears_with_funnel(self):
        res = self._run(funnel=sd.build_funnel(),
                        funnel_benchmarks=sd.build_funnel_benchmarks())
        self.assertIn("퍼널 병목 진단", res.markdown)
        self.assertIn("←병목", res.markdown)

    def test_absent_without_funnel(self):
        self.assertIsNone(self._run().inputs.funnel)
        self.assertNotIn("퍼널 병목 진단", self._run().markdown)

    def test_benchmarkless_run_states_withheld_verdict(self):
        res = self._run(funnel=sd.build_funnel())
        self.assertIn("기준선 미설정", res.markdown)
        self.assertIsNone(res.inputs.funnel.bottleneck)
