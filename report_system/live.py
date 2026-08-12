"""실데이터 파이프라인: 설정 파일(JSON) → 커넥터 수집 → 진단리포트.

설정 예시는 examples/site_config.json 참조.
수집 이력(Provenance)은 out/provenance.json 으로 저장되어 근거원장 입력이 된다.
"""
from __future__ import annotations

import json
import pathlib
import random
from datetime import date, timedelta

from .connectors.applyhome import fetch_subscription_history
from .connectors.base import Fetcher, api_key
from .connectors.listings import ListingsFormatError, load as load_listings
from .connectors.molit import (build_comparables, fetch_range,
                               to_transactions)
from .ledger import ForecastLedger
from .runstore import RunStore
from .models import (CatalystPlan, DatasetMeta, FieldFeedback,
                     ListingSnapshot, MaturityStage, Site, SupplyItem,
                     SupplyStage, Site as _Site, TypeSpec)
from .pipeline import PipelineResult, run


def load_config(path: str) -> dict:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def _site_from(cfg: dict) -> Site:
    s = cfg["site"]
    return Site(
        id=s["id"], name=s["name"], address=s["address"],
        lat=float(s["lat"]), lng=float(s["lng"]),
        total_units=int(s["total_units"]),
        types=[TypeSpec(
            name=t["name"], area_m2=float(t["area_m2"]), units=int(t["units"]),
            base_price=int(t["base_price"]),
            option_cost=int(t.get("option_cost", 0)),
            floors=tuple(t.get("floors", [1, 20])),  # type: ignore[arg-type]
        ) for t in s["types"]],
        expected_movein=date.fromisoformat(s["expected_movein"]) if s.get("expected_movein") else None,
        region=s.get("region", ""),
        brand_tier=int(s.get("brand_tier", 2)),
        product_type=s.get("product_type", "아파트"))


def _supply_from(cfg: dict) -> list[SupplyItem]:
    return [SupplyItem(
        name=i["name"], units=int(i["units"]),
        stage=SupplyStage(i["stage"]),
        months_to_movein=int(i["months_to_movein"]),
    ) for i in cfg.get("supply", [])]


def _catalysts_from(cfg: dict) -> list[CatalystPlan]:
    return [CatalystPlan(
        id=c["id"], name=c["name"], stage=MaturityStage(c["stage"]),
        budget_total=int(c.get("budget_total", 0)),
        budget_secured=int(c.get("budget_secured", 0)),
        dist_m=float(c.get("dist_m", 9999)),
        time_saving_min=float(c.get("time_saving_min", 0)),
        negatives=list(c.get("negatives", [])),
        source_docs=list(c.get("source_docs", [])),
    ) for c in cfg.get("catalysts", [])]


def _incomes_from(cfg: dict) -> list[float]:
    """소득 표본: 실데이터 커넥터 미구현 영역 — 설정의 분포 파라미터로 생성.

    [LIMITATION] L5는 공공 대체(로그정규 근사)이며 리포트에 가정으로 병기된다.
    """
    p = cfg.get("income_model", {"median": 60_000_000, "sigma": 0.45, "n": 500})
    rng = random.Random(int(p.get("seed", 7)))
    mu = __import__("math").log(float(p["median"]))
    return [max(24_000_000.0, rng.lognormvariate(mu, float(p["sigma"])))
            for _ in range(int(p.get("n", 500)))]


def _feedback_from(cfg: dict) -> FieldFeedback:
    f = cfg.get("feedback")
    if not f:
        return FieldFeedback(total_consults=0, rejections={})
    return FieldFeedback(
        total_consults=int(f.get("total_consults", 0)),
        rejections={k: int(v) for k, v in f.get("rejections", {}).items()},
        visitor_home_regions={k: int(v) for k, v in f.get("visitor_home_regions", {}).items()})


def run_live(config_path: str, asof: date | None = None,
             cache_dir: str = "out/cache", offline: bool = False,
             ledger_path: str = "out/forecast_ledger.db",
             store_path: str = "out/runs.db") -> PipelineResult:
    cfg = load_config(config_path)
    asof = asof or date.fromisoformat(cfg["asof"])
    key = api_key()
    # 장부·이력 DB의 상위 디렉터리를 미리 생성 (sqlite는 자동 생성하지 않음)
    for pth in (ledger_path, store_path):
        parent = pathlib.Path(pth).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
    fetcher = Fetcher(cache_dir=cache_dir, offline=offline)

    # E01 실거래 수집 → 비교단지·거래 적재
    raws = fetch_range(fetcher, key, cfg["lawd_cd"], asof,
                       months=int(cfg.get("months", 24)))
    comp_cfg = cfg["comparables"]
    comps = build_comparables(raws, comp_cfg)
    apt_to_cid = {w["apt_nm"]: cid for cid, w in
                  zip(comps.keys(), comp_cfg)}
    txs = to_transactions(raws, apt_to_cid)
    if not txs:
        names = sorted({r.apt_nm for r in raws})[:30]
        raise RuntimeError(
            "비교단지 거래 0건 — 설정의 apt_nm이 실거래 데이터의 단지명과 일치하는지 확인 필요.\n"
            f"이 지역({cfg['lawd_cd']}) 단지명 예시: {names}")

    # E02 청약 이력 수집
    sub_hist = fetch_subscription_history(
        fetcher, key,
        region_names=list(cfg.get("subscription_regions", [])),
        since=asof - timedelta(days=int(cfg.get("subscription_lookback_days", 900))),
        until=asof)

    # 매물·호가 선행 신호 (P1-2). 설정에 파일이 지정된 경우에만 활성화된다.
    neutral = ListingSnapshot(asof, 0, 1.0, 1.0)
    listings_pair = (neutral, neutral)
    listings_note = "매물 파일 미지정 — 시장 선행 신호 비활성"
    lf = cfg.get("listings_file")
    if lf:
        try:
            lr = load_listings(lf, until=asof)
            pair = lr.latest_pair
            if pair:
                listings_pair = pair
                listings_note = (f"{lr.source} — 관측 {len(lr.snapshots)}건, "
                                 f"최신 {lr.snapshots[-1].asof}")
            else:
                listings_note = f"{lr.source} — 관측 {len(lr.snapshots)}건(2건 미만, 비교 불가)"
            if lr.skipped:
                listings_note += f" · 제외 {len(lr.skipped)}행"
        except ListingsFormatError as e:
            listings_note = f"매물 파일 오류 — {e}" 

    result = run(
        site=_site_from(cfg),
        comps=comps,
        txs=txs,
        sub_history=sub_hist,
        supply_items=_supply_from(cfg),
        catalyst_plans_old=_catalysts_from(cfg),
        catalyst_plans_new=_catalysts_from(cfg),
        dataset_meta=_dataset_meta(sub_hist_n=len(sub_hist), tx_n=len(txs),
                                   listings_note=listings_note),
        incomes=_incomes_from(cfg),
        feedback=_feedback_from(cfg),
        listings=listings_pair,
        asof=asof,
        ledger=ForecastLedger(ledger_path),
        store=RunStore(store_path))

    pathlib.Path("out").mkdir(exist_ok=True)
    pathlib.Path("out/provenance.json").write_text(
        fetcher.provenance_json(), encoding="utf-8")
    return result


def _dataset_meta(sub_hist_n: int, tx_n: int,
                  listings_note: str = "") -> list[DatasetMeta]:
    """수집 결과 기반의 적합성 평가(라이브 기본값)."""
    return [
        DatasetMeta("L11 실거래 (국토부 E01)", 24, 24, 18, 15, 15,
                    note=f"수집 {tx_n}건, 캐시 재현 가능"),
        DatasetMeta("L12 청약 이력 (청약홈 E02)", 20, 22, 15, 14, 15,
                    note=f"수집 {sub_hist_n}건 — 가격 갭·동시 공급 미제공(지역·기간 매칭)"),
        DatasetMeta("L5 소득·구매력 (로그정규 근사)", 12, 14, 10, 8, 15,
                    note="공공 대체 근사 — 제한 사용 [LIMITATION]"),
        (DatasetMeta("매물·호가 (파일 수집)", 18, 20, 12, 12, 15, note=listings_note)
         if listings_note.startswith("매물·호가 파일")
         else DatasetMeta("매물·호가 (미수집)", 0, 0, 0, 0, 0, note=listings_note)),
    ]
