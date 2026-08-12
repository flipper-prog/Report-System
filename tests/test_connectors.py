"""커넥터 단위 테스트 — 네트워크 없이 픽스처로 파싱·캐시·오류 경로를 검증한다."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system.connectors import applyhome, molit
from report_system.connectors.base import Fetcher
from report_system.subscription import predict
from report_system.models import SubscriptionRecord

# ── 픽스처 ───────────────────────────────────────────────────────────────────

MOLIT_XML_NEW = """<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>000</resultCode><resultMsg>OK</resultMsg></header>
<body><items>
<item><aptNm>테스트래미안</aptNm><dealAmount>145,000</dealAmount>
<excluUseAr>84.97</excluUseAr><floor>12</floor>
<dealYear>2026</dealYear><dealMonth>6</dealMonth><dealDay>15</dealDay>
<buildYear>2019</buildYear><umdNm>역삼동</umdNm></item>
<item><aptNm>테스트래미안</aptNm><dealAmount>151,000</dealAmount>
<excluUseAr>84.97</excluUseAr><floor>20</floor>
<dealYear>2026</dealYear><dealMonth>6</dealMonth><dealDay>20</dealDay>
<buildYear>2019</buildYear><umdNm>역삼동</umdNm><cdealType>O</cdealType></item>
</items><numOfRows>500</numOfRows><pageNo>1</pageNo><totalCount>2</totalCount></body></response>"""

MOLIT_XML_LEGACY = """<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>00</resultCode></header><body><items>
<item><아파트>구형단지</아파트>
<거래금액> 98,500</거래금액>
<전용면적>59.9</전용면적>
<층>7</층>
<년>2026</년><월>5</월><일>3</일>
<건축년도>2016</건축년도>
</item></items><totalCount>1</totalCount></body></response>"""

MOLIT_XML_ERROR = """<?xml version="1.0" encoding="UTF-8"?>
<OpenAPI_ServiceResponse><cmmMsgHeader>
<errMsg>SERVICE ERROR</errMsg>
<returnAuthMsg>등록되지 않은 서비스키</returnAuthMsg>
<returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"""

APPLY_DETAIL = """{"page":1,"perPage":500,"totalCount":2,"data":[
 {"HOUSE_MANAGE_NO":"2026000101","PBLANC_NO":"2026000101","HOUSE_NM":"테스트푸르지오",
  "SUBSCRPT_AREA_CODE_NM":"서울","RCEPT_BGNDE":"2026-03-10"},
 {"HOUSE_MANAGE_NO":"2026000202","PBLANC_NO":"2026000202","HOUSE_NM":"지방단지",
  "SUBSCRPT_AREA_CODE_NM":"경북","RCEPT_BGNDE":"2026-04-01"}]}"""

APPLY_CMPET = """{"page":1,"perPage":500,"totalCount":3,"data":[
 {"HOUSE_MANAGE_NO":"2026000101","PBLANC_NO":"2026000101","HOUSE_TY":"084.97A",
  "SUPLY_HSHLDCO":120,"REQ_CNT":1440,"CMPET_RATE":"12.0"},
 {"HOUSE_MANAGE_NO":"2026000101","PBLANC_NO":"2026000101","HOUSE_TY":"059.98B",
  "SUPLY_HSHLDCO":80,"CMPET_RATE":"(△5)"},
 {"HOUSE_MANAGE_NO":"2026000202","PBLANC_NO":"2026000202","HOUSE_TY":"084.90",
  "SUPLY_HSHLDCO":200,"REQ_CNT":90,"CMPET_RATE":"0.45"}]}"""


class TestMolitParsing(unittest.TestCase):
    def test_new_format_and_cancel_flag(self):
        rows, total = molit.parse_response(MOLIT_XML_NEW.encode())
        self.assertEqual(total, 2)
        self.assertEqual(rows[0].price_won, 1_450_000_000)
        self.assertEqual(rows[0].deal_date, date(2026, 6, 15))
        self.assertFalse(rows[0].canceled)
        self.assertTrue(rows[1].canceled)

    def test_legacy_korean_tags(self):
        rows, total = molit.parse_response(MOLIT_XML_LEGACY.encode())
        self.assertEqual(total, 1)
        self.assertEqual(rows[0].apt_nm, "구형단지")
        self.assertEqual(rows[0].price_won, 985_000_000)
        self.assertEqual(rows[0].build_year, 2016)

    def test_error_response_raises(self):
        with self.assertRaises(molit.MolitApiError):
            molit.parse_response(MOLIT_XML_ERROR.encode())

    def test_to_transactions_filters_by_config(self):
        rows, _ = molit.parse_response(MOLIT_XML_NEW.encode())
        txs = molit.to_transactions(rows, {"테스트래미안": "C00"})
        self.assertEqual(len(txs), 2)
        self.assertEqual(molit.to_transactions(rows, {"없는단지": "C00"}), [])

    def test_build_comparables_uses_real_build_year(self):
        rows, _ = molit.parse_response(MOLIT_XML_NEW.encode())
        comps = molit.build_comparables(rows, [{"apt_nm": "테스트래미안", "dist_m": 500}])
        self.assertEqual(list(comps.values())[0].built_year, 2019)


class TestApplyhomeParsing(unittest.TestCase):
    def _fetch(self):
        class FakeFetcher:
            def __init__(self):
                self.calls = 0
            def get(self, source, url, params):
                if "Detail" in url:
                    return APPLY_DETAIL.encode()
                return APPLY_CMPET.encode()
        return FakeFetcher()

    def test_join_filter_and_shortfall(self):
        recs = applyhome.fetch_subscription_history(
            self._fetch(), "key", region_names=["서울"],
            since=date(2026, 1, 1), until=date(2026, 7, 1))
        # 서울 공고 1건 × 타입 2건 (경북은 지역 필터로 제외)
        self.assertEqual(len(recs), 2)
        t84 = next(r for r in recs if "084" in r.complex_id)
        self.assertEqual(t84.applicants, 1440)
        self.assertAlmostEqual(t84.competition_rate, 12.0)
        self.assertTrue(t84.sold_out_in_order)
        t59 = next(r for r in recs if "059" in r.complex_id)
        self.assertFalse(t59.sold_out_in_order)      # (△5) → 미달
        self.assertIsNone(t59.price_gap_pct)          # API 미제공 필드

    def test_none_fields_do_not_break_matching(self):
        recs = [SubscriptionRecord(f"H{i}", date(2026, 3, 1), 100, 100 * (i + 1),
                                   "서울", None, None, True) for i in range(6)]
        fc = predict(recs, "서울", price_gap_pct=5.0, concurrent_supply=1000)
        self.assertTrue(fc.ok)
        self.assertGreaterEqual(fc.n_cases, 5)


class TestFetcherCache(unittest.TestCase):
    def test_cache_hit_and_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fetcher(cache_dir=tmp, offline=False)
            served = {"n": 0}
            def fake(url):
                served["n"] += 1
                return b"BODY"
            f._fetch_with_retry = fake  # type: ignore[method-assign]
            b1 = f.get("src", "https://x.example/api", {"a": 1})
            b2 = f.get("src", "https://x.example/api", {"a": 1})
            self.assertEqual((b1, b2, served["n"]), (b"BODY", b"BODY", 1))
            self.assertTrue(f.provenance[1].from_cache)
            # offline 모드에서 캐시에 있는 요청은 성공, 없는 요청은 실패
            f2 = Fetcher(cache_dir=tmp, offline=True)
            self.assertEqual(f2.get("src", "https://x.example/api", {"a": 1}), b"BODY")
            with self.assertRaises(RuntimeError):
                f2.get("src", "https://x.example/api", {"a": 2})


if __name__ == "__main__":
    unittest.main(verbosity=2)
