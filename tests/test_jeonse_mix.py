"""전세가율의 표본 구성 편의 테스트.

전세와 매매를 각각 풀링해 중위끼리 나누면 **두 표본의 구성 차이가 그대로
비율에 들어간다**. 전세는 소형·저가 단지에서, 매매는 대형·고가 단지에서 더
많이 나오는 것이 흔한 패턴이고 전세가율은 소형일수록 높으므로, 풀링 비율은
하방 완충을 실제보다 두텁게 보고한다 — 나쁜 소식을 지우는 방향의 편의다.

여기서 확인할 것: 같은 단지·같은 면적대끼리 짝지어 내는가, 그리고 짝지을 수
없을 때 그 사실과 구성 차이를 밝히는가.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system.jeonse import (AREA_BUCKET_M2, MIN_STRATUM, RATIO_STRONG,
                                  RATIO_WEAK, _weighted_median, analyze)
from report_system.models import RentRecord, Transaction

ASOF = date(2026, 7, 25)
SALE_PPSM = 10_000_000.0


def _mix(spec, months: int = 24):
    """spec: [(complex_id, area, 전세가율, 전세 월건수, 매매 월건수)]"""
    rents: list[RentRecord] = []
    sales: list[Transaction] = []
    for i in range(months):
        d = ASOF - timedelta(days=15 * i)
        for cid, area, ratio, nj, ns in spec:
            for _ in range(nj):
                rents.append(RentRecord(cid, d, area, 10,
                                        int(area * SALE_PPSM * ratio)))
            for _ in range(ns):
                sales.append(Transaction(cid, d, area, 10,
                                         int(area * SALE_PPSM)))
    return rents, sales


class TestMixBias(unittest.TestCase):
    #: 소형 단지는 전세가율 75%, 대형 단지는 55%.
    #: 전세 표본은 소형에, 매매 표본은 대형에 쏠려 있다.
    SKEWED = [("A", 59.0, 0.75, 5, 1), ("B", 84.0, 0.55, 1, 5)]

    def test_pooling_would_report_the_small_unit_ratio(self):
        """풀링이면 전세 표본이 몰린 소형 단지의 비율이 그대로 나온다."""
        from statistics import median
        rents, sales = _mix(self.SKEWED)
        pooled = (median([r.deposit_ppsm for r in rents])
                  / median([t.price / t.area_m2 for t in sales]) * 100)
        self.assertAlmostEqual(pooled, 75.0, places=1)
        self.assertGreaterEqual(pooled, RATIO_STRONG)   # '하방 지지 두터움'

    def test_paired_ratio_is_not_the_pooled_ratio(self):
        rents, sales = _mix(self.SKEWED)
        r = analyze(rents, sales, ASOF)
        self.assertTrue(r.paired)
        self.assertLess(r.ratio_pct, 75.0)

    def test_strata_recover_each_complex_ratio(self):
        rents, sales = _mix(self.SKEWED)
        r = analyze(rents, sales, ASOF)
        by = {s.complex_id: s.ratio_pct for s in r.strata}
        self.assertAlmostEqual(by["A"], 75.0, places=1)
        self.assertAlmostEqual(by["B"], 55.0, places=1)

    def test_headline_stays_within_stratum_range(self):
        rents, sales = _mix(self.SKEWED)
        r = analyze(rents, sales, ASOF)
        rs = [s.ratio_pct for s in r.strata]
        self.assertGreaterEqual(r.ratio_pct, min(rs))
        self.assertLessEqual(r.ratio_pct, max(rs))

    def test_stratum_labels_name_complex_and_area(self):
        rents, sales = _mix(self.SKEWED)
        for s in analyze(rents, sales, ASOF).strata:
            self.assertIn("㎡", s.label)
            self.assertIn(s.complex_id, s.label)


class TestSubjectWeighting(unittest.TestCase):
    """지표가 답할 질문은 '이 현장'의 하방이다 — 현장 타입 구성으로 가중한다."""

    SPEC = [("A", 59.0, 0.75, 5, 5), ("B", 84.0, 0.55, 5, 5)]

    def test_small_type_site_gets_small_type_ratio(self):
        rents, sales = _mix(self.SPEC)
        r = analyze(rents, sales, ASOF, subject_types=[(59.9, 300)])
        self.assertTrue(r.weighted_by_subject)
        self.assertAlmostEqual(r.ratio_pct, 75.0, places=1)

    def test_large_type_site_gets_large_type_ratio(self):
        rents, sales = _mix(self.SPEC)
        r = analyze(rents, sales, ASOF, subject_types=[(84.9, 300)])
        self.assertTrue(r.weighted_by_subject)
        self.assertAlmostEqual(r.ratio_pct, 55.0, places=1)

    def test_mixed_site_lands_between(self):
        rents, sales = _mix(self.SPEC)
        r = analyze(rents, sales, ASOF,
                    subject_types=[(59.9, 100), (84.9, 200)])
        self.assertGreaterEqual(r.ratio_pct, 55.0)
        self.assertLessEqual(r.ratio_pct, 75.0)

    def test_unmatched_subject_areas_fall_back_to_sample_weighting(self):
        rents, sales = _mix(self.SPEC)
        r = analyze(rents, sales, ASOF, subject_types=[(150.0, 300)])
        self.assertTrue(r.paired)
        self.assertFalse(r.weighted_by_subject)
        self.assertTrue(any("표본 수로 가중" in l for l in r.limitations))

    def test_weighting_basis_is_disclosed(self):
        rents, sales = _mix(self.SPEC)
        r = analyze(rents, sales, ASOF, subject_types=[(84.9, 300)])
        self.assertTrue(any("현장 타입 구성으로 가중" in l for l in r.limitations))


class TestFallbackToPooling(unittest.TestCase):
    def test_no_common_stratum_falls_back_and_warns(self):
        # 전세는 A단지에만, 매매는 B단지에만 — 짝지을 계층이 없다
        rents, sales = _mix([("A", 59.0, 0.75, 5, 0), ("B", 84.0, 0.55, 0, 5)])
        r = analyze(rents, sales, ASOF)
        self.assertFalse(r.paired)
        self.assertIsNotNone(r.ratio_pct)
        note = " ".join(r.limitations)
        self.assertIn("풀링", note)
        self.assertIn("[LIMITATION]", note)

    def test_fallback_quantifies_the_mix_gap(self):
        rents, sales = _mix([("A", 59.0, 0.75, 5, 0), ("B", 84.0, 0.55, 0, 5)])
        note = " ".join(analyze(rents, sales, ASOF).limitations)
        self.assertIn("59㎡", note)
        self.assertIn("84㎡", note)
        self.assertIn("단지 구성 상이", note)

    def test_fallback_ratio_is_the_plain_quotient(self):
        rents, sales = _mix([("A", 59.0, 0.75, 5, 0), ("B", 84.0, 0.55, 0, 5)])
        r = analyze(rents, sales, ASOF)
        self.assertAlmostEqual(r.jeonse_ppsm / r.sale_ppsm * 100, r.ratio_pct,
                               places=6)

    def test_thin_stratum_does_not_qualify(self):
        # 계층당 전세 2건 (< MIN_STRATUM) — 짝짓기 불가
        rents, sales = _mix([("A", 59.0, 0.75, 2, 5)], months=3)
        r = analyze(rents, sales, ASOF)
        self.assertFalse(r.paired)

    def test_minimum_stratum_is_enforced(self):
        self.assertGreaterEqual(MIN_STRATUM, 2)


class TestWeightedMedian(unittest.TestCase):
    def test_single_value(self):
        self.assertEqual(_weighted_median([(60.0, 5)]), 60.0)

    def test_weight_dominates(self):
        self.assertEqual(_weighted_median([(50.0, 1), (80.0, 99)]), 80.0)

    def test_tie_prefers_the_thinner_buffer(self):
        # 전세가율은 하방 완충 두께 — 동점이면 보수적인(낮은) 쪽
        self.assertEqual(_weighted_median([(55.0, 10), (75.0, 10)]), 55.0)

    def test_zero_weight_strata_are_excluded_by_caller(self):
        self.assertEqual(_weighted_median([(55.0, 0), (75.0, 3)]), 75.0)


class TestVerdictImpact(unittest.TestCase):
    """구성 편의가 판정 ①의 위험 강도를 낮추는 방향으로 작용했다."""

    def test_mix_bias_would_have_masked_a_thin_buffer(self):
        """대형 타입 현장인데 소형 전세가 표본을 지배하는 상황."""
        from statistics import median
        rents, sales = _mix([("A", 59.0, 0.80, 8, 1), ("B", 84.0, 0.50, 1, 8)])

        # 종전(풀링): 소형의 80%가 그대로 나와 '두터움'으로 읽혔다
        pooled = (median([r.deposit_ppsm for r in rents])
                  / median([t.price / t.area_m2 for t in sales]) * 100)
        self.assertGreaterEqual(pooled, RATIO_STRONG)

        # 현재(계층 + 현장 타입 가중): 실제 완충은 얇다
        subject = analyze(rents, sales, ASOF, subject_types=[(84.9, 300)])
        self.assertLess(subject.ratio_pct, RATIO_WEAK)
        self.assertEqual(subject.label, "하방 완충 얇음")

    def test_area_bucket_width_is_sane(self):
        self.assertGreaterEqual(AREA_BUCKET_M2, 5.0)


if __name__ == "__main__":
    unittest.main()
