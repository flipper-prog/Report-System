"""총취득원가 기준 정합성 테스트.

이 시스템의 가격 판정은 "총취득원가로 비교한다"를 원칙으로 한다. 원칙이
한쪽에만 적용되면 비교가 성립하지 않는다 — 현장만 세금을 얹고 비교 거래는
신고가 그대로 쓰면, 현장이 부대비용만큼 비싸 보이고 판정 ①이 '밴드 상단'
쪽으로 계통적으로 기운다. 여기서 확인할 것은 (1) 세율이 구간 누진과 면적
기준을 지키는가, (2) 비교의 양쪽이 같은 함수를 통과하는가다.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system.acquisition import (BRACKET_HIGH, BRACKET_LOW,
                                       EDU_TAX_MULTIPLIER, FARM_TAX_AREA_M2,
                                       FARM_TAX_RATE, HEAVY_RATE, RATE_HIGH,
                                       RATE_LOW, base_rate, tax_rate,
                                       total_cost, total_ppsm)
from report_system.models import (Comparable, Site, Transaction, TypeSpec,
                                  total_acquisition_cost)
from report_system.pricing import (adjusted_ppsm, market_positions,
                                   quality_adjusted_bands)

ASOF = date(2026, 7, 25)


class TestBaseRate(unittest.TestCase):
    def test_low_bracket(self):
        self.assertEqual(base_rate(500_000_000), RATE_LOW)
        self.assertEqual(base_rate(BRACKET_LOW), RATE_LOW)

    def test_high_bracket(self):
        self.assertEqual(base_rate(BRACKET_HIGH), RATE_HIGH)
        self.assertEqual(base_rate(1_500_000_000), RATE_HIGH)

    def test_middle_bracket_is_progressive(self):
        a = base_rate(650_000_000)
        b = base_rate(800_000_000)
        self.assertLess(RATE_LOW, a)
        self.assertLess(a, b)
        self.assertLess(b, RATE_HIGH)

    def test_continuous_at_boundaries(self):
        self.assertAlmostEqual(base_rate(BRACKET_LOW), base_rate(BRACKET_LOW + 1),
                               places=6)
        self.assertAlmostEqual(base_rate(BRACKET_HIGH - 1), base_rate(BRACKET_HIGH),
                               places=6)

    def test_monotone(self):
        prices = [1e8 * i for i in range(1, 21)]
        rates = [base_rate(p) for p in prices]
        self.assertEqual(rates, sorted(rates))

    def test_zero_and_negative_safe(self):
        self.assertEqual(base_rate(0), 0.0)
        self.assertEqual(base_rate(-1), 0.0)


class TestTaxRate(unittest.TestCase):
    def test_small_apartment_low_price(self):
        # 5억 · 전용 84㎡ → 취득세 1% + 지방교육세 0.1% = 1.1%
        self.assertAlmostEqual(tax_rate(500_000_000, 84.0), 0.011, places=6)

    def test_large_apartment_high_price(self):
        # 10억 · 전용 100㎡ → 3% + 0.3% + 농특세 0.2% = 3.5%
        self.assertAlmostEqual(tax_rate(1_000_000_000, 100.0), 0.035, places=6)

    def test_high_price_small_area_has_no_farm_tax(self):
        self.assertAlmostEqual(tax_rate(1_000_000_000, 84.0), 0.033, places=6)

    def test_farm_tax_boundary_is_exclusive(self):
        at = tax_rate(1_000_000_000, FARM_TAX_AREA_M2)
        over = tax_rate(1_000_000_000, FARM_TAX_AREA_M2 + 0.1)
        self.assertAlmostEqual(over - at, FARM_TAX_RATE, places=6)

    def test_edu_tax_is_proportional_to_base(self):
        r = tax_rate(500_000_000, 60.0)
        self.assertAlmostEqual(r, RATE_LOW * (1 + EDU_TAX_MULTIPLIER), places=6)

    def test_explicit_base_overrides_bracket(self):
        # 다주택 중과 시나리오 — 가액과 무관하게 지정 세율 사용
        self.assertAlmostEqual(tax_rate(300_000_000, 84.0, base=HEAVY_RATE),
                               HEAVY_RATE * (1 + EDU_TAX_MULTIPLIER), places=6)


class TestTotals(unittest.TestCase):
    def test_total_cost_adds_rate(self):
        p = 500_000_000
        self.assertAlmostEqual(total_cost(p, 84.0), p * 1.011, places=2)

    def test_total_ppsm_divides_by_area(self):
        self.assertAlmostEqual(total_ppsm(500_000_000, 84.0),
                               total_cost(500_000_000, 84.0) / 84.0, places=4)

    def test_zero_area_is_safe(self):
        self.assertEqual(total_ppsm(500_000_000, 0.0), 0.0)

    def test_type_spec_uses_progressive_rate(self):
        cheap = TypeSpec("59A", 59.9, 100, 500_000_000)
        rich = TypeSpec("120A", 120.0, 100, 1_200_000_000)
        self.assertAlmostEqual(total_acquisition_cost(cheap) / 500_000_000,
                               1.011, places=4)
        self.assertAlmostEqual(total_acquisition_cost(rich) / 1_200_000_000,
                               1.035, places=4)

    def test_options_are_included_before_tax(self):
        t = TypeSpec("84A", 84.0, 100, 500_000_000, option_cost=20_000_000)
        self.assertEqual(total_acquisition_cost(t),
                         int(total_cost(520_000_000, 84.0)))


class TestComparisonSymmetry(unittest.TestCase):
    """비교의 양쪽이 같은 기준을 통과해야 판정이 성립한다."""

    def _fixture(self, price=500_000_000, area=84.0):
        site = Site(id="S", name="현장", address="a", lat=37.0, lng=127.0,
                    total_units=300,
                    types=[TypeSpec("84A", area, 300, price, 0, (1, 25))],
                    region="R")
        comps = {"C1": Comparable(id="C1", name="비교1", built_year=2026,
                                  units=500, brand_tier=2, dist_m=300)}
        # 밴드에 폭이 있어야 '하단/내/상단'이 구분된다 (±10% 분포)
        txs = [Transaction("C1", date(2026, 3, 5), area, 12,
                           int(price * (0.90 + 0.20 * i / 19)))
               for i in range(20)]
        return site, comps, txs

    def test_identical_price_lands_inside_band_not_above(self):
        """같은 가격·같은 면적의 비교 거래만 있으면 '상단'일 수 없다.

        현장에만 부대비용을 얹던 시절에는 이 상황에서도 '밴드 상단(가격 저항
        위험)'이 나왔다 — 세금만큼 현장이 비싸 보였기 때문이다.
        """
        site, comps, txs = self._fixture()
        bands = quality_adjusted_bands(site, comps, txs, ASOF)
        pos = market_positions(site, bands)[0]
        self.assertEqual(pos.label, "밴드 내")
        # 분포의 중심에 정확히 놓인다 — 세금만큼 밀려 올라가지 않는다
        self.assertAlmostEqual(pos.subject_ppsm / pos.band.q50, 1.0, places=3)

    def test_comparable_ppsm_is_grossed_up(self):
        comp = Comparable(id="C1", name="c", built_year=2026, units=100,
                          brand_tier=2, dist_m=300)
        tx = Transaction("C1", date(2026, 3, 5), 84.0, 12, 500_000_000)
        self.assertAlmostEqual(adjusted_ppsm(tx, comp, ASOF, (1, 25)),
                               total_ppsm(500_000_000, 84.0), delta=1.0)

    def test_same_tax_base_on_both_sides_preserves_position(self):
        # 양쪽에 같은 세율을 적용하면 상대 위치는 변하지 않는다
        site, comps, txs = self._fixture()
        for tb in (None, RATE_LOW, HEAVY_RATE):
            bands = quality_adjusted_bands(site, comps, txs, ASOF, tax_base=tb)
            pos = market_positions(site, bands, tax_base=tb)[0]
            self.assertEqual(pos.label, "밴드 내")

    def test_cheaper_subject_reads_as_lower_band(self):
        site, comps, txs = self._fixture()
        site.types[0].base_price = int(500_000_000 * 0.85)
        bands = quality_adjusted_bands(site, comps, txs, ASOF)
        self.assertEqual(market_positions(site, bands)[0].label,
                         "밴드 하단(가격 경쟁력)")

    def test_pricier_subject_reads_as_upper_band(self):
        site, comps, txs = self._fixture()
        site.types[0].base_price = int(500_000_000 * 1.20)
        bands = quality_adjusted_bands(site, comps, txs, ASOF)
        self.assertEqual(market_positions(site, bands)[0].label,
                         "밴드 상단(가격 저항 위험)")


class TestPipelineWiring(unittest.TestCase):
    def setUp(self):
        from report_system import sample_data as sd
        from report_system.ledger import ForecastLedger
        comps = sd.build_comparables()
        from report_system.pipeline import run
        self.res = run(
            site=sd.build_site(), comps=comps,
            txs=sd.build_transactions(comps),
            sub_history=sd.build_subscription_history(),
            supply_items=sd.build_supply(),
            catalyst_plans_old=sd.build_catalysts(),
            catalyst_plans_new=sd.build_catalysts(),
            dataset_meta=sd.build_dataset_meta(),
            incomes=sd.build_incomes(), feedback=sd.build_feedback(),
            listings=sd.build_listing_snapshots(),
            asof=sd.ASOF, ledger=ForecastLedger())

    def test_report_discloses_applied_rate(self):
        self.assertIn("적용 부대비용률", self.res.markdown)
        self.assertIn("같은 규칙으로", self.res.markdown)

    def test_tax_basis_is_a_robustness_check(self):
        names = {c.assumption for c in self.res.inputs.robustness.checks}
        self.assertIn("취득 부대비용 전제", names)

    def test_symmetric_tax_does_not_flip_price_verdict(self):
        # 양쪽 동일 적용이므로 세율 전제만으로 방향이 바뀌면 안 된다
        for c in self.res.inputs.robustness.checks:
            if c.assumption == "취득 부대비용 전제":
                self.assertFalse(c.flipped, c.change)


if __name__ == "__main__":
    unittest.main()
