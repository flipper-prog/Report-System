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
from .connectors.commerce import CommerceApiError, fetch_radius
from .connectors.housing import HousingFormatError, fetch_kosis as fetch_housing_kosis
from .connectors.housing import load as load_housing
from .connectors.listings import ListingsFormatError, load as load_listings
from .connectors.migration import (KosisApiError, MigrationFormatError,
                                   fetch_kosis, kosis_key)
from .connectors.migration import load as load_migration
from .connectors.mobility import MobilityFormatError, load as load_mobility
from .connectors.sgis import SgisAuthError, credentials as sgis_credentials
from .connectors.sgis import fetch_region_stats, get_token
from .connectors.transit import (StationFormatError, TransitApiError,
                                 collect as collect_transit)
from .connectors.unsold import UnsoldFormatError, load as load_unsold
from .connectors.molit import (build_comparables, fetch_range,
                               to_transactions)
from .connectors.rent import RentApiError
from .connectors.rent import fetch_range as fetch_rent_range
from .connectors.rent import to_records as to_rent_records
from .calibrate import load as load_coefficients
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

    # E01-R 전월세 실거래 수집 (L11 완성 — 전세가율·전월세전환율)
    rents = []
    if cfg.get("collect_rent", True):
        try:
            rent_raws = fetch_rent_range(
                fetcher, key, cfg["lawd_cd"], asof,
                months=int(cfg.get("rent_months", cfg.get("months", 24))))
            rents = to_rent_records(rent_raws, apt_to_cid)
            if not rents:
                print("[안내] 비교단지 전월세 거래 0건 — 전세 기반 하방 점검 생략")
        except (RentApiError, RuntimeError) as e:
            print(f"[안내] 전월세 수집 생략 — {e}")

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

    # L1·L2·L4 SGIS 지역 통계 (선택 — 별도 인증)
    region_stats = None
    if cfg.get("sgis_adm_cd"):
        try:
            token = get_token(fetcher, *sgis_credentials())
            years = list(cfg.get("sgis_years", [asof.year - 5, asof.year - 3, asof.year - 1]))
            region_stats = fetch_region_stats(fetcher, str(cfg["sgis_adm_cd"]), years, token)
        except (SgisAuthError, RuntimeError) as e:
            print(f"[안내] SGIS 수집 생략 — {e}")

    # L9 상권 (선택 — DATA_GO_KR 키 공용)
    commerce = None
    if cfg.get("commerce_radius_m"):
        try:
            site_cfg = cfg["site"]
            commerce = fetch_radius(fetcher, key, float(site_cfg["lng"]),
                                    float(site_cfg["lat"]),
                                    int(cfg["commerce_radius_m"]))
        except (CommerceApiError, RuntimeError) as e:
            print(f"[안내] 상권 수집 생략 — {e}")

    # L12 보강 — 미분양 (파일)
    unsold = None
    if cfg.get("unsold_file"):
        try:
            unsold = load_unsold(cfg["unsold_file"], until=asof)
        except UnsoldFormatError as e:
            print(f"[안내] 미분양 수집 생략 — {e}")

    # L3 인구이동 — KOSIS API(kosis_migration) 또는 파일(migration_file)
    migration = None
    if cfg.get("kosis_migration"):
        try:
            spec = dict(cfg["kosis_migration"])
            migration = fetch_kosis(fetcher, kosis_key(), spec,
                                    population=cfg.get("region_population"))
        except KosisApiError as e:
            print(f"[안내] 인구이동(KOSIS) 수집 생략 — {e}")
    if migration is None and cfg.get("migration_file"):
        try:
            migration = load_migration(
                cfg["migration_file"], until=f"{asof.year}-{asof.month:02d}",
                population=cfg.get("region_population"))
        except MigrationFormatError as e:
            print(f"[안내] 인구이동 수집 생략 — {e}")

    # L7 생활이동·O/D — 계약 자료 파일
    mobility = None
    if cfg.get("mobility_file"):
        try:
            mobility = load_mobility(
                cfg["mobility_file"],
                focus=cfg.get("mobility_focus") or cfg["site"].get("region", ""),
                purpose=cfg.get("mobility_purpose"))
        except MobilityFormatError as e:
            print(f"[안내] 생활이동 수집 생략 — {e}")

    # L8 교통망·접근성 — 정류장(TAGO API) + 역 좌표 파일
    transit = None
    if cfg.get("transit_radius_m") or cfg.get("stations_file"):
        try:
            site_cfg = cfg["site"]
            transit = collect_transit(
                fetcher if cfg.get("transit_radius_m") else None,
                key if cfg.get("transit_radius_m") else None,
                float(site_cfg["lat"]), float(site_cfg["lng"]),
                radius_m=int(cfg.get("transit_radius_m", 500)),
                stations_file=cfg.get("stations_file"))
        except (TransitApiError, StationFormatError, RuntimeError) as e:
            print(f"[안내] 교통 접근성 수집 생략 — {e}")

    # L13 주택건설실적 — KOSIS API(kosis_housing) 또는 파일(housing_file)
    housing = None
    if cfg.get("kosis_housing"):
        try:
            housing = fetch_housing_kosis(fetcher, kosis_key(), dict(cfg["kosis_housing"]))
        except KosisApiError as e:
            print(f"[안내] 주택건설실적(KOSIS) 수집 생략 — {e}")
    if housing is None and cfg.get("housing_file"):
        try:
            housing = load_housing(cfg["housing_file"],
                                   until=f"{asof.year}-{asof.month:02d}")
        except HousingFormatError as e:
            print(f"[안내] 주택건설실적 수집 생략 — {e}")

    result = run(
        site=_site_from(cfg),
        comps=comps,
        txs=txs,
        sub_history=sub_hist,
        supply_items=_supply_from(cfg),
        catalyst_plans_old=_catalysts_from(cfg),
        catalyst_plans_new=_catalysts_from(cfg),
        dataset_meta=_dataset_meta(sub_hist_n=len(sub_hist), tx_n=len(txs),
                                   listings_note=listings_note,
                                   region_stats=region_stats,
                                   commerce=commerce, unsold=unsold,
                                   migration=migration, mobility=mobility,
                                   transit=transit, rent_n=len(rents)),
        incomes=_incomes_from(cfg),
        feedback=_feedback_from(cfg),
        listings=listings_pair,
        asof=asof,
        ledger=ForecastLedger(ledger_path),
        store=RunStore(store_path),
        region_stats=region_stats, commerce=commerce, unsold=unsold,
        migration=migration, mobility=mobility, transit=transit,
        rents=rents,
        coef=load_coefficients(cfg.get("coefficients_file", "out/coefficients.json")),
        provenance=fetcher.provenance, housing=housing)

    pathlib.Path("out").mkdir(exist_ok=True)
    pathlib.Path("out/provenance.json").write_text(
        fetcher.provenance_json(), encoding="utf-8")
    return result


def _dataset_meta(sub_hist_n: int, tx_n: int,
                  listings_note: str = "",
                  region_stats=None, commerce=None, unsold=None,
                  migration=None, mobility=None, transit=None,
                  rent_n: int = 0) -> list[DatasetMeta]:
    """수집 결과 기반의 적합성 평가(라이브 기본값)."""
    return [
        DatasetMeta("L11 실거래 (국토부 E01)", 24, 24, 18, 15, 15,
                    note=f"수집 {tx_n}건, 캐시 재현 가능"),
        (DatasetMeta("L11 전월세 (국토부 E01-R)", 22, 24, 17, 14, 15,
                     note=f"수집 {rent_n}건 — 갱신 계약은 분석에서 제외")
         if rent_n else
         DatasetMeta("L11 전월세 (미수집)", 0, 0, 0, 0, 0,
                     note="collect_rent=false 또는 비교단지 전월세 0건")),
        DatasetMeta("L12 청약 이력 (청약홈 E02)", 20, 22, 15, 14, 15,
                    note=f"수집 {sub_hist_n}건 — 가격 갭·동시 공급 미제공(지역·기간 매칭)"),
        DatasetMeta("L5 소득·구매력 (로그정규 근사)", 12, 14, 10, 8, 15,
                    note="공공 대체 근사 — 제한 사용 [LIMITATION]"),
        (DatasetMeta("매물·호가 (파일 수집)", 18, 20, 12, 12, 15, note=listings_note)
         if listings_note.startswith("매물·호가 파일")
         else DatasetMeta("매물·호가 (미수집)", 0, 0, 0, 0, 0, note=listings_note)),
        (DatasetMeta("L1·L2·L4 인구·가구·사업체 (SGIS)", 12, 20, 18, 14, 15,
                     note="시군구 단위 — 생활권보다 해상도 낮음 [LIMITATION]")
         if region_stats is not None
         else DatasetMeta("L1·L2·L4 (미수집)", 0, 0, 0, 0, 0,
                          note="sgis_adm_cd 미지정 또는 인증 없음")),
        (DatasetMeta("L9 상권 (소상공인공단)", 22, 18, 15, 13, 15,
                     note=f"업소 {getattr(commerce, 'total_stores', 0):,}건 수집")
         if commerce is not None
         else DatasetMeta("L9 상권 (미수집)", 0, 0, 0, 0, 0,
                          note="commerce_radius_m 미지정")),
        (DatasetMeta("L12 미분양 (파일)", 15, 22, 16, 14, 15,
                     note=f"관측 {len(getattr(unsold, 'points', []))}개월")
         if unsold is not None
         else DatasetMeta("L12 미분양 (미수집)", 0, 0, 0, 0, 0,
                          note="unsold_file 미지정")),
        (DatasetMeta("L3 인구이동", 18, 20, 16, 14, 15,
                     note=f"{getattr(migration, 'source', '')} — "
                          f"관측 {len(getattr(migration, 'points', []))}개 시점")
         if migration is not None
         else DatasetMeta("L3 인구이동 (미수집)", 0, 0, 0, 0, 0,
                          note="kosis_migration·migration_file 미지정")),
        (DatasetMeta("L7 생활이동·O/D (계약 자료)", 20, 12, 18, 12, 15,
                     note="갱신 주기가 길어 최근 개통·입주 효과 미반영 [LIMITATION]")
         if mobility is not None
         else DatasetMeta("L7 생활이동·O/D (미수집)", 0, 0, 0, 0, 0,
                          note="mobility_file 미지정 — KTDB·통신사 계약 대상")),
        (DatasetMeta("L8 교통망·접근성", 20, 22, 18, 12, 15,
                     note=f"정류장 {len(getattr(transit, 'stops', []))}개소 · "
                          f"역 {len(getattr(transit, 'stations', []))}개 — "
                          "직선거리 도보 환산 [LIMITATION]")
         if transit is not None
         else DatasetMeta("L8 교통망·접근성 (미수집)", 0, 0, 0, 0, 0,
                          note="transit_radius_m·stations_file 미지정")),
    ]
