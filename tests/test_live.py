"""실데이터 파이프라인(run_live) 통합 테스트.

네트워크 없이 Fetcher 캐시를 실제 API 응답 형식으로 미리 채운 뒤
offline 모드로 전 구간(수집 → 정제 → 분석 → 판정 → 봉인 → 리포트)을 검증한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system.connectors import applyhome, molit
from report_system.live import run_live

KEY = "TEST-KEY-1234"
ASOF = date(2026, 7, 25)
LAWD = "11680"
MONTHS = 2


def cache_path(cache_dir: Path, url: str, params: dict) -> Path:
    qs = urllib.parse.urlencode(params, safe=":")
    digest = hashlib.sha256(f"{url}?{qs}".encode()).hexdigest()[:24]
    return cache_dir / f"{digest}.bin"


def molit_xml(items: list[dict], total: int) -> bytes:
    body = "".join(
        f"<item><aptNm>{i['apt']}</aptNm><dealAmount>{i['amt']:,}</dealAmount>"
        f"<excluUseAr>{i['area']}</excluUseAr><floor>{i['floor']}</floor>"
        f"<dealYear>{i['y']}</dealYear><dealMonth>{i['m']}</dealMonth>"
        f"<dealDay>{i['d']}</dealDay><buildYear>{i['build']}</buildYear>"
        f"<umdNm>역삼동</umdNm></item>" for i in items)
    return (f'<?xml version="1.0" encoding="UTF-8"?><response>'
            f"<header><resultCode>000</resultCode></header><body><items>{body}</items>"
            f"<totalCount>{total}</totalCount></body></response>").encode()


def build_month_items(y: int, m: int) -> list[dict]:
    """단지별 12건 × 2단지 — 밴드 산출에 충분한 표본."""
    out = []
    for day in range(1, 13):
        out.append({"apt": "표본래미안", "amt": 145_000 + day * 300, "area": 84.97,
                    "floor": 3 + day, "y": y, "m": m, "d": day, "build": 2019})
        out.append({"apt": "표본자이", "amt": 138_000 + day * 250, "area": 59.98,
                    "floor": 2 + day, "y": y, "m": m, "d": day, "build": 2016})
    return out


def applyhome_payloads() -> tuple[bytes, bytes]:
    details, cmpets = [], []
    for i in range(9):
        mno = f"20260001{i:02d}"
        details.append({"HOUSE_MANAGE_NO": mno, "PBLANC_NO": mno,
                        "HOUSE_NM": f"표본단지{i}", "SUBSCRPT_AREA_CODE_NM": "서울",
                        "RCEPT_BGNDE": f"2026-0{(i % 6) + 1}-10"})
        cmpets.append({"HOUSE_MANAGE_NO": mno, "PBLANC_NO": mno,
                       "HOUSE_TY": "084.97A", "SUPLY_HSHLDCO": 100,
                       "REQ_CNT": 100 * (i + 2), "CMPET_RATE": f"{i + 2}.0"})
    wrap = lambda rows: json.dumps(
        {"page": 1, "perPage": 500, "totalCount": len(rows), "data": rows},
        ensure_ascii=False).encode()
    return wrap(details), wrap(cmpets)


def seed_cache(cache_dir: Path) -> None:
    # E01 실거래: asof 월부터 과거 MONTHS개월
    y, m = ASOF.year, ASOF.month
    for _ in range(MONTHS):
        items = build_month_items(y, m)
        p = cache_path(cache_dir, molit.URL, {
            "serviceKey": KEY, "LAWD_CD": LAWD, "DEAL_YMD": f"{y}{m:02d}",
            "pageNo": 1, "numOfRows": molit.PAGE_SIZE})
        p.write_bytes(molit_xml(items, len(items)))
        m -= 1
        if m == 0:
            y, m = y - 1, 12

    # E02 청약: 상세 + 경쟁률
    det, cmp_ = applyhome_payloads()
    cache_path(cache_dir, applyhome.DETAIL_URL,
               {"page": 1, "perPage": applyhome.PER_PAGE, "serviceKey": KEY}).write_bytes(det)
    cache_path(cache_dir, applyhome.CMPET_URL,
               {"page": 1, "perPage": applyhome.PER_PAGE, "serviceKey": KEY}).write_bytes(cmp_)


CONFIG = {
    "asof": ASOF.isoformat(),
    "lawd_cd": LAWD,
    "months": MONTHS,
    "subscription_regions": ["서울"],
    "subscription_lookback_days": 900,
    "site": {
        "id": "LIVE-TEST", "name": "테스트 현장", "address": "서울 강남구 테스트로 1",
        "lat": 37.5, "lng": 127.0, "total_units": 300, "region": "서울",
        "expected_movein": "2028-09-01",
        "types": [
            {"name": "59A", "area_m2": 59.9, "units": 120,
             "base_price": 1_180_000_000, "option_cost": 20_000_000, "floors": [1, 25]},
            {"name": "84A", "area_m2": 84.9, "units": 180,
             "base_price": 1_620_000_000, "option_cost": 30_000_000, "floors": [1, 25]},
        ],
    },
    "comparables": [
        {"apt_nm": "표본래미안", "dist_m": 600, "brand_tier": 1},
        {"apt_nm": "표본자이", "dist_m": 1100, "brand_tier": 1},
    ],
    "supply": [{"name": "인근 A", "units": 900, "stage": "입주예정", "months_to_movein": 10}],
    "catalysts": [{
        "id": "K-1", "name": "테스트선 연장", "stage": "설계·인가",
        "budget_total": 1_000_000_000_000, "budget_secured": 250_000_000_000,
        "dist_m": 700, "time_saving_min": 12,
        "negatives": ["공사 소음"], "source_docs": ["고시 예시"],
    }],
    "income_model": {"median": 78_000_000, "sigma": 0.45, "n": 300, "seed": 7},
    "feedback": {"total_consults": 40,
                 "rejections": {"가격": 20, "경쟁현장": 12, "대출": 8},
                 "visitor_home_regions": {"서울": 30, "경기": 10}},
}


class TestLivePipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        seed_cache(self.cache)
        self.cfg_path = self.root / "config.json"
        self.cfg_path.write_text(json.dumps(CONFIG, ensure_ascii=False), encoding="utf-8")
        self._prev_key = os.environ.get("DATA_GO_KR_API_KEY")
        os.environ["DATA_GO_KR_API_KEY"] = KEY
        self._cwd = os.getcwd()
        os.chdir(self.root)          # provenance.json 이 임시 디렉터리에 쓰이도록

    def tearDown(self):
        os.chdir(self._cwd)
        if self._prev_key is None:
            os.environ.pop("DATA_GO_KR_API_KEY", None)
        else:
            os.environ["DATA_GO_KR_API_KEY"] = self._prev_key
        self.tmp.cleanup()

    def _run(self):
        return run_live(str(self.cfg_path), offline=True,
                        cache_dir=str(self.cache),
                        ledger_path=str(self.root / "ledger.db"))

    def test_end_to_end_from_api_payloads(self):
        result = self._run()
        md = result.markdown

        # 리포트 골격
        for section in ["결론 요약", "데이터 커버리지", "거래 데이터 정제",
                        "품질조정 가격 밴드", "실부담 시뮬레이션", "청약 수요 전망",
                        "확률조정 공급", "촉매카드", "표현 린트"]:
            self.assertIn(section, md)

        # 실거래가 실제로 밴드 산출에 쓰였는가 (2개월 × 24건)
        self.assertEqual(sum(result.inputs.clean.summary.values()) -
                         result.inputs.clean.summary["사용"], 0)  # 오염 없음
        self.assertEqual(result.inputs.clean.summary["사용"], 48)
        self.assertTrue(result.inputs.bands)
        self.assertTrue(result.inputs.positions)

        # 청약 전망이 산출되고 장부에 봉인되었는가
        self.assertTrue(result.inputs.sub_forecast.ok, result.inputs.sub_forecast.reason)
        self.assertTrue(result.forecast_id)

        # 커버리지 표기: 매물 커넥터 미구현은 D등급으로 노출
        self.assertIn("매물·호가 (미수집)", md)

        # 수집 이력 기록
        prov = json.loads((self.root / "out" / "provenance.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(prov), MONTHS + 2)
        self.assertTrue(all(p["from_cache"] for p in prov))
        self.assertTrue(all("***KEY***" in p["url"] for p in prov))
        self.assertNotIn(KEY, json.dumps(prov))       # 인증키 유출 없음

    def test_config_name_mismatch_gives_actionable_error(self):
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        cfg["comparables"] = [{"apt_nm": "존재하지않는단지", "dist_m": 500}]
        self.cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            self._run()
        msg = str(ctx.exception)
        self.assertIn("비교단지 거래 0건", msg)
        self.assertIn("표본래미안", msg)      # 실제 단지명 후보 안내

    def test_offline_requires_seeded_cache(self):
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        cfg["months"] = MONTHS + 1        # 캐시에 없는 월 요청
        self.cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            self._run()
        self.assertIn("offline", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
