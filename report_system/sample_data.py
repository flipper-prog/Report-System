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
