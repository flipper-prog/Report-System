"""판매 논리 산출물 테스트 (제안서 제6장).

이 모듈의 핵심은 답하는 능력이 아니라 **답하지 않는 능력**이다. 근거가 없는
축에서 그럴듯한 문장이 생성되면 상담원이 근거 없는 말을 손에 쥐게 된다.
테스트의 절반은 그 경우를 확인한다.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.ledger import ForecastLedger
from report_system.models import AdGrade, FieldFeedback
from report_system.pipeline import run
from report_system.salespack import QUESTIONS, build


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


class TestFullPack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = _run(rents=sd.build_rents(sd.build_comparables()),
                       unsold=sd.build_unsold(), housing=sd.build_housing())
        cls.pack = cls.res.inputs.salespack

    def test_all_six_axes_present(self):
        codes = [a.code for a in self.pack.answers]
        self.assertEqual(codes, [q[0] for q in QUESTIONS])

    def test_every_answer_carries_evidence_and_counter(self):
        for a in self.pack.answers:
            if not a.answerable:
                continue
            self.assertTrue(a.evidence, f"{a.code} 근거 없음")
            self.assertTrue(a.counter, f"{a.code} 반대·한계 없음")

    def test_no_answer_is_unconditionally_advertisable_except_calculation(self):
        """계산으로 확정되는 축 외에는 조건부 이하 등급이어야 한다."""
        for a in self.pack.answers:
            if a.claim is None:
                continue
            if a.claim.ad_grade == AdGrade.ALLOWED:
                self.assertEqual(a.claim.grade.value, "CALCULATION",
                                 f"{a.code}: 계산 근거 없이 '사용 가능' 등급")

    def test_lint_applied_to_generated_sentences(self):
        self.assertIsNotNone(self.pack.lint)
        self.assertEqual(len(self.pack.lint.passed) + len(self.pack.lint.blocked),
                         sum(1 for a in self.pack.answers if a.claim))

    def test_markdown_contains_all_three_deliverables(self):
        md = self.pack.as_markdown()
        for section in ("메시지맵", "근거카드", "거절 대응 자료"):
            self.assertIn(section, md)

    def test_report_embeds_pack(self):
        self.assertIn("판매 논리 산출물", self.res.markdown)
        self.assertIn("Q1", self.res.markdown)

    def test_q6_refuses_manufactured_urgency(self):
        q6 = next(a for a in self.pack.answers if a.code == "Q6")
        self.assertIn("마감 임박이 아니라", q6.headline)
        self.assertTrue(any("긴급성" in c for c in q6.counter))


class TestRefusalToAnswer(unittest.TestCase):
    """근거가 없으면 문장을 만들지 않는다."""

    def test_q1_unanswerable_without_bands(self):
        pack = _run(comps={}).inputs.salespack
        q1 = next(a for a in pack.answers if a.code == "Q1")
        self.assertFalse(q1.answerable)
        self.assertIsNone(q1.claim)
        self.assertIn("비교 표본 부족", q1.unanswerable_reason)

    def test_q4_unanswerable_without_catalysts(self):
        pack = _run(catalyst_plans_old=[], catalyst_plans_new=[]).inputs.salespack
        q4 = next(a for a in pack.answers if a.code == "Q4")
        self.assertFalse(q4.answerable)
        self.assertIn("등록되지 않음", q4.unanswerable_reason)

    def test_q5_unanswerable_without_turnover(self):
        from report_system.models import Comparable
        comps = {k: Comparable(c.id, c.name, c.built_year, 0, c.brand_tier,
                               c.dist_m, c.region, c.is_presale_right)
                 for k, c in sd.build_comparables().items()}
        pack = _run(comps=comps).inputs.salespack
        q5 = next(a for a in pack.answers if a.code == "Q5")
        self.assertFalse(q5.answerable)
        self.assertIn("회전율 미산출", q5.unanswerable_reason)

    def test_unanswerable_axis_shown_not_hidden(self):
        pack = _run(catalyst_plans_old=[], catalyst_plans_new=[]).inputs.salespack
        md = pack.as_markdown()
        self.assertIn("답변 불가", md)
        self.assertIn("확인 후 회신", md)
        self.assertTrue(any("답변 불가 축" in n for n in pack.notes))

    def test_forbidden_stage_catalysts_do_not_become_talking_points(self):
        """모든 개발계획이 검토 단계면 언급 가능한 축으로 만들지 않는다."""
        from report_system.models import CatalystPlan, MaturityStage
        idea_only = [CatalystPlan("K-9", "검토중 노선", MaturityStage.IDEA,
                                  budget_total=0, budget_secured=0,
                                  dist_m=1500, time_saving_min=3)]
        pack = _run(catalyst_plans_old=idea_only,
                    catalyst_plans_new=idea_only).inputs.salespack
        q4 = next(a for a in pack.answers if a.code == "Q4")
        self.assertFalse(q4.answerable)
        self.assertIn("검토 단계", q4.unanswerable_reason)


class TestRebuttals(unittest.TestCase):
    def test_sorted_by_share_and_priority_flagged(self):
        pack = _run().inputs.salespack
        shares = [r.share for r in pack.rebuttals]
        self.assertEqual(shares, sorted(shares, reverse=True))
        self.assertTrue(pack.rebuttals[0].priority)
        self.assertAlmostEqual(sum(shares), 1.0, places=6)

    def test_no_rebuttals_without_feedback(self):
        pack = _run(feedback=FieldFeedback(total_consults=0,
                                           rejections={})).inputs.salespack
        self.assertEqual(pack.rebuttals, [])
        self.assertNotIn("거절 대응 자료", pack.as_markdown())

    def test_unknown_reason_flagged_for_definition(self):
        fb = FieldFeedback(total_consults=10, rejections={"신규사유": 10})
        pack = _run(feedback=fb).inputs.salespack
        self.assertEqual(len(pack.rebuttals), 1)
        self.assertIn("신규 정의 필요", pack.rebuttals[0].response)

    def test_response_degrades_when_its_axis_has_no_evidence(self):
        """대응 논리가 기대는 축의 근거가 없으면 사실 기반 대응 불가로 낮춘다."""
        fb = FieldFeedback(total_consults=10, rejections={"가격": 10})
        pack = _run(comps={}, feedback=fb).inputs.salespack
        self.assertIn("사실 기반 대응 불가", pack.rebuttals[0].response)
        self.assertEqual(pack.rebuttals[0].evidence, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
