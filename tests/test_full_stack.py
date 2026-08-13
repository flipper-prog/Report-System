"""전 레이어 동시 가동 통합 테스트.

각 커넥터는 개별로 검증되지만, 실제 운영은 **모두 켜진 상태**다. 레이어가
서로의 판정·근거원장·커버리지표에 끼어들면서 생기는 문제는 조합에서만 드러난다.
이 테스트는 그 조합 하나를 끝까지 통과시키고, 산출물의 불변 조건을 고정한다.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.connectors import (commerce, housing, migration, mobility,
                                     sgis, transit, unsold)
from report_system.connectors.base import Provenance
from report_system.ledger import ForecastLedger
from report_system.pricing import Coefficients
from report_system.pipeline import run
from report_system.render_html import markdown_to_html
from report_system.runstore import RunStore

# 개별 레이어 테스트의 가짜 Fetcher 를 재사용한다
from test_layers import FakeCommerceFetcher, FakeSgisFetcher, store as _store
from test_mobility_layers import (FakeTagoFetcher, INFLOW_CSV, OD_CSV,
                                  STATIONS_CSV, SITE_LAT, SITE_LNG, _stop)

UNSOLD_CSV = """month,unsold,after_done
2026-02,1200,150
2026-03,1350,160
2026-04,1500,180
2026-05,1600,200
2026-06,1700,220
2026-07,1750,240
"""

PROV = [
    Provenance("국토교통부 아파트 매매 실거래가 OpenAPI (E01)", "https://x?serviceKey=***KEY***",
               "2026-07-25T01:00:00+00:00", "a" * 64, True),
    Provenance("국토교통부 아파트 전월세 실거래가 OpenAPI (E01-R)", "https://y",
               "2026-07-25T01:00:01+00:00", "b" * 64, True),
    Provenance("청약홈 APT 분양정보 상세 (E02)", "https://z",
               "2026-07-25T01:00:02+00:00", "c" * 64, True),
    Provenance("SGIS 통계지리정보 (L1·L2·L4)", "https://s",
               "2026-07-25T01:00:03+00:00", "d" * 64, False),
    Provenance("소상공인시장진흥공단 상가업소 (L9)", "https://c",
               "2026-07-25T01:00:04+00:00", "e" * 64, False),
    Provenance("국토교통부 TAGO 정류소정보 (L8)", "https://t",
               "2026-07-25T01:00:05+00:00", "f" * 64, False),
]


class TestFullStack(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        (tmp / "u.csv").write_text(UNSOLD_CSV, encoding="utf-8")
        (tmp / "m.csv").write_text(INFLOW_CSV, encoding="utf-8")
        (tmp / "od.csv").write_text(OD_CSV, encoding="utf-8")
        (tmp / "st.csv").write_text(STATIONS_CSV, encoding="utf-8")

        comps = sd.build_comparables()
        cls.comps = comps
        cls.res = run(
            site=sd.build_site(), comps=comps,
            txs=sd.build_transactions(comps),
            sub_history=sd.build_subscription_history(),
            supply_items=sd.build_supply(),
            catalyst_plans_old=sd.build_catalysts(),
            catalyst_plans_new=sd.build_catalysts(),
            dataset_meta=sd.build_dataset_meta(),
            incomes=sd.build_incomes(), feedback=sd.build_feedback(),
            listings=sd.build_listing_snapshots(),
            rents=sd.build_rents(comps),
            asof=sd.ASOF,
            ledger=ForecastLedger(str(tmp / "led.db")),
            store=RunStore(str(tmp / "runs.db")),
            region_stats=sgis.fetch_region_stats(
                FakeSgisFetcher(), "11680", [2021, 2023, 2025], token="TOK"),
            commerce=commerce.fetch_radius(
                FakeCommerceFetcher([_store("소매", "편의점"), _store("의료", "내과의원"),
                                     _store("교육", "학원"), _store("음식", "커피전문점"),
                                     _store("금융", "은행지점")]),
                "k", 127.0, 37.5),
            unsold=unsold.load(str(tmp / "u.csv")),
            housing=sd.build_housing(),
            income_stats=sd.build_income_stats(),
            migration=migration.load(str(tmp / "m.csv"), population=300_000),
            mobility=mobility.load(str(tmp / "od.csv"), focus="강남구", purpose="출근"),
            transit=transit.collect(
                FakeTagoFetcher([_stop("정류장1", 37.5002, 127.0002),
                                 _stop("정류장2", 37.5005, 127.0005),
                                 _stop("정류장3", 37.5008, 127.0008)]),
                "k", SITE_LAT, SITE_LNG, stations_file=str(tmp / "st.csv")),
            coef=Coefficients(age_per_year=0.019, age_cap=0.57, floor_low=0.048,
                              floor_high=-0.028, time_per_year=0.012,
                              source="실데이터 교정 (2026-07-25 기준, 관측 720건)"),
            provenance=PROV)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    # ── 산출물 전체 ─────────────────────────────────────────────────────────
    def test_every_section_present(self):
        md = self.res.markdown
        for section in ("결론 요약", "데이터 커버리지", "거래 데이터 정제",
                        "품질조정 가격 밴드", "전세 기반 하방 점검",
                        "실부담 시뮬레이션", "청약 수요 전망", "모델 검증",
                        "확률조정 공급", "환금성", "지역 기반 통계",
                        "주택건설실적",
                        "개발계획 촉매카드", "조기경보", "표현 린트", "근거원장"):
            self.assertIn(section, md, f"'{section}' 절이 누락됨")

    def test_all_four_verdicts_cite_their_layers(self):
        by = {v.name[0]: v for v in self.res.inputs.verdicts}
        self.assertTrue(any("전세가율" in r for r in by["①"].rationale))
        joined2 = " ".join(by["②"].rationale)
        for token in ("지역 통계", "인구이동", "생활이동", "교통 접근성", "생활 인프라"):
            self.assertIn(token, joined2)
        self.assertTrue(any("미분양" in r for r in by["③"].rationale))
        self.assertTrue(any("주택건설실적" in r for r in by["③"].rationale))
        self.assertTrue(by["④"].rationale)

    # ── 근거원장 ────────────────────────────────────────────────────────────
    def test_no_layer_is_registered_missing(self):
        """모두 켠 실행에서는 '미산출'이 남지 않아야 한다."""
        missing = [e.metric for e in self.res.inputs.evidence.items
                   if not e.computed]
        self.assertEqual(missing, [], f"미산출 항목이 남음: {missing}")

    def test_evidence_ids_are_unique_and_sequential(self):
        ids = [e.id for e in self.res.inputs.evidence.items]
        self.assertEqual(ids, [f"E-{i:02d}" for i in range(1, len(ids) + 1)])

    def test_evidence_links_real_collection_hashes(self):
        led = self.res.inputs.evidence
        self.assertGreaterEqual(led.sourced_count, 4)
        self.assertIn("sha256", led.as_markdown())

    def test_calibrated_coefficients_declared_not_flagged_as_example(self):
        anchor = next(e for e in self.res.inputs.evidence.items
                      if e.metric.startswith("품질조정 앵커"))
        self.assertIn("실데이터 교정", anchor.method)
        self.assertFalse(any("미교정" in x for x in anchor.limitations))
        # 가격 밴드 모델 카드는 교정값을 밝히고 '예시값' 문구를 쓰지 않는다.
        # (시나리오 드라이버 계수는 별개이며 여전히 미교정이므로 그 문구가 남는다)
        card = next(c for c in self.res.inputs.model_cards
                    if "가격 밴드" in c.model_id)
        self.assertTrue(any("실데이터 교정" in x for x in card.known_limits))
        self.assertFalse(any("예시값" in x for x in card.known_limits))

    # ── 커버리지표 ──────────────────────────────────────────────────────────
    def test_coverage_table_has_no_unexpected_gaps(self):
        rows = {r.layer.split()[0]: r for r in self.res.inputs.coverage_rows}
        # 민간 라이선스(L6·L10) 외에는 미확보가 없어야 한다
        gaps = [k for k, r in rows.items()
                if r.coverage.value == "미확보" and k not in ("L6", "L10")]
        self.assertEqual(gaps, [], f"예상 밖 미확보 레이어: {gaps}")

    def test_rent_and_listings_noted_on_l11(self):
        rows = {r.layer.split()[0]: r for r in self.res.inputs.coverage_rows}
        self.assertIn("전월세 연동됨", rows["L11"].note)

    # ── 안전 불변조건 ───────────────────────────────────────────────────────
    def test_no_api_key_or_raw_url_in_output(self):
        md = self.res.markdown
        self.assertNotIn("serviceKey", md)
        self.assertNotIn("https://", md)

    def test_forecasts_are_sealed_and_marked(self):
        self.assertTrue(self.res.forecast_id)
        self.assertIn("[FORECAST]", self.res.markdown)
        for e in self.res.inputs.evidence.items:
            if e.metric == "청약 경쟁률 전망":
                self.assertIn(self.res.forecast_id, e.method)

    def test_blocked_claims_never_appear_as_usable(self):
        lint = self.res.inputs.lint
        self.assertTrue(lint.blocked)
        for claim, _reason in lint.blocked:
            self.assertNotIn(f"· 사용 가능] {claim.text}", self.res.markdown)

    def test_limitations_survive_into_report(self):
        md = self.res.markdown
        for token in ("해상도", "직선거리", "갱신 계약", "LIMITATION"):
            self.assertIn(token, md)

    # ── 렌더링 ──────────────────────────────────────────────────────────────
    def test_html_render_is_self_contained(self):
        h = markdown_to_html(self.res.markdown, "전체 통합")
        self.assertTrue(h.startswith("<!doctype html>"))
        self.assertIn("<nav class='toc'>", h)
        self.assertIn("class='badge", h)
        # 외부 자산·스크립트를 끌어오지 않는다
        self.assertNotIn("<script", h)
        self.assertNotIn("src=", h)
        self.assertNotIn("@import", h)
        self.assertFalse(re.search(r"href=['\"]http", h))

    def test_markdown_tables_are_wellformed(self):
        """헤더와 본문의 열 수가 어긋나면 렌더러가 조용히 잘라낸다."""
        lines = self.res.markdown.split("\n")
        i = 0
        checked = 0
        while i < len(lines) - 1:
            if (lines[i].startswith("|")
                    and re.fullmatch(r"\|[\s:|-]+\|", lines[i + 1].strip())):
                width = lines[i].count("|")
                j = i + 2
                while j < len(lines) and lines[j].startswith("|"):
                    self.assertEqual(lines[j].count("|"), width,
                                     f"{j + 1}행 열 수 불일치: {lines[j][:70]}")
                    j += 1
                checked += 1
                i = j
            else:
                i += 1
        self.assertGreater(checked, 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
