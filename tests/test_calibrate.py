"""조정계수 교정 엔진 테스트.

핵심 검증: 효과가 **실제로 존재하는** 합성 데이터에서는 계수를 복원하고,
효과가 없는 데이터에서는 계수를 바꾸지 않는다(노이즈를 학습하지 않는다).
"""
from __future__ import annotations

import json
import math
import random
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system import sample_data as sd
from report_system.calibrate import (AGE_RANGE, CalibrationError, MIN_T,
                                     _drop_constant_columns, _invert,
                                     build_design, calibrate, load, ols, save)
from report_system.models import Comparable, Site, Transaction, TypeSpec
from report_system.pricing import DEFAULT_COEF, Coefficients, adjusted_ppsm

ASOF = date(2026, 7, 25)

# 합성 데이터에 심는 '참값'
TRUE_AGE = 0.020        # 연식 1년당 2% 할인
TRUE_LOW = 0.050        # 저층 5% 할인
TRUE_HIGH = 0.040       # 상층 4% 프리미엄


def _site() -> Site:
    return Site(id="CAL-1", name="교정 테스트", address="주소", lat=37.5, lng=127.0,
                total_units=400,
                types=[TypeSpec("84A", 84.9, 400, 1_000_000_000, 0, (1, 25))],
                expected_movein=date(2028, 9, 1), region="테스트시")


def _comps() -> dict[str, Comparable]:
    spec = [("A", 2005, 400, 1, False), ("B", 2011, 900, 2, False),
            ("C", 2017, 1400, 1, False), ("D", 2021, 700, 2, True)]
    return {cid: Comparable(id=cid, name=f"단지{cid}", built_year=by, units=500,
                            brand_tier=bt, dist_m=d, region="테스트시",
                            is_presale_right=pre)
            for cid, by, d, bt, pre in spec}


def _txs(comps, n_per=180, noise=0.02, seed=11,
         age_eff=TRUE_AGE, low_eff=TRUE_LOW, high_eff=TRUE_HIGH):
    """참값을 곱셈으로 심은 거래 표본 — 로그선형 모형과 정확히 대응한다."""
    rng = random.Random(seed)
    out: list[Transaction] = []
    for cid, c in comps.items():
        for _ in range(n_per):
            d = ASOF - timedelta(days=rng.randint(1, 36 * 30))
            floor = rng.randint(1, 25)
            area = rng.choice([79.9, 84.9, 89.9])
            age = d.year - c.built_year
            ppsm = 10_000_000.0
            ppsm *= math.exp(-age_eff * age)
            if floor <= 5:
                ppsm *= math.exp(-low_eff)
            elif floor >= 21:
                ppsm *= math.exp(high_eff)
            ppsm *= math.exp(rng.gauss(0, noise))
            out.append(Transaction(cid, d, area, floor, int(ppsm * area)))
    return out


class TestLinearAlgebra(unittest.TestCase):
    def test_invert_identity(self):
        inv = _invert([[2.0, 0.0], [0.0, 4.0]])
        self.assertAlmostEqual(inv[0][0], 0.5)
        self.assertAlmostEqual(inv[1][1], 0.25)

    def test_singular_matrix_raises(self):
        with self.assertRaises(CalibrationError):
            _invert([[1.0, 2.0], [2.0, 4.0]])

    def test_ols_recovers_known_line(self):
        # y = 3 + 2x 정확히
        X = [[1.0, float(i)] for i in range(20)]
        y = [3 + 2 * i for i in range(20)]
        fit = ols(X, y, ["const", "x"])
        self.assertAlmostEqual(fit.beta[0], 3.0, places=6)
        self.assertAlmostEqual(fit.beta[1], 2.0, places=6)
        self.assertAlmostEqual(fit.r2, 1.0, places=9)

    def test_ols_rejects_underdetermined(self):
        with self.assertRaises(CalibrationError):
            ols([[1.0, 2.0]], [1.0], ["a", "b"])


class TestDesignMatrix(unittest.TestCase):
    def test_columns_and_rows(self):
        comps = _comps()
        X, y, names = build_design(_txs(comps, n_per=5), comps)
        self.assertEqual(len(X), 20)
        self.assertEqual(len(names), len(X[0]))
        self.assertIn("age", names)
        self.assertIn("presale", names)

    def test_transactions_without_comp_meta_dropped(self):
        comps = _comps()
        txs = _txs(comps, n_per=3)
        txs.append(Transaction("UNKNOWN", ASOF, 84.9, 5, 900_000_000))
        X, _, _ = build_design(txs, comps)
        self.assertEqual(len(X), 12)

    def test_no_usable_rows_raises(self):
        with self.assertRaises(CalibrationError):
            build_design([Transaction("X", ASOF, 84.9, 5, 1)], {})

    def test_constant_column_dropped(self):
        X = [[1.0, 5.0, float(i)] for i in range(10)]
        Xk, names, dropped = _drop_constant_columns(X, ["const", "flat", "vary"])
        self.assertEqual(dropped, ["flat"])
        self.assertEqual(names, ["const", "vary"])
        self.assertEqual(len(Xk[0]), 2)


class TestRecovery(unittest.TestCase):
    """참값이 심긴 데이터에서 계수를 복원하는가."""

    def setUp(self):
        self.comps = _comps()
        self.site = _site()
        self.txs = _txs(self.comps)

    def test_age_coefficient_recovered(self):
        res = calibrate(self.site, self.comps, self.txs, ASOF)
        age = next(c for c in res.checks if c.name == "age")
        self.assertTrue(age.accepted, age.reason)
        self.assertAlmostEqual(age.estimate, TRUE_AGE, delta=0.005)
        self.assertGreater(abs(age.t), MIN_T)

    def test_floor_coefficients_recovered_with_correct_signs(self):
        res = calibrate(self.site, self.comps, self.txs, ASOF)
        low = next(c for c in res.checks if c.name == "floor_low")
        high = next(c for c in res.checks if c.name == "floor_high")
        self.assertTrue(low.accepted, low.reason)
        self.assertTrue(high.accepted, high.reason)
        self.assertGreater(low.estimate, 0)      # 저층은 상향 조정
        self.assertLess(high.estimate, 0)        # 상층은 하향 조정
        # 층 구간 경계가 백분위 기반이라 참값보다 약하게 추정되는 것은 정상
        self.assertGreater(low.estimate, TRUE_LOW * 0.5)
        self.assertLess(high.estimate, -TRUE_HIGH * 0.5)

    def test_candidate_differs_from_default(self):
        res = calibrate(self.site, self.comps, self.txs, ASOF)
        self.assertNotEqual(res.candidate.age_per_year, DEFAULT_COEF.age_per_year)
        self.assertIn("실데이터 교정", res.candidate.source)

    def test_regression_fit_quality_reported(self):
        res = calibrate(self.site, self.comps, self.txs, ASOF)
        self.assertGreater(res.fit.r2, 0.8)
        self.assertGreater(res.fit.n, 500)

    def test_backtest_comparison_runs(self):
        res = calibrate(self.site, self.comps, self.txs, ASOF)
        self.assertIsNotNone(res.coverage_before)
        self.assertIsNotNone(res.coverage_after)
        # 채택 여부와 무관하게 판정 문구가 두 적중률을 모두 밝혀야 한다
        self.assertTrue(res.decision.startswith("교정 "))

    def test_markdown_report_is_wellformed(self):
        md = calibrate(self.site, self.comps, self.txs, ASOF).as_markdown()
        self.assertIn("조정계수 교정 보고서", md)
        self.assertIn("계수별 게이트", md)
        self.assertIn("홀드아웃 검증", md)
        for line in md.splitlines():          # 표 셀 안에 파이프가 새지 않았는가
            if line.startswith("|") and line.endswith("|"):
                self.assertNotIn("|t|", line)


class TestRejection(unittest.TestCase):
    """효과가 없는 데이터에서 계수를 바꾸지 않는가."""

    def test_no_effect_data_keeps_defaults(self):
        comps = _comps()
        txs = _txs(comps, age_eff=0.0, low_eff=0.0, high_eff=0.0, noise=0.05)
        res = calibrate(_site(), comps, txs, ASOF)
        self.assertEqual(res.adopted.as_dict(), DEFAULT_COEF.as_dict())
        self.assertFalse(res.changed)

    def test_wrong_sign_effect_rejected(self):
        """구축이 더 비싼(부호가 뒤집힌) 데이터는 납득 범위 밖으로 기각."""
        comps = _comps()
        txs = _txs(comps, age_eff=-0.03, low_eff=0.0, high_eff=0.0, noise=0.02)
        res = calibrate(_site(), comps, txs, ASOF)
        age = next(c for c in res.checks if c.name == "age")
        self.assertFalse(age.accepted)
        self.assertIn("납득 범위", age.reason)
        self.assertEqual(res.adopted.age_per_year, DEFAULT_COEF.age_per_year)

    def test_implausibly_large_effect_rejected(self):
        comps = _comps()
        txs = _txs(comps, age_eff=0.12, low_eff=0.0, high_eff=0.0, noise=0.01)
        res = calibrate(_site(), comps, txs, ASOF)
        age = next(c for c in res.checks if c.name == "age")
        self.assertFalse(age.accepted)
        self.assertGreater(age.estimate, AGE_RANGE[1])

    def test_small_sample_skips_calibration(self):
        comps = _comps()
        res = calibrate(_site(), comps, _txs(comps, n_per=5), ASOF)
        self.assertIsNone(res.fit)
        self.assertIn("교정 미실시", res.decision)
        self.assertEqual(res.adopted.as_dict(), DEFAULT_COEF.as_dict())

    def test_synthetic_sample_data_is_not_overfit(self):
        """저장소 샘플 데이터에는 연식·층 효과가 없으므로 교정되지 않아야 한다."""
        comps = sd.build_comparables()
        res = calibrate(sd.build_site(), comps, sd.build_transactions(comps),
                        sd.ASOF)
        self.assertFalse(res.changed)


class TestCoefficientPlumbing(unittest.TestCase):
    def test_adjustment_uses_supplied_coefficients(self):
        comp = Comparable(id="C", name="c", built_year=2016, units=100,
                          brand_tier=2, dist_m=500, region="r")
        tx = Transaction("C", ASOF, 84.9, 3, 849_000_000)   # ppsm 1,000만
        base = adjusted_ppsm(tx, comp, ASOF, (1, 25))
        strong = adjusted_ppsm(tx, comp, ASOF, (1, 25),
                               Coefficients(age_per_year=0.05, floor_low=0.10))
        self.assertGreater(strong, base)

    def test_age_cap_still_applies(self):
        comp = Comparable(id="C", name="c", built_year=1970, units=100,
                          brand_tier=2, dist_m=500, region="r")
        tx = Transaction("C", ASOF, 84.9, 12, 849_000_000)
        v = adjusted_ppsm(tx, comp, ASOF, (1, 25),
                          Coefficients(age_per_year=0.05, age_cap=0.20))
        # 비교 거래도 총취득원가 기준으로 환산된 뒤 조정된다 (acquisition.py)
        from report_system.acquisition import total_ppsm
        self.assertAlmostEqual(v, total_ppsm(849_000_000, 84.9) * math.exp(0.20),
                               delta=1_000)

    def test_age_measured_at_trade_date_not_asof(self):
        """같은 단지의 거래는 언제 체결됐든 조정 후 같은 값이 되어야 한다.

        연식을 기준일로 재면 오래된 거래일수록 조정값이 커져 같은 단지 안에서도
        계통적으로 어긋난다. 시간 경과분은 시점수정이 따로 담당한다.
        """
        comp = Comparable(id="C", name="c", built_year=2010, units=100,
                          brand_tier=2, dist_m=500, region="r")
        coef = Coefficients(age_per_year=0.02, age_cap=0.8)
        old = Transaction("C", date(2023, 7, 1), 84.9, 12, 849_000_000)
        new = Transaction("C", date(2026, 7, 1), 84.9, 12, 849_000_000)
        # 같은 명목가라면 오래된 거래의 연식이 더 낮으므로 조정값이 더 작다
        self.assertLess(adjusted_ppsm(old, comp, ASOF, (1, 25), coef),
                        adjusted_ppsm(new, comp, ASOF, (1, 25), coef))

    def test_time_adjustment_lifts_older_transactions(self):
        comp = Comparable(id="C", name="c", built_year=2016, units=100,
                          brand_tier=2, dist_m=500, region="r")
        tx = Transaction("C", date(2024, 7, 25), 84.9, 12, 849_000_000)
        flat = Coefficients(time_per_year=0.0)
        rising = Coefficients(time_per_year=0.05, time_cap=0.5)
        # 2년 전 거래 → exp(0.05×2) ≈ 1.105배
        self.assertAlmostEqual(
            adjusted_ppsm(tx, comp, ASOF, (1, 25), rising)
            / adjusted_ppsm(tx, comp, ASOF, (1, 25), flat),
            math.exp(0.10), delta=0.002)

    def test_time_adjustment_respects_cap(self):
        comp = Comparable(id="C", name="c", built_year=2016, units=100,
                          brand_tier=2, dist_m=500, region="r")
        tx = Transaction("C", date(2016, 7, 25), 84.9, 12, 849_000_000)   # 10년 전
        capped = Coefficients(time_per_year=0.10, time_cap=0.20)
        flat = Coefficients(time_per_year=0.0)
        self.assertAlmostEqual(
            adjusted_ppsm(tx, comp, ASOF, (1, 25), capped)
            / adjusted_ppsm(tx, comp, ASOF, (1, 25), flat),
            math.exp(0.20), delta=0.002)

    def test_calibrated_age_cap_scales_with_rate(self):
        """상한을 고정하면 계수가 클수록 오래된 단지들이 모두 상한에 걸려 뭉개진다."""
        comps = _comps()
        res = calibrate(_site(), comps, _txs(comps), ASOF)
        self.assertTrue(res.changed)
        self.assertGreater(res.adopted.age_cap, DEFAULT_COEF.age_cap)
        self.assertAlmostEqual(res.adopted.age_cap,
                               res.adopted.age_per_year * 30, places=6)

    def test_holdout_improvement_drives_adoption(self):
        comps = _comps()
        res = calibrate(_site(), comps, _txs(comps), ASOF)
        self.assertGreater(res.improvement, 0.5)
        self.assertIn("교정 채택", res.decision)
        # 적중률도 명목에서 멀어지지 않아야 한다(안전장치)
        self.assertLessEqual(abs(res.coverage_after - res.nominal),
                             abs(res.coverage_before - res.nominal) + 0.05)

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = str(Path(tmp) / "c.json")
            c = Coefficients(age_per_year=0.021, age_cap=0.2, floor_low=0.047,
                             floor_high=-0.038, time_per_year=0.03,
                             time_cap=0.1, source="테스트 교정")
            save(c, p)
            back = load(p)
            self.assertEqual(back.as_dict(), c.as_dict())

    def test_load_missing_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load(str(Path(tmp) / "none.json")).as_dict(),
                             DEFAULT_COEF.as_dict())

    def test_load_partial_file_fills_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.json"
            p.write_text(json.dumps({"age_per_year": 0.03}), encoding="utf-8")
            c = load(str(p))
            self.assertEqual(c.age_per_year, 0.03)
            self.assertEqual(c.floor_low, DEFAULT_COEF.floor_low)

    def test_model_card_declares_coefficient_source(self):
        from report_system.modelcard import price_band_card
        card = price_band_card(
            100, "2024 ~ 2026", None,
            Coefficients(age_per_year=0.02, floor_low=0.05, floor_high=-0.04,
                         source="실데이터 교정 (2026-07-25)"))
        self.assertTrue(any("실데이터 교정" in x for x in card.known_limits))
        default_card = price_band_card(100, "2024 ~ 2026", None, DEFAULT_COEF)
        self.assertTrue(any("예시값" in x for x in default_card.known_limits))

    def test_pipeline_accepts_coefficients(self):
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
                  incomes=sd.build_incomes(), feedback=sd.build_feedback(),
                  listings=sd.build_listing_snapshots(),
                  asof=sd.ASOF, ledger=ForecastLedger(),
                  coef=Coefficients(age_per_year=0.02, floor_low=0.05,
                                    floor_high=-0.04,
                                    source="실데이터 교정 (테스트)"))
        self.assertIn("실데이터 교정", res.markdown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
