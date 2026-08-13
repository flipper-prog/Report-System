"""신규 레이어 커넥터 테스트 — SGIS(L1·L2·L4) · 상권(L9) · 미분양(L12 보강)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.connectors import commerce, sgis, unsold
from report_system.ledger import ForecastLedger
from report_system.pipeline import run

# ── 픽스처 ───────────────────────────────────────────────────────────────────

AUTH_OK = json.dumps({"errCd": 0, "result": {"accessToken": "TOK-123"}})
AUTH_FAIL = json.dumps({"errCd": -401, "errMsg": "인증 실패"})


def pop(year_value):
    return json.dumps({"errCd": 0, "result": [{"population": year_value}]})


def house(hh, avg):
    return json.dumps({"errCd": 0,
                       "result": [{"household_cnt": hh, "avg_fmember_cnt": avg}]})


def comp(corp, workers):
    return json.dumps({"errCd": 0,
                       "result": [{"corp_cnt": corp, "tot_worker": workers}]})


class FakeSgisFetcher:
    """연도별 응답을 URL·params 로 분기하는 가짜 Fetcher."""

    def __init__(self, missing_year: int | None = None):
        self.missing_year = missing_year
        self.calls = 0

    def get(self, source, url, params):
        self.calls += 1
        if "authentication" in url:
            return AUTH_OK.encode()
        year = int(params["year"])
        if self.missing_year and year == self.missing_year:
            return json.dumps({"errCd": -100, "errMsg": "자료 없음"}).encode()
        idx = year - 2020
        if "population" in url:
            return pop(100_000 + idx * 2_000).encode()
        if "household" in url:
            return house(40_000 + idx * 1_500, 2.5 - idx * 0.02).encode()
        return comp(5_000 + idx * 100, 30_000 + idx * 900).encode()


class TestSgis(unittest.TestCase):
    def test_token_and_series(self):
        f = FakeSgisFetcher()
        stats = sgis.fetch_region_stats(f, "11680", [2021, 2023, 2025], token="TOK")
        self.assertEqual(len(stats.population), 3)
        self.assertEqual(len(stats.households), 3)
        self.assertEqual(len(stats.employees), 3)
        self.assertGreater(stats.population_cagr, 0)
        self.assertGreater(stats.household_cagr, 0)
        self.assertIn("인구", stats.summary())

    def test_spatial_limitation_always_present(self):
        stats = sgis.fetch_region_stats(FakeSgisFetcher(), "11680", [2023], token="TOK")
        self.assertTrue(any("해상도" in x for x in stats.limitations))

    def test_missing_year_is_skipped_not_fatal(self):
        f = FakeSgisFetcher(missing_year=2023)
        stats = sgis.fetch_region_stats(f, "11680", [2021, 2023, 2025], token="TOK")
        self.assertEqual(len(stats.population), 2)   # 2023 제외

    def test_auth_failure_raises(self):
        class BadAuth(FakeSgisFetcher):
            def get(self, source, url, params):
                return AUTH_FAIL.encode()
        with self.assertRaises(sgis.SgisAuthError):
            sgis.get_token(BadAuth(), "k", "s")

    def test_credentials_missing_message(self):
        saved = {k: os.environ.pop(k, None) for k in (sgis.KEY_ENV, sgis.SECRET_ENV)}
        try:
            with self.assertRaises(sgis.SgisAuthError) as ctx:
                sgis.credentials()
            self.assertIn("SGIS_CONSUMER_KEY", str(ctx.exception))
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_cagr_none_with_single_point(self):
        stats = sgis.fetch_region_stats(FakeSgisFetcher(), "11680", [2023], token="TOK")
        self.assertIsNone(stats.population_cagr)


# ── 상권 ─────────────────────────────────────────────────────────────────────

def store(major, small):
    return {"indsLclsNm": major, "indsSclsNm": small}


class FakeCommerceFetcher:
    def __init__(self, items, total=None, code="00"):
        self.items = items
        self.total = total if total is not None else len(items)
        self.code = code

    def get(self, source, url, params):
        return json.dumps({
            "header": {"resultCode": self.code, "resultMsg": "OK"},
            "body": {"items": self.items, "totalCount": self.total},
        }).encode()


class TestCommerce(unittest.TestCase):
    def test_counts_and_essential_coverage(self):
        items = [store("소매", "편의점"), store("의료", "내과의원"),
                 store("교육", "보習학원"), store("음식", "커피전문점"),
                 store("금융", "은행지점"), store("소매", "잡화")]
        stats = commerce.fetch_radius(FakeCommerceFetcher(items), "k", 127.0, 37.5)
        self.assertEqual(stats.total_stores, 6)
        self.assertEqual(stats.essential_coverage, 1.0)
        self.assertEqual(stats.label, "생활 인프라 충족")
        self.assertIn("업소", stats.summary())

    def test_sparse_area_flagged(self):
        stats = commerce.fetch_radius(FakeCommerceFetcher([]), "k", 127.0, 37.5)
        self.assertEqual(stats.total_stores, 0)
        self.assertEqual(stats.label, "판정 불가")
        self.assertTrue(any("0건" in x for x in stats.limitations))

    def test_partial_essential_gives_lower_label(self):
        items = [store("소매", "편의점"), store("소매", "잡화")]
        stats = commerce.fetch_radius(FakeCommerceFetcher(items), "k", 127.0, 37.5)
        self.assertLess(stats.essential_coverage, 0.5)
        self.assertEqual(stats.label, "생활 인프라 부족")

    def test_api_error_raises(self):
        with self.assertRaises(commerce.CommerceApiError):
            commerce.fetch_radius(FakeCommerceFetcher([], code="99"), "k", 127.0, 37.5)

    def test_card_spending_limitation_always_noted(self):
        stats = commerce.fetch_radius(
            FakeCommerceFetcher([store("소매", "편의점")]), "k", 127.0, 37.5)
        self.assertTrue(any("카드소비" in x for x in stats.limitations))


# ── 미분양 ───────────────────────────────────────────────────────────────────

UNSOLD_CSV = """month,unsold,after_done
2026-02,1200,150
2026-03,1350,160
2026-04,1500,180
2026-05,1600,200
2026-06,1700,220
2026-07,1750,240
"""

UNSOLD_DECLINING = """month,unsold,after_done
2026-02,2000,300
2026-07,1200,150
"""


class TestUnsold(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, text, name="u.csv"):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def test_rising_trend_labeled(self):
        s = unsold.load(self._write(UNSOLD_CSV))
        self.assertEqual(len(s.points), 6)
        self.assertGreater(s.trend_pct(), 20)
        self.assertIn("증가", s.label)
        self.assertIn("준공 후", s.summary())

    def test_declining_trend_labeled(self):
        s = unsold.load(self._write(UNSOLD_DECLINING))
        self.assertLess(s.trend_pct(), -20)
        self.assertIn("감소", s.label)

    def test_future_months_excluded(self):
        s = unsold.load(self._write(UNSOLD_CSV), until=date(2026, 4, 30))
        self.assertEqual(len(s.points), 3)
        self.assertTrue(any("이후" in x for x in s.skipped))

    def test_missing_column_raises(self):
        with self.assertRaises(unsold.UnsoldFormatError):
            unsold.load(self._write("month\n2026-01\n"))

    def test_bad_rows_skipped(self):
        s = unsold.load(self._write("month,unsold\n2026-01,100\nbad,50\n2026-02,-3\n"))
        self.assertEqual(len(s.points), 1)
        self.assertEqual(len(s.skipped), 2)

    def test_json_supported(self):
        data = [{"month": "2026-06", "unsold": 100},
                {"month": "2026-07", "unsold": 140}]
        p = self.tmp / "u.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        s = unsold.load(str(p))
        self.assertEqual(len(s.points), 2)


# ── 파이프라인 통합 ──────────────────────────────────────────────────────────

class TestLayerIntegration(unittest.TestCase):
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

    def test_region_and_commerce_appear_in_demand_verdict(self):
        stats = sgis.fetch_region_stats(FakeSgisFetcher(), "11680", [2021, 2023, 2025], token="TOK")
        cm = commerce.fetch_radius(
            FakeCommerceFetcher([store("소매", "편의점"), store("의료", "의원")]),
            "k", 127.0, 37.5)
        res = self._run(region_stats=stats, commerce=cm)
        v2 = next(v for v in res.inputs.verdicts if v.name.startswith("②"))
        self.assertTrue(any("지역 통계" in r for r in v2.rationale))
        self.assertTrue(any("생활 인프라" in r for r in v2.rationale))
        self.assertIn("지역 기반 통계", res.markdown)

    def test_rising_unsold_escalates_supply_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "u.csv"
            p.write_text(UNSOLD_CSV, encoding="utf-8")
            s = unsold.load(str(p))
        res = self._run(unsold=s)
        v3 = next(v for v in res.inputs.verdicts if v.name.startswith("③"))
        self.assertTrue(any("미분양" in r for r in v3.rationale))
        self.assertEqual(v3.direction, "부정")

    def test_coverage_table_reflects_new_layers(self):
        stats = sgis.fetch_region_stats(FakeSgisFetcher(), "11680", [2023, 2025], token="TOK")
        res = self._run(region_stats=stats)
        rows = {r.layer.split()[0]: r for r in res.inputs.coverage_rows}
        self.assertEqual(rows["L1"].coverage.value, "조건부")     # SGIS 시군구 단위
        self.assertEqual(rows["L9"].coverage.value, "미확보")     # 상권 미연동

    def test_without_new_layers_report_still_valid(self):
        res = self._run()
        self.assertNotIn("지역 기반 통계", res.markdown)
        self.assertIn("결론 요약", res.markdown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
