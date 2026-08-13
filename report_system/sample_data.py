"""합성 샘플 데이터 (고정 시드).

⚠️ 본 모듈의 모든 수치는 파이프라인 시연·테스트용 합성 데이터이며
어떤 실제 현장·단지의 분석 결과도 아니다.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

from .models import (
    CatalystPlan, Comparable, DatasetMeta, FieldFeedback, ListingSnapshot,
    MaturityStage, RentRecord, Site, SubscriptionRecord, SupplyItem,
    SupplyStage, Transaction, TypeSpec,
)

SEED = 20260812
ASOF = date(2026, 7, 25)


def build_site() -> Site:
    return Site(
        id="SAMPLE-001",
        name="샘플 시티 A현장 (합성 데이터)",
        address="샘플시 표본구 검증로 1",
        lat=37.50, lng=127.00,
        total_units=460,
        types=[
            TypeSpec("59A", 59.9, 180, 620_000_000, 18_000_000, (1, 25)),
            TypeSpec("84A", 84.9, 220, 830_000_000, 24_000_000, (1, 25)),
            TypeSpec("84B", 84.8, 60, 815_000_000, 24_000_000, (1, 15)),
        ],
        expected_movein=date(2028, 9, 1),
        region="샘플권역",
        brand_tier=2,
    )


def build_comparables() -> dict[str, Comparable]:
    comps = [
        Comparable("C-OLD1", "표본1단지(구축)", 2016, 800, 2, 600, "샘플권역"),
        Comparable("C-OLD2", "표본2단지(구축)", 2019, 500, 2, 1100, "샘플권역"),
        Comparable("C-NEW1", "표본3단지(준신축)", 2023, 700, 1, 1500, "샘플권역"),
        Comparable("C-PRS1", "표본4 분양권", 2027, 600, 2, 900, "샘플권역",
                   is_presale_right=True),
    ]
    return {c.id: c for c in comps}


def build_transactions(comps: dict[str, Comparable]) -> list[Transaction]:
    rng = random.Random(SEED)
    base_ppsm = {"C-OLD1": 9_000_000, "C-OLD2": 9_600_000,
                 "C-NEW1": 10_800_000, "C-PRS1": 10_200_000}
    txs: list[Transaction] = []
    for cid, comp in comps.items():
        for i in range(70):
            d = ASOF - timedelta(days=rng.randint(1, 36 * 30))
            area = rng.choice([59.9, 74.9, 84.9])
            floor = rng.randint(1, 25)
            drift = 1 + 0.02 * (1 - (ASOF - d).days / (36 * 30))   # 완만한 상승 추세
            noise = rng.gauss(1.0, 0.05)
            price = int(base_ppsm[cid] * area * drift * noise)
            txs.append(Transaction(cid, d, area, floor, price))
    # 오염 표본 주입: 취소·중복·이상 고가·특수 저가
    txs.append(Transaction("C-OLD1", ASOF - timedelta(days=30), 84.9, 10, 1_300_000_000, canceled=True))
    dup = txs[0]
    txs.append(Transaction(dup.complex_id, dup.trade_date, dup.area_m2, dup.floor, dup.price))
    txs.append(Transaction("C-OLD2", ASOF - timedelta(days=45), 84.9, 12, 2_600_000_000))  # 이상 고가
    txs.append(Transaction("C-NEW1", ASOF - timedelta(days=60), 84.9, 3, 380_000_000))     # 특수 저가 의심
    return txs


def build_rents(comps: dict[str, Comparable]) -> list[RentRecord]:
    """전월세 합성 표본 — 전세가율이 약 62%(하방 지지 보통)가 되도록 생성.

    최근 6개월 전세 ㎡단가를 소폭 높여 전세가율 상승(실수요 강화) 국면을 만든다.
    갱신 계약도 섞어 분석 단계에서 제외되는지 확인할 수 있게 한다.
    """
    rng = random.Random(SEED + 5)
    base_ppsm = {"C-OLD1": 9_000_000, "C-OLD2": 9_600_000,
                 "C-NEW1": 10_800_000, "C-PRS1": 10_200_000}
    out: list[RentRecord] = []
    for cid, comp in comps.items():
        for _ in range(24):
            d = ASOF - timedelta(days=rng.randint(1, 12 * 30))
            area = rng.choice([59.9, 74.9, 84.9])
            recent = (ASOF - d).days <= 6 * 30
            ratio = rng.gauss(0.64 if recent else 0.60, 0.03)
            deposit = int(base_ppsm[cid] * area * max(0.4, ratio))
            renewal = rng.random() < 0.15
            if rng.random() < 0.25:
                # 월세: 보증금을 전세의 30% 수준으로 낮추고 전환율 약 5.5% 적용
                mon_deposit = int(deposit * 0.3)
                monthly = int((deposit - mon_deposit) * 0.055 / 12)
                out.append(RentRecord(cid, d, area, rng.randint(1, 25),
                                      mon_deposit, monthly, renewal))
            else:
                out.append(RentRecord(cid, d, area, rng.randint(1, 25),
                                      deposit, 0, renewal))
    return out


def build_subscription_history() -> list[SubscriptionRecord]:
    rng = random.Random(SEED + 1)
    hist: list[SubscriptionRecord] = []
    for i in range(28):
        gap = rng.uniform(-8, 12)             # 분양가-시세 갭(%)
        supply = rng.choice([300, 700, 1200, 2500])
        base = max(0.2, 8 - 0.5 * gap - supply / 1200 + rng.gauss(0, 1.2))
        units = rng.choice([200, 400, 600])
        hist.append(SubscriptionRecord(
            complex_id=f"H-{i:02d}",
            open_date=ASOF - timedelta(days=rng.randint(60, 900)),
            units=units,
            applicants=int(units * base),
            region="샘플권역" if i % 4 else "인접권역",
            price_gap_pct=gap,
            concurrent_supply=supply,
            sold_out_in_order=base >= 1.0,
        ))
    return hist


def build_supply() -> list[SupplyItem]:
    return [
        SupplyItem("표본5단지", 900, SupplyStage.MOVEIN, 10),
        SupplyItem("표본6단지", 1200, SupplyStage.START, 26),
        SupplyItem("표본7구역", 2000, SupplyStage.PERMIT, 34),
        SupplyItem("표본8구역(장기)", 3000, SupplyStage.PERMIT, 60),  # 윈도 밖
    ]


def build_catalysts() -> list[CatalystPlan]:
    return [
        CatalystPlan(
            "K-1", "샘플선 연장(가칭)", MaturityStage.DESIGN,
            budget_total=1_200_000_000_000, budget_secured=300_000_000_000,
            dist_m=700, time_saving_min=12,
            negatives=["공사 기간 소음", "개통 일정 지연 이력 1회"],
            source_docs=["국토부 고시 제2026-000호(예시)"]),
        CatalystPlan(
            "K-2", "표본IC 신설(검토)", MaturityStage.IDEA,
            budget_total=0, budget_secured=0,
            dist_m=1800, time_saving_min=4,
            negatives=["법정계획 미반영"],
            source_docs=["지자체 검토 보도자료(예시)"]),
    ]


def build_dataset_meta() -> list[DatasetMeta]:
    return [
        DatasetMeta("L11 실거래(정제)", 24, 24, 18, 14, 15),
        DatasetMeta("L12 청약 이력", 22, 22, 17, 13, 15),
        DatasetMeta("L13 공급 파이프라인", 20, 18, 15, 12, 15),
        DatasetMeta("L5 소득·구매력(대체)", 15, 16, 13, 10, 15,
                    note="공공 대체 데이터 — 제한 사용"),
        DatasetMeta("L6 유동인구(민간)", 20, 20, 15, 12, 0,
                    note="라이선스 미확보 — 사용 금지 (커버리지: 별도 협의)"),
    ]


def build_incomes() -> list[float]:
    rng = random.Random(SEED + 2)
    return [max(28_000_000, rng.lognormvariate(17.9, 0.45)) for _ in range(500)]


def build_feedback() -> FieldFeedback:
    return FieldFeedback(
        total_consults=120,
        rejections={"가격": 34, "대출": 12, "가족협의": 9, "경쟁현장": 11, "교통": 6},
        visitor_home_regions={"샘플권역": 58, "인접권역": 31, "원거리": 11},
    )


def build_listing_snapshots() -> tuple[ListingSnapshot, ListingSnapshot]:
    old = ListingSnapshot(ASOF - timedelta(days=30), 210, 10_900_000, 10_400_000)
    new = ListingSnapshot(ASOF, 268, 11_300_000, 10_350_000)
    return old, new


# ── 선택 레이어 합성 표본 (generate --full 시연용) ──────────────────────────
#
# examples/*.csv 는 '실제 설정에서 어떤 파일을 넣는가'의 예시이고 강남권 좌표·
# 지역명으로 되어 있다. 합성 현장은 좌표도 지역명도 가상이므로, 시연용 레이어는
# 현장과 아귀가 맞게 여기서 직접 만든다.

def build_unsold() -> "UnsoldSeries":
    from .connectors.unsold import UnsoldPoint, UnsoldSeries
    months = ["2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07"]
    vals = [1200, 1290, 1380, 1450, 1510, 1560]      # 완만한 증가 국면
    return UnsoldSeries(
        points=[UnsoldPoint(m, v, int(v * 0.13)) for m, v in zip(months, vals)],
        source="미분양 합성 표본")


def build_migration() -> "MigrationSeries":
    from .connectors.migration import MigrationPoint, MigrationSeries
    rng = random.Random(SEED + 6)
    pts, sources = [], {"표본구 갑동": 0, "인접 을시": 0, "원거리 병시": 0}
    for i in range(12):
        y, m = divmod(ASOF.year * 12 + ASOF.month - 1 - (11 - i), 12)
        moved_in = 4200 + i * 40 + rng.randint(-120, 120)
        moved_out = 4050 + i * 25 + rng.randint(-120, 120)
        pts.append(MigrationPoint(f"{y}-{m + 1:02d}", moved_in, moved_out))
        for k, share in (("표본구 갑동", 0.48), ("인접 을시", 0.33),
                         ("원거리 병시", 0.19)):
            sources[k] += int(moved_in * share)
    return MigrationSeries(points=pts, inflow_sources=sources,
                           population=310_000, source="인구이동 합성 표본")


def build_mobility() -> "ODMatrix":
    from .connectors.mobility import ODMatrix
    od = ODMatrix(focus="샘플권역", purpose="출근", source="O/D 합성 표본")
    od.internal = 128_000
    od.outbound = {"인접 을시": 46_000, "원거리 병시": 21_000, "표본 도심": 38_000}
    od.inbound = {"인접 을시": 52_000, "표본구 갑동": 34_000, "원거리 병시": 12_000}
    od.limitations.append(
        "O/D 는 계약·승인 기반 제공 자료로 갱신 주기가 길다 — 최근 개통·입주 "
        "효과는 반영되지 않을 수 있음 [LIMITATION]")
    return od


def build_transit() -> "TransitAccess":
    """합성 현장(37.50, 127.00) 주변에 정류장·역을 배치한다."""
    from .connectors.transit import Station, Stop, TransitAccess
    from .geo import haversine_m
    site = build_site()
    acc = TransitAccess(lat=site.lat, lng=site.lng, radius_m=500)
    for name, dlat, dlng in (("표본역 1번출구", 0.0021, 0.0011),
                             ("검증로 정류장", 0.0008, -0.0014),
                             ("표본초교 정류장", -0.0019, 0.0021)):
        la, ln = site.lat + dlat, site.lng + dlng
        acc.stops.append(Stop(name, la, ln, haversine_m(site.lat, site.lng, la, ln)))
    acc.stops.sort(key=lambda s: s.dist_m)
    for name, dlat, dlng, lines in (("표본역", 0.0024, 0.0013, ["표본선"]),
                                    ("검증역", 0.0130, -0.0090, ["검증선", "표본선"])):
        la, ln = site.lat + dlat, site.lng + dlng
        acc.stations.append(
            Station(name, la, ln, haversine_m(site.lat, site.lng, la, ln), lines))
    acc.stations.sort(key=lambda s: s.dist_m)
    acc.limitations.append(
        "직선거리 기반 도보 환산(보정계수 1.3) — 실제 보행 경로·고저차·신호는 "
        "미반영. 배차 간격·환승 편의도 평가에 포함되지 않음 [LIMITATION]")
    return acc
