"""환금성·상품 프로파일·HTML 렌더러 테스트."""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import profiles, sample_data as sd
from report_system.liquidity import analyze
from report_system.models import Comparable, Transaction
from report_system.render_html import markdown_to_html
from report_system.supply import probability_adjusted
from report_system.verdicts import supply_verdict

ASOF = date(2026, 7, 25)


def _txs(n: int, complex_id: str = "C1") -> list[Transaction]:
    return [Transaction(complex_id, ASOF - timedelta(days=20 * i), 84.0, 10,
                        800_000_000 + i * 1_000_000) for i in range(n)]


class TestLiquidity(unittest.TestCase):
    def test_turnover_and_label(self):
        comps = {"C1": Comparable("C1", "단지", 2018, 200, 2, 500)}
        res = analyze(_txs(24), comps, ASOF, window_months=24)
        self.assertIsNotNone(res.turnover_pct_year)
        self.assertGreater(res.turnover_pct_year, 0)
        self.assertIn(res.label, ("환금성 양호", "환금성 보통", "환금성 취약"))

    def test_low_turnover_is_weak(self):
        comps = {"C1": Comparable("C1", "단지", 2018, 2000, 2, 500)}
        res = analyze(_txs(6), comps, ASOF, window_months=24)
        self.assertEqual(res.label, "환금성 취약")

    def test_missing_units_reports_limitation(self):
        comps = {"C1": Comparable("C1", "단지", 2018, 0, 2, 500)}
        res = analyze(_txs(10), comps, ASOF)
        self.assertIsNone(res.turnover_pct_year)
        self.assertIn("판정 불가", res.as_rationale())
        self.assertTrue(any("세대수" in x for x in res.limitations))

    def test_always_flags_missing_jeonse_data(self):
        res = analyze([], {}, ASOF)
        self.assertTrue(any("전세" in x for x in res.limitations))

    def test_weak_liquidity_escalates_supply_verdict(self):
        sa = probability_adjusted(
            [__import__("report_system.models", fromlist=["SupplyItem"]).SupplyItem(
                "소규모", 100, __import__("report_system.models",
                                        fromlist=["SupplyStage"]).SupplyStage.MOVEIN, 6)])
        comps = {"C1": Comparable("C1", "단지", 2018, 5000, 2, 500)}
        weak = analyze(_txs(4), comps, ASOF)
        v = supply_verdict(sa, site_units=460, liq=weak)
        self.assertEqual(v.direction, "부정")
        self.assertTrue(any("환금성" in r for r in v.rationale))


class TestProfiles(unittest.TestCase):
    def test_apartment_default(self):
        p = profiles.get("아파트")
        self.assertTrue(p.subscription_applicable)
        self.assertEqual(p.area_tolerance, 0.20)

    def test_knowledge_center_skips_subscription(self):
        p = profiles.get(profiles.ProductType.KNOWLEDGE)
        self.assertFalse(p.subscription_applicable)
        self.assertIn("청약 전망 미적용", profiles.applicability_note(p))
        self.assertGreater(p.demand_weights["C2"], p.demand_weights["C1"])

    def test_retail_weights_footfall(self):
        p = profiles.get(profiles.ProductType.RETAIL)
        self.assertEqual(p.demand_weights["C4"], 1.0)
        self.assertLess(p.demand_weights["C1"], 0.5)

    def test_all_profiles_have_notes(self):
        for t in profiles.ProductType:
            self.assertTrue(profiles.get(t).notes)


class TestProfileIntegration(unittest.TestCase):
    """프로파일이 실제 파이프라인 동작을 바꾸는지 확인 (dead code 방지)."""

    def _run(self, product: str):
        from report_system.ledger import ForecastLedger
        from report_system.pipeline import run
        site = sd.build_site()
        site.product_type = product
        comps = sd.build_comparables()
        return run(site=site, comps=comps, txs=sd.build_transactions(comps),
                   sub_history=sd.build_subscription_history(),
                   supply_items=sd.build_supply(),
                   catalyst_plans_old=sd.build_catalysts(),
                   catalyst_plans_new=sd.build_catalysts(),
                   dataset_meta=sd.build_dataset_meta(),
                   incomes=sd.build_incomes(),
                   feedback=sd.build_feedback(),
                   listings=sd.build_listing_snapshots(),
                   asof=sd.ASOF, ledger=ForecastLedger())

    def test_knowledge_center_skips_subscription_forecast(self):
        res = self._run("지식산업센터")
        self.assertFalse(res.inputs.sub_forecast.ok)
        self.assertIn("청약 제도 비적용", res.inputs.sub_forecast.reason)
        self.assertEqual(res.forecast_id, "")          # 봉인 대상 없음
        self.assertIn("청약 전망 미적용", res.markdown)

    def test_apartment_produces_subscription_forecast(self):
        res = self._run("아파트")
        self.assertTrue(res.inputs.sub_forecast.ok)
        self.assertTrue(res.forecast_id)

    def test_profile_changes_band_sample_size(self):
        """오피스텔은 면적 허용치가 넓어 동일 데이터에서 표본이 더 많이 잡힌다."""
        apt = self._run("아파트")
        oft = self._run("오피스텔")
        apt_n = max((b.n for b in apt.inputs.bands if b.level == "타입"), default=0)
        oft_n = max((b.n for b in oft.inputs.bands if b.level == "타입"), default=0)
        self.assertGreater(oft_n, apt_n)

    def test_profile_notes_rendered(self):
        res = self._run("오피스텔")
        self.assertIn("오피스텔", res.markdown)
        self.assertIn("직주근접", res.markdown)


class TestHtmlRenderer(unittest.TestCase):
    def test_tables_headings_lists(self):
        md = ("# 제목\n\n## 절\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
              "- 항목1\n- 항목2\n\n> 인용문\n")
        h = markdown_to_html(md, "t")
        for frag in ("<h1>", "<h2>", "<table>", "<th>a</th>", "<td>1</td>",
                     "<li>항목1</li>", "<blockquote>"):
            self.assertIn(frag, h)

    def test_escapes_and_inline_marks(self):
        h = markdown_to_html("**굵게** ~~취소~~ `코드` <script>x</script>\n")
        self.assertIn("<strong>굵게</strong>", h)
        self.assertIn("<del>취소</del>", h)
        self.assertIn("<code>코드</code>", h)
        self.assertNotIn("<script>", h)
        self.assertIn("&lt;script&gt;", h)

    def test_dark_mode_tokens_present(self):
        h = markdown_to_html("# t\n")
        self.assertIn("prefers-color-scheme: dark", h)
        self.assertIn('data-theme="dark"', h)

    def test_real_report_renders(self):
        from report_system.ledger import ForecastLedger
        from report_system.pipeline import run
        comps = sd.build_comparables()
        res = run(site=sd.build_site(), comps=comps,
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
        h = markdown_to_html(res.markdown, "리포트")
        self.assertGreater(len(h), 10_000)
        self.assertIn("<table>", h)
        self.assertTrue(h.startswith("<!doctype html>"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
