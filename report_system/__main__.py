"""CLI.

사용법:
  python -m report_system generate   # 샘플 데이터로 진단리포트 생성 (out/)
  python -m report_system coverage   # 예측 이력 장부의 적중률 조회
"""
from __future__ import annotations

import argparse
import pathlib
import sys

from . import sample_data as sd
from .ledger import ForecastLedger
from .pipeline import run

OUT = pathlib.Path("out")


def cmd_generate() -> int:
    OUT.mkdir(exist_ok=True)
    ledger = ForecastLedger(str(OUT / "forecast_ledger.db"))
    comps = sd.build_comparables()
    cat_new = sd.build_catalysts()
    # 조기경보 시연: 직전 스냅숏(예산 확보 전 단계)을 old로 사용
    cat_old = sd.build_catalysts()
    cat_old[0].budget_secured = 450_000_000_000

    result = run(
        site=sd.build_site(),
        comps=comps,
        txs=sd.build_transactions(comps),
        sub_history=sd.build_subscription_history(),
        supply_items=sd.build_supply(),
        catalyst_plans_old=cat_old,
        catalyst_plans_new=cat_new,
        dataset_meta=sd.build_dataset_meta(),
        incomes=sd.build_incomes(),
        feedback=sd.build_feedback(),
        listings=sd.build_listing_snapshots(),
        asof=sd.ASOF,
        ledger=ledger,
    )
    path = OUT / "sample_report.md"
    path.write_text(result.markdown, encoding="utf-8")
    print(f"리포트 생성: {path}")
    if result.forecast_id:
        print(f"청약 전망 봉인: {result.forecast_id} "
              f"(무결성 {'OK' if ledger.verify_seal(result.forecast_id) else 'FAIL'})")
    return 0


def cmd_coverage() -> int:
    ledger = ForecastLedger(str(OUT / "forecast_ledger.db"))
    rep = ledger.coverage("subscription", nominal_confidence=0.60)
    print(f"[청약 전망] {rep.verdict()}")
    for row in ledger.history("subscription"):
        fid, target, lo, hi, issued, actual, hit = row
        status = "미확정" if actual is None else ("적중" if hit else "이탈")
        print(f"  {issued} {target} [{lo:.1f},{hi:.1f}] → {actual if actual is not None else '-'} {status}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="report_system")
    p.add_argument("command", choices=["generate", "coverage"])
    args = p.parse_args()
    return cmd_generate() if args.command == "generate" else cmd_coverage()


if __name__ == "__main__":
    sys.exit(main())
