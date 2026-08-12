"""doctor(사전 점검) 테스트 — 네트워크 없이 설정·매칭 로직을 검증한다."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system.doctor import (FAIL, OK, WARN, check_config,
                                  match_comparables, summarize)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "site_config.json"


def base_cfg() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


class TestConfigChecks(unittest.TestCase):
    def test_example_config_has_no_failures(self):
        checks = check_config(base_cfg())
        _, _, fail = summarize(checks)
        self.assertEqual(fail, 0, [c.line() for c in checks if c.status == FAIL])

    def test_missing_required_key_fails_fast(self):
        cfg = base_cfg()
        del cfg["lawd_cd"]
        checks = check_config(cfg)
        self.assertTrue(any(c.status == FAIL and "lawd_cd" in c.name for c in checks))

    def test_unit_sum_mismatch_detected(self):
        cfg = base_cfg()
        cfg["site"]["total_units"] += 7
        checks = check_config(cfg)
        self.assertTrue(any(c.name == "세대수 정합" and c.status == FAIL for c in checks))

    def test_price_unit_error_detected(self):
        cfg = base_cfg()
        cfg["site"]["types"][0]["base_price"] = 14_500      # 만원 단위로 잘못 입력
        checks = check_config(cfg)
        self.assertTrue(any("base_price" in c.name and c.status == FAIL for c in checks))

    def test_bad_lawd_code_detected(self):
        cfg = base_cfg()
        cfg["lawd_cd"] = "1168000000"
        checks = check_config(cfg)
        self.assertTrue(any(c.name == "법정동 코드" and c.status == FAIL for c in checks))

    def test_future_asof_warns(self):
        cfg = base_cfg()
        cfg["asof"] = "2099-01-01"
        checks = check_config(cfg)
        self.assertTrue(any(c.name == "분석 기준일" and c.status == WARN for c in checks))

    def test_missing_units_warns_about_liquidity(self):
        cfg = base_cfg()
        for c in cfg["comparables"]:
            c.pop("units", None)
        checks = check_config(cfg)
        self.assertTrue(any("units" in c.name and c.status == WARN for c in checks))

    def test_missing_supply_and_catalysts_warn(self):
        cfg = base_cfg()
        cfg["supply"] = []
        cfg["catalysts"] = []
        checks = check_config(cfg)
        names = {c.name for c in checks if c.status == WARN}
        self.assertIn("공급 파이프라인", names)
        self.assertIn("개발계획", names)


class TestComparableMatching(unittest.TestCase):
    def test_exact_match_ok(self):
        cfg = {"comparables": [{"apt_nm": "표본래미안"}]}
        checks = match_comparables(cfg, ["표본래미안", "표본자이"])
        self.assertEqual(checks[0].status, OK)

    def test_suggests_candidates_on_mismatch(self):
        cfg = {"comparables": [{"apt_nm": "래미안"}]}
        checks = match_comparables(cfg, ["역삼래미안1차", "개포자이"])
        self.assertEqual(checks[0].status, FAIL)
        self.assertIn("역삼래미안1차", checks[0].detail)

    def test_partial_prefix_candidates(self):
        cfg = {"comparables": [{"apt_nm": "표본없는단지"}]}
        checks = match_comparables(cfg, ["표본래미안", "다른단지"])
        self.assertEqual(checks[0].status, FAIL)
        self.assertIn("표본래미안", checks[0].detail)   # 앞 2글자 기반 후보

    def test_no_names_returns_warning(self):
        checks = match_comparables({"comparables": [{"apt_nm": "x"}]}, [])
        self.assertEqual(checks[0].status, WARN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
