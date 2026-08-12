"""CLI.

사용법:
  python -m report_system generate                      # 샘플 데이터 진단리포트 (out/)
  python -m report_system live --config <site.json>     # 실데이터 진단리포트 (E01·E02)
  python -m report_system doctor --config <site.json>   # 실행 전 설정·연결 진단
  python -m report_system coverage                      # 예측 이력 장부 적중률 조회

실데이터 실행 전제: 환경변수 DATA_GO_KR_API_KEY (공공데이터포털 인증키).
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import date

from . import sample_data as sd
from .connectors.base import MissingApiKeyError
from .ledger import ForecastLedger
from .pipeline import run
from .render_html import markdown_to_html

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
    html_path = OUT / "sample_report.html"
    html_path.write_text(
        markdown_to_html(result.markdown, result.inputs.site.name), encoding="utf-8")
    print(f"리포트 생성: {path} / {html_path}")
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


def cmd_backtest() -> int:
    """샘플 데이터로 백테스트만 실행 (모델 검증 단독 확인용)."""
    from datetime import date as _date

    from .backtest import (backtest_price_bands, backtest_subscription,
                           quarterly_cutoffs)
    from .modelcard import detect_drift

    comps = sd.build_comparables()
    txs = sd.build_transactions(comps)
    first = min(t.trade_date for t in txs)
    reports = [
        backtest_price_bands(sd.build_site(), comps, txs,
                             quarterly_cutoffs(first, sd.ASOF)),
        backtest_subscription(sd.build_subscription_history()),
    ]
    OUT.mkdir(exist_ok=True)
    lines = ["# 백테스트 결과", ""]
    for r in reports:
        print(f"[{r.name}] n={r.n} — {r.verdict()}")
        lines += [r.as_markdown(), ""]
        if r.folds:
            d = detect_drift(r)
            print(f"  드리프트: {d.verdict}")
            lines += [d.as_markdown(), ""]
    (OUT / "backtest.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"저장: {OUT / 'backtest.md'}")
    return 0


def cmd_doctor(config: str, skip_api: bool) -> int:
    """실행 전 설정·연결·데이터 가용성 진단."""
    import json

    from .doctor import (check_apis, check_config, match_comparables,
                         summarize)

    cfg = json.loads(pathlib.Path(config).read_text(encoding="utf-8"))
    checks = check_config(cfg)
    print("[1] 설정 검사")
    for c in checks:
        print(c.line())

    names: list[str] = []
    if not skip_api:
        print("\n[2] API 연결 검사")
        api_checks, names = check_apis(cfg)
        for c in api_checks:
            print(c.line())
        checks += api_checks

        print("\n[3] 비교단지 매칭")
        m = match_comparables(cfg, names)
        for c in m:
            print(c.line())
        checks += m
        if names:
            OUT.mkdir(exist_ok=True)
            (OUT / "apt_names.txt").write_text("\n".join(names), encoding="utf-8")
            print(f"     · 지역 단지명 {len(names)}개 저장: {OUT / 'apt_names.txt'}")

    ok, warn, fail = summarize(checks)
    print(f"\n결과: OK {ok} · 주의 {warn} · 실패 {fail}")
    if fail:
        print("실패 항목을 해결한 뒤 live 를 실행하십시오.")
    return 1 if fail else 0


def cmd_live(config: str, asof: str | None, offline: bool) -> int:
    from .live import run_live
    OUT.mkdir(exist_ok=True)
    try:
        result = run_live(
            config, asof=date.fromisoformat(asof) if asof else None,
            offline=offline)
    except MissingApiKeyError as e:
        print(f"[설정 필요] {e}", file=sys.stderr)
        return 2
    path = OUT / "live_report.md"
    path.write_text(result.markdown, encoding="utf-8")
    html_path = OUT / "live_report.html"
    html_path.write_text(
        markdown_to_html(result.markdown, result.inputs.site.name), encoding="utf-8")
    print(f"리포트 생성: {path} / {html_path}")
    print("수집 이력: out/provenance.json")
    if result.forecast_id:
        print(f"청약 전망 봉인: {result.forecast_id}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="report_system")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("generate")
    sub.add_parser("coverage")
    sub.add_parser("backtest")
    dc = sub.add_parser("doctor")
    dc.add_argument("--config", required=True, help="현장 설정 JSON 경로")
    dc.add_argument("--skip-api", action="store_true", help="설정 검사만 수행")
    lv = sub.add_parser("live")
    lv.add_argument("--config", required=True, help="현장 설정 JSON 경로")
    lv.add_argument("--asof", help="분석 기준일 YYYY-MM-DD (기본: 설정값)")
    lv.add_argument("--offline", action="store_true",
                    help="네트워크 없이 캐시만 사용 (재현 실행)")
    args = p.parse_args()
    if args.command == "generate":
        return cmd_generate()
    if args.command == "coverage":
        return cmd_coverage()
    if args.command == "backtest":
        return cmd_backtest()
    if args.command == "doctor":
        return cmd_doctor(args.config, args.skip_api)
    return cmd_live(args.config, args.asof, args.offline)


if __name__ == "__main__":
    sys.exit(main())
