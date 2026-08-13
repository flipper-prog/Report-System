"""근거원장 테스트 — 모든 핵심 수치가 출처·산출식·표본과 함께 등재되는가."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.connectors.base import Provenance
from report_system.evidence import EvidenceLedger, Source
from report_system.ledger import ForecastLedger
from report_system.models import ClaimGrade
from report_system.pipeline import run

PROV = [
    Provenance("국토교통부 아파트 매매 실거래가 OpenAPI (E01)",
               "https://example/x?serviceKey=***KEY***", "2026-07-25T01:00:00+00:00",
               "a" * 64, True),
    Provenance("국토교통부 아파트 전월세 실거래가 OpenAPI (E01-R)",
               "https://example/y?serviceKey=***KEY***", "2026-07-25T01:00:05+00:00",
               "b" * 64, True),
    Provenance("청약홈 APT 분양정보 상세 (E02)", "https://example/z",
               "2026-07-25T01:00:09+00:00", "c" * 64, False),
]


class TestLedgerBasics(unittest.TestCase):
    def test_ids_are_sequential(self):
        led = EvidenceLedger()
        a = led.add("지표1", "1", "m", ClaimGrade.FACT)
        b = led.add("지표2", "2", "m", ClaimGrade.FACT)
        self.assertEqual((a.id, b.id), ("E-01", "E-02"))

    def test_missing_items_are_recorded_not_dropped(self):
        led = EvidenceLedger()
        e = led.add_missing("전세가율", "표본 미달")
        self.assertEqual(e.value, "미산출")
        self.assertFalse(e.computed)
        self.assertEqual(e.grade, ClaimGrade.LIMITATION)
        self.assertEqual(len(led.items), 1)
        self.assertEqual(led.computed_count, 0)

    def test_find_matches_provenance_by_keyword(self):
        led = EvidenceLedger(PROV)
        self.assertEqual(len(led.find("매매 실거래")), 1)
        self.assertEqual(len(led.find("전월세")), 1)
        self.assertEqual(len(led.find("매매 실거래", "전월세")), 2)
        self.assertEqual(led.find("없는출처"), [])

    def test_find_dedupes_repeated_collections(self):
        led = EvidenceLedger(PROV + PROV)
        self.assertEqual(len(led.find("매매 실거래")), 1)

    def test_source_short_includes_hash_prefix(self):
        s = Source("출처", "2026-07-25T01:00:00+00:00", "d" * 64)
        self.assertIn("sha256 dddddddddddd…", s.short())
        self.assertEqual(Source("출처만").short(), "출처만")

    def test_sourced_count_counts_only_hashed_sources(self):
        led = EvidenceLedger(PROV)
        led.add("연결됨", "1", "m", ClaimGrade.FACT, led.find("매매 실거래"))
        led.add("연결안됨", "2", "m", ClaimGrade.FACT)
        led.add_missing("미산출", "사유")
        self.assertEqual(led.computed_count, 2)
        self.assertEqual(led.sourced_count, 1)

    def test_accepts_provenance_dicts(self):
        led = EvidenceLedger([{"source": "출처A", "fetched_at": "t", "sha256": "e" * 64}])
        self.assertEqual(len(led.find("출처A")), 1)

    def test_markdown_contains_all_sections(self):
        led = EvidenceLedger(PROV)
        led.add("지표", "값", "방법", ClaimGrade.CALCULATION, led.find("매매 실거래"),
                n=12, limitations=["한계 문장"])
        md = led.as_markdown()
        self.assertIn("| E-01 | 지표 | 값 | CALCULATION | 12 | 방법 |", md)
        self.assertIn("데이터 출처", md)
        self.assertIn("sha256 aaaaaaaaaaaa…", md)
        self.assertIn("한계 문장", md)

    def test_markdown_notes_when_no_sources_linked(self):
        led = EvidenceLedger()
        led.add("지표", "값", "방법", ClaimGrade.FACT)
        self.assertIn("수집 이력에 연결된 항목이 없습니다", led.as_markdown())

    def test_empty_ledger_is_stated(self):
        self.assertIn("등재된 근거 항목이 없습니다", EvidenceLedger().as_markdown())


class TestPipelineEvidence(unittest.TestCase):
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

    def test_report_contains_evidence_section(self):
        res = self._run()
        self.assertIn("근거원장", res.markdown)
        self.assertIn("| E-01 |", res.markdown)

    def test_headline_metrics_are_all_registered(self):
        led = self._run().inputs.evidence
        metrics = {e.metric for e in led.items}
        for expected in ("정제 후 사용 거래", "품질조정 앵커 (타입 중위 ㎡단가)",
                         "청약 경쟁률 전망", "확률조정 공급배수",
                         "연환산 거래 회전율", "조건부 가격 시나리오"):
            self.assertIn(expected, metrics)

    def test_uncollected_layers_are_registered_as_missing(self):
        led = self._run().inputs.evidence
        by = {e.metric: e for e in led.items}
        self.assertEqual(by["인구이동 순이동"].value, "미산출")
        self.assertEqual(by["대중교통 접근성"].value, "미산출")
        self.assertTrue(by["인구이동 순이동"].limitations)

    def test_collected_layers_replace_missing_entries(self):
        comps = sd.build_comparables()
        res = self._run(rents=sd.build_rents(comps))
        by = {e.metric: e for e in res.inputs.evidence.items}
        self.assertNotEqual(by["전세가율"].value, "미산출")
        self.assertEqual(by["전세가율"].grade, ClaimGrade.CALCULATION)

    def test_uncalibrated_coefficients_flagged_on_anchor(self):
        led = self._run().inputs.evidence
        anchor = next(e for e in led.items if e.metric.startswith("품질조정 앵커"))
        self.assertIn("초기값", anchor.method)
        self.assertTrue(any("미교정" in x for x in anchor.limitations))

    def test_calibrated_coefficients_change_anchor_method(self):
        from report_system.pricing import Coefficients
        led = self._run(coef=Coefficients(age_per_year=0.02,
                                          source="실데이터 교정 (테스트)")).inputs.evidence
        anchor = next(e for e in led.items if e.metric.startswith("품질조정 앵커"))
        self.assertIn("실데이터 교정", anchor.method)
        self.assertFalse(any("미교정" in x for x in anchor.limitations))

    def test_forecast_entries_cite_seal_id(self):
        res = self._run()
        sub = next(e for e in res.inputs.evidence.items
                   if e.metric == "청약 경쟁률 전망")
        self.assertEqual(sub.grade, ClaimGrade.FORECAST)
        self.assertIn(res.forecast_id, sub.method)

    def test_provenance_links_sources_with_hashes(self):
        led = self._run(provenance=PROV).inputs.evidence
        tx = next(e for e in led.items if e.metric == "정제 후 사용 거래")
        self.assertTrue(tx.sources)
        self.assertTrue(tx.sources[0].sha256)
        self.assertGreaterEqual(led.sourced_count, 1)
        self.assertIn("sha256", led.as_markdown())

    def test_no_api_key_leaks_into_evidence(self):
        md = self._run(provenance=PROV).markdown
        self.assertNotIn("serviceKey=", md)   # 원장은 URL이 아니라 출처명만 싣는다


if __name__ == "__main__":
    unittest.main(verbosity=2)
