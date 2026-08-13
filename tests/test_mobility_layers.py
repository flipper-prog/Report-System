"""신규 레이어 커넥터 테스트 — 인구이동(L3) · 생활이동 O/D(L7) · 교통 접근성(L8)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.connectors import migration, mobility, transit
from report_system.geo import haversine_m, walk_minutes
from report_system.ledger import ForecastLedger
from report_system.models import FieldFeedback
from report_system.feedback import check as feedback_check
from report_system.pipeline import run


class TestGeo(unittest.TestCase):
    def test_known_distance(self):
        # 서울시청 ↔ 강남역 직선거리는 약 8.3km
        d = haversine_m(37.5665, 126.9780, 37.4979, 127.0276)
        self.assertGreater(d, 7_500)
        self.assertLess(d, 9_000)

    def test_zero_distance(self):
        self.assertAlmostEqual(haversine_m(37.5, 127.0, 37.5, 127.0), 0.0, places=6)

    def test_walk_minutes_applies_detour(self):
        # 직선 670m → 보정 후 871m → 약 13분
        self.assertAlmostEqual(walk_minutes(670.0), 13.0, delta=0.2)


# ── L3 인구이동 ──────────────────────────────────────────────────────────────

INFLOW_CSV = """period,moved_in,moved_out,from_region
2026-02,5000,4000,성남시
2026-03,5200,4100,성남시
2026-04,5100,4050,하남시
2026-05,5300,4200,성남시
2026-06,5400,4150,광주시
2026-07,5500,4300,성남시
"""

OUTFLOW_CSV = """period,moved_in,moved_out
2026-02,3000,5000
2026-03,3100,5200
2026-07,2900,5400
"""


class TestMigration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, text, name="m.csv"):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def test_inflow_labeled_with_population(self):
        s = migration.load(self._write(INFLOW_CSV), population=300_000)
        self.assertEqual(len(s.points), 6)
        self.assertGreater(s.net_recent(), 0)
        self.assertGreater(s.net_rate_per_1000(), 0)
        self.assertEqual(s.label, "순유입 지속")
        self.assertIn("순이동", s.summary())

    def test_outflow_labeled_without_population(self):
        s = migration.load(self._write(OUTFLOW_CSV))
        self.assertLess(s.net_recent(), 0)
        self.assertIsNone(s.net_rate_per_1000())
        self.assertTrue(s.label.startswith("순유출"))
        self.assertTrue(any("기준 인구 미지정" in x for x in s.limitations))

    def test_same_period_rows_are_merged(self):
        csv = ("period,moved_in,moved_out,from_region\n"
               "2026-07,1000,500,A시\n2026-07,800,400,B시\n")
        s = migration.load(self._write(csv))
        self.assertEqual(len(s.points), 1)
        self.assertEqual(s.points[0].moved_in, 1800)
        self.assertEqual(s.points[0].moved_out, 900)
        self.assertEqual(set(s.inflow_sources), {"A시", "B시"})

    def test_future_periods_excluded(self):
        s = migration.load(self._write(INFLOW_CSV), until="2026-04")
        self.assertEqual(len(s.points), 3)
        self.assertTrue(any("이후" in x for x in s.skipped))

    def test_missing_column_raises(self):
        with self.assertRaises(migration.MigrationFormatError):
            migration.load(self._write("period,moved_in\n2026-01,100\n"))

    def test_bad_rows_skipped(self):
        s = migration.load(self._write(
            "period,moved_in,moved_out\n2026-01,100,50\nxx,1,1\n2026-02,-5,3\n"))
        self.assertEqual(len(s.points), 1)
        self.assertEqual(len(s.skipped), 2)

    def test_top_sources_share(self):
        s = migration.load(self._write(INFLOW_CSV))
        top = s.top_sources(3)
        self.assertEqual(top[0][0], "성남시")
        self.assertAlmostEqual(sum(sh for _, sh in s.top_sources(10)), 1.0, places=6)

    def test_visitor_alignment_matched_and_mismatched(self):
        s = migration.load(self._write(INFLOW_CSV))
        share, note = s.visitor_alignment({"성남시": 80, "부산시": 20})
        self.assertGreaterEqual(share, 0.5)
        self.assertIn("정합", note)
        share2, note2 = s.visitor_alignment({"부산시": 90, "성남시": 10})
        self.assertLess(share2, 0.25)
        self.assertIn("재검토", note2)

    def test_visitor_alignment_none_without_sources(self):
        s = migration.load(self._write(OUTFLOW_CSV))
        self.assertIsNone(s.visitor_alignment({"성남시": 10}))
        self.assertTrue(any("출발지" in x for x in s.limitations))

    def test_feedback_flags_mismatch(self):
        s = migration.load(self._write(INFLOW_CSV))
        fb = FieldFeedback(total_consults=100, rejections={},
                           visitor_home_regions={"부산시": 95, "성남시": 5})
        flags = feedback_check(fb, price_verdict_positive=False,
                               expected_home_regions=["부산시"],
                               catalyst_ad_active=False, migration=s)
        self.assertTrue(any("인구이동 교차검증" in f.signal for f in flags))

    def test_kosis_parsing(self):
        rows = [
            {"PRD_DE": "202606", "ITM_ID": "T1", "DT": "5,000"},
            {"PRD_DE": "202606", "ITM_ID": "T2", "DT": "4000"},
            {"PRD_DE": "202607", "ITM_ID": "T1", "DT": "5200"},
            {"PRD_DE": "202607", "ITM_ID": "T2", "DT": "4100"},
        ]

        class F:
            def get(self, source, url, params):
                return json.dumps(rows).encode()

        s = migration.fetch_kosis(F(), "k", {"orgId": "101", "tblId": "T",
                                             "item_in": "T1", "item_out": "T2"},
                                  population=200_000)
        self.assertEqual([p.period for p in s.points], ["2026-06", "2026-07"])
        self.assertEqual(s.points[0].net, 1000)
        self.assertEqual(s.label, "순유입 지속")

    def test_kosis_error_response_raises(self):
        class F:
            def get(self, source, url, params):
                return json.dumps({"err": "20", "errMsg": "인증키 오류"}).encode()

        with self.assertRaises(migration.KosisApiError):
            migration.fetch_kosis(F(), "k", {"item_in": "a", "item_out": "b"})

    def test_kosis_requires_item_codes(self):
        with self.assertRaises(migration.KosisApiError):
            migration.fetch_kosis(None, "k", {"orgId": "101"})


# ── L7 생활이동·O/D ──────────────────────────────────────────────────────────

OD_CSV = """origin,destination,trips,purpose
강남구,강남구,600,출근
강남구,중구,200,출근
강남구,서초구,200,출근
서초구,강남구,300,출근
성남시,강남구,150,출근
부산시,대구시,900,출근
강남구,중구,50,여가
"""

OD_DEPENDENT = """origin,destination,trips
A구,A구,100
A구,도심,500
A구,B구,100
"""


class TestMobility(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, text, name="od.csv"):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def test_self_containment_and_labels(self):
        od = mobility.load(self._write(OD_CSV), focus="강남구", purpose="출근")
        self.assertEqual(od.internal, 600)
        self.assertEqual(od.total_outbound, 400)
        self.assertEqual(od.total_inbound, 450)
        self.assertAlmostEqual(od.self_containment, 0.6)
        self.assertEqual(od.label, "자족형 생활권")
        self.assertEqual(od.net_flow, 50)

    def test_unrelated_flows_excluded(self):
        od = mobility.load(self._write(OD_CSV), focus="강남구", purpose="출근")
        self.assertNotIn("부산시", od.inbound)
        self.assertNotIn("대구시", od.outbound)

    def test_purpose_filter_applied(self):
        allp = mobility.load(self._write(OD_CSV), focus="강남구")
        self.assertEqual(allp.outbound["중구"], 250)     # 출근 200 + 여가 50
        commute = mobility.load(self._write(OD_CSV), focus="강남구", purpose="출근")
        self.assertEqual(commute.outbound["중구"], 200)

    def test_dependent_label(self):
        od = mobility.load(self._write(OD_DEPENDENT), focus="A구")
        self.assertLess(od.self_containment, 0.5)
        self.assertIn("도심 의존", od.label)

    def test_target_regions_from_inbound(self):
        od = mobility.load(self._write(OD_CSV), focus="강남구", purpose="출근")
        self.assertEqual(od.target_regions(2), ["서초구", "성남시"])

    def test_partial_name_matching(self):
        csv = "origin,destination,trips\n서울 강남구,서울 강남구,100\n서초구,서울 강남구,50\n"
        od = mobility.load(self._write(csv), focus="강남구")
        self.assertEqual(od.internal, 100)
        self.assertEqual(od.total_inbound, 50)

    def test_no_match_flagged(self):
        od = mobility.load(self._write(OD_CSV), focus="없는구")
        self.assertIsNone(od.self_containment)
        self.assertTrue(any("0건" in x for x in od.limitations))

    def test_missing_column_raises(self):
        with self.assertRaises(mobility.MobilityFormatError):
            mobility.load(self._write("origin,destination\nA,B\n"), focus="A")

    def test_empty_focus_raises(self):
        with self.assertRaises(mobility.MobilityFormatError):
            mobility.load(self._write(OD_CSV), focus="  ")


# ── L8 교통망·접근성 ─────────────────────────────────────────────────────────

SITE_LAT, SITE_LNG = 37.5000, 127.0000


def _stop(name, lat, lng):
    return {"nodenm": name, "gpslati": lat, "gpslong": lng}


class FakeTagoFetcher:
    def __init__(self, items, code="00", shape="list"):
        self.items = items
        self.code = code
        self.shape = shape

    def get(self, source, url, params):
        if self.shape == "list":
            items = {"item": self.items}
        elif self.shape == "single":
            items = {"item": self.items[0]}
        else:
            items = ""
        return json.dumps({"response": {
            "header": {"resultCode": self.code, "resultMsg": "OK"},
            "body": {"items": items}}}).encode()


STATIONS_CSV = """name,lat,lng,lines
가까운역,37.5010,127.0010,"2호선,분당선"
먼역,37.5150,127.0150,3호선
아주먼역,38.0000,128.0000,9호선
"""


class TestTransit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, text, name="st.csv"):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def test_stops_filtered_by_radius(self):
        f = FakeTagoFetcher([
            _stop("바로앞", 37.5002, 127.0002),      # 약 28m
            _stop("가까움", 37.5015, 127.0015),      # 약 200m
            _stop("멀다", 37.5100, 127.0100),        # 약 1.4km
        ])
        stops = transit.fetch_stops(f, "k", SITE_LAT, SITE_LNG, radius_m=500)
        self.assertEqual([s.name for s in stops], ["바로앞", "가까움"])
        self.assertLess(stops[0].dist_m, stops[1].dist_m)

    def test_single_item_response_shape(self):
        f = FakeTagoFetcher([_stop("단일", 37.5002, 127.0002)], shape="single")
        self.assertEqual(len(transit.fetch_stops(f, "k", SITE_LAT, SITE_LNG)), 1)

    def test_empty_items_shape(self):
        f = FakeTagoFetcher([], shape="empty")
        self.assertEqual(transit.fetch_stops(f, "k", SITE_LAT, SITE_LNG), [])

    def test_api_error_raises(self):
        with self.assertRaises(transit.TransitApiError):
            transit.fetch_stops(FakeTagoFetcher([], code="99"), "k",
                                SITE_LAT, SITE_LNG)

    def test_stations_sorted_and_range_filtered(self):
        st, skipped = transit.load_stations(
            self._write(STATIONS_CSV), SITE_LAT, SITE_LNG)
        self.assertEqual([s.name for s in st], ["가까운역", "먼역"])   # 3km 초과 제외
        self.assertEqual(st[0].lines, ["2호선", "분당선"])
        self.assertEqual(skipped, [])

    def test_station_file_missing_column(self):
        with self.assertRaises(transit.StationFormatError):
            transit.load_stations(self._write("name,lat\n역,37.5\n"),
                                  SITE_LAT, SITE_LNG)

    def test_collect_prime_station_allows_ad_phrase(self):
        acc = transit.collect(
            FakeTagoFetcher([_stop("정류장1", 37.5002, 127.0002),
                             _stop("정류장2", 37.5005, 127.0005),
                             _stop("정류장3", 37.5008, 127.0008)]),
            "k", SITE_LAT, SITE_LNG, radius_m=500,
            stations_file=self._write(STATIONS_CSV))
        self.assertEqual(acc.label, "대중교통 접근 우수")
        self.assertIn("역세권", acc.station_label)
        self.assertIn("도보", acc.ad_note())
        self.assertIn("양호", acc.bus_label)

    def test_collect_far_station_forbids_ad_phrase(self):
        far = "name,lat,lng\n먼역,37.5200,127.0200\n"
        acc = transit.collect(None, None, SITE_LAT, SITE_LNG,
                              stations_file=self._write(far))
        self.assertIn("사용 불가", acc.ad_note())
        self.assertEqual(acc.label, "대중교통 접근 취약")
        self.assertTrue(any("인증키 없음" in x for x in acc.limitations))

    def test_collect_without_stations_cannot_judge(self):
        acc = transit.collect(FakeTagoFetcher([]), "k", SITE_LAT, SITE_LNG)
        self.assertEqual(acc.nearest_station, None)
        self.assertIn("사용 금지", acc.ad_note())
        self.assertTrue(any("역 좌표 파일 미지정" in x for x in acc.limitations))

    def test_detour_limitation_always_present(self):
        acc = transit.collect(None, None, SITE_LAT, SITE_LNG)
        self.assertTrue(any("직선거리" in x for x in acc.limitations))


# ── 파이프라인 통합 ──────────────────────────────────────────────────────────

class TestIntegration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, text, name):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return str(p)

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

    def test_new_layers_appear_in_demand_verdict_and_report(self):
        mig = migration.load(self._write(INFLOW_CSV, "m.csv"), population=300_000)
        od = mobility.load(self._write(OD_CSV, "od.csv"), focus="강남구",
                           purpose="출근")
        acc = transit.collect(
            FakeTagoFetcher([_stop("정류장", 37.5002, 127.0002)]), "k",
            SITE_LAT, SITE_LNG, stations_file=self._write(STATIONS_CSV, "s.csv"))
        res = self._run(migration=mig, mobility=od, transit=acc)
        v2 = next(v for v in res.inputs.verdicts if v.name.startswith("②"))
        joined = " ".join(v2.rationale)
        for token in ("인구이동(L3)", "생활이동·O/D(L7)", "교통 접근성(L8)"):
            self.assertIn(token, joined)
        self.assertIn("광고 타깃 후보", joined)
        self.assertIn("지역 기반 통계", res.markdown)
        self.assertIn("접근성 표현 가능 범위", res.markdown)

    def test_coverage_table_reflects_new_layers(self):
        mig = migration.load(self._write(INFLOW_CSV, "m.csv"))
        res = self._run(migration=mig)
        rows = {r.layer.split()[0]: r for r in res.inputs.coverage_rows}
        self.assertEqual(rows["L3"].coverage.value, "확보 가능")
        self.assertEqual(rows["L7"].coverage.value, "미확보")
        self.assertEqual(rows["L8"].coverage.value, "미확보")

    def test_prime_station_produces_allowed_claim(self):
        acc = transit.collect(None, None, SITE_LAT, SITE_LNG,
                              stations_file=self._write(STATIONS_CSV, "s.csv"))
        res = self._run(transit=acc)
        self.assertIn("도보", res.markdown)
        self.assertIn("가까운역", res.markdown)

    def test_far_station_produces_no_walk_claim(self):
        far = "name,lat,lng\n먼역,37.5200,127.0200\n"
        acc = transit.collect(None, None, SITE_LAT, SITE_LNG,
                              stations_file=self._write(far, "s2.csv"))
        res = self._run(transit=acc)
        self.assertNotIn("먼역까지 도보", res.markdown)
        self.assertIn("사용 불가", res.markdown)

    def test_without_new_layers_report_unchanged(self):
        res = self._run()
        self.assertNotIn("인구이동(L3)", res.markdown)
        self.assertIn("결론 요약", res.markdown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
