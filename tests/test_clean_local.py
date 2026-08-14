"""정제 기준의 시점 중립성 테스트.

이상치·특수거래 판정을 **전 기간 중위값** 기준으로 하면 필터의 효과가 시점과
상관된다. 상승장에서는 최근의 진짜 특수거래가 전 기간 중위 대비로는 기준을
넘어 살아남고, 하락장에서는 정상적인 최근 저가 거래가 통째로 지워진다. 후자가
특히 위험하다 — 분석이 하락 신호를 담은 근거를 스스로 삭제하는 것이기 때문이다.

여기서 확인할 것: 같은 시기의 같은 단지·면적대와만 비교하는가.
"""
from __future__ import annotations

import random
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report_system.models import Transaction
from report_system.transactions import (LOCAL_WINDOW_MONTHS, MIN_LOCAL_SAMPLES,
                                        RULES_VERSION, clean)

START = date(2023, 8, 1)


def _series(annual_factor: float, months: int = 36, per_month: int = 8,
            special_months: tuple[int, ...] = (), seed: int = 11):
    """월별 추세 + 잡음. special_months 에는 동시기 대비 50% 거래를 심는다."""
    rng = random.Random(seed)
    txs: list[Transaction] = []
    flags: dict[int, str] = {}
    for i in range(months):
        d = START + timedelta(days=30 * i)
        level = 10_000_000.0 * (annual_factor ** (i / 12))
        for _ in range(per_month):
            t = Transaction("C1", d, 84.0, 10,
                            int(level * (1 + rng.gauss(0, 0.06)) * 84))
            txs.append(t)
            flags[id(t)] = "정상"
        if i in special_months:
            t = Transaction("C1", d, 84.0, 10, int(level * 0.5 * 84))
            txs.append(t)
            flags[id(t)] = "특수"
    return txs, flags


def _removed_ids(cr) -> set[int]:
    return {id(t) for v in cr.removed.values() for t in v}


class TestRisingMarket(unittest.TestCase):
    """상승장 — 최근의 특수거래를 놓치지 않아야 한다."""

    def test_all_planted_specials_caught(self):
        txs, flags = _series(1.18, special_months=(5, 18, 33))
        rm = _removed_ids(clean(txs))
        caught = [t for t in txs if flags[id(t)] == "특수" and id(t) in rm]
        self.assertEqual(len(caught), 3)

    def test_recent_special_is_caught(self):
        """전 기간 중위 기준이었다면 살아남던 케이스."""
        txs, flags = _series(1.18, special_months=(33,))
        rm = _removed_ids(clean(txs))
        special = next(t for t in txs if flags[id(t)] == "특수")
        self.assertIn(id(special), rm)

    def test_no_normal_trades_removed(self):
        txs, flags = _series(1.18, special_months=(5, 18, 33))
        rm = _removed_ids(clean(txs))
        wrong = [t for t in txs if flags[id(t)] == "정상" and id(t) in rm]
        self.assertEqual(wrong, [])


class TestFallingMarket(unittest.TestCase):
    """하락장 — 하락 신호를 담은 정상 거래를 지우지 않아야 한다."""

    def test_steep_decline_keeps_recent_trades(self):
        txs, _ = _series(0.62)          # 연 -38%
        cr = clean(txs)
        self.assertEqual(cr.removed["특수 의심"], [])

    def test_moderate_decline_keeps_recent_trades(self):
        txs, _ = _series(0.70)          # 연 -30%
        self.assertEqual(clean(txs).removed["특수 의심"], [])

    def test_trend_is_preserved_after_cleaning(self):
        from report_system.timeseries import monthly_trend
        txs, _ = _series(0.62)
        cr = clean(txs)
        self.assertLess(monthly_trend(cr.kept).slope_pct_per_year, -20.0)

    def test_special_in_falling_market_still_caught(self):
        txs, flags = _series(0.70, special_months=(30,))
        rm = _removed_ids(clean(txs))
        special = next(t for t in txs if flags[id(t)] == "특수")
        self.assertIn(id(special), rm)


class TestFallback(unittest.TestCase):
    """동시기 표본이 얕으면 국소 기준이 오히려 불안정하다."""

    def test_sparse_group_falls_back_and_says_so(self):
        # 24개월에 걸쳐 월 1건 — 창(±6개월) 안에 최대 13건이므로 국소 판정 가능
        txs = [Transaction("C1", START + timedelta(days=30 * i), 84.0, 10,
                           500_000_000) for i in range(24)]
        cr = clean(txs)
        self.assertEqual(cr.fallback_judged, 0)
        self.assertIn("전 건 동시기", cr.basis_note)

    def test_very_sparse_group_uses_whole_period(self):
        # 서로 2년씩 떨어진 3건 — 창 안에 자기 자신뿐
        txs = [Transaction("C1", date(2020 + i, 5, 1), 84.0, 10, 500_000_000)
               for i in range(3)]
        cr = clean(txs)
        self.assertEqual(cr.fallback_judged, 3)
        self.assertEqual(cr.local_judged, 0)
        self.assertIn("[LIMITATION]", cr.basis_note)

    def test_counts_cover_every_transaction(self):
        txs, _ = _series(1.05)
        cr = clean(txs)
        self.assertEqual(cr.local_judged + cr.fallback_judged, len(txs))

    def test_thresholds_are_sane(self):
        self.assertGreaterEqual(LOCAL_WINDOW_MONTHS, 3)
        self.assertGreaterEqual(MIN_LOCAL_SAMPLES, 3)


class TestUnchangedBehaviour(unittest.TestCase):
    def test_flat_market_result_is_stable(self):
        txs, _ = _series(1.0)
        a, b = clean(txs), clean(txs)
        self.assertEqual(a.summary, b.summary)

    def test_cancel_and_duplicate_rules_intact(self):
        t1 = Transaction("C1", date(2025, 5, 1), 84.0, 10, 500_000_000)
        t2 = Transaction("C1", date(2025, 5, 1), 84.0, 10, 500_000_000)
        t3 = Transaction("C1", date(2025, 5, 1), 84.0, 10, 500_000_000)
        t3.canceled = True
        cr = clean([t1, t2, t3])
        self.assertEqual(len(cr.removed["취소"]), 1)
        self.assertEqual(len(cr.removed["중복"]), 1)
        self.assertEqual(len(cr.kept), 1)

    def test_rules_version_bumped(self):
        # 룰이 바뀌면 버전이 올라가야 백테스트·리포트 결과와 대조가 가능하다
        self.assertNotEqual(RULES_VERSION, "clean-1.0")

    def test_groups_stay_separated_by_complex_and_area(self):
        # 다른 단지의 가격대가 서로의 기준을 오염시키면 안 된다
        cheap = [Transaction("A", date(2025, 5, 1), 84.0, 10, 300_000_000)
                 for _ in range(8)]
        rich = [Transaction("B", date(2025, 5, 1), 84.0, 10, 900_000_000)
                for _ in range(8)]
        for i, t in enumerate(cheap + rich):
            t.floor = i          # 완전중복 회피
        cr = clean(cheap + rich)
        self.assertEqual(len(cr.kept), 16)




class TestDeterminism(unittest.TestCase):
    """같은 입력은 같은 분석을 낸다.

    예외는 예측 이력 장부의 봉인 ID 하나뿐이다 — 발행 시점을 포함하는 해시이므로
    재발행할 때마다 달라지는 것이 설계된 동작이다(부록 E.3). 그 외의 어떤
    바이트도 실행마다 달라져서는 안 된다.
    """

    def _report(self):
        from report_system import sample_data as sd
        from report_system.ledger import ForecastLedger
        from report_system.pipeline import run
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
                   asof=sd.ASOF, ledger=ForecastLedger()).markdown

    @staticmethod
    def _mask_seals(md: str) -> str:
        import re
        return re.sub(r":[0-9a-f]{12}\b", ":<SEAL>", md)

    def test_report_is_byte_identical_apart_from_seal_ids(self):
        a, b = self._report(), self._report()
        self.assertEqual(self._mask_seals(a), self._mask_seals(b))

    def test_cleaning_is_order_independent(self):
        """입력 순서가 정제 결과를 바꾸면 안 된다."""
        import random
        from report_system import sample_data as sd
        txs = sd.build_transactions(sd.build_comparables())
        shuffled = list(txs)
        random.Random(3).shuffle(shuffled)
        a, b = clean(txs), clean(shuffled)
        self.assertEqual(a.summary, b.summary)
        self.assertEqual([(t.complex_id, t.trade_date, t.price) for t in a.kept],
                         [(t.complex_id, t.trade_date, t.price) for t in b.kept])


if __name__ == "__main__":
    unittest.main()
