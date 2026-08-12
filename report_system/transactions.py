"""거래 정제 (부록 A.9): 취소·중복·이상·특수관계 의심 거래의 제거.

정제 룰은 코드로 고정되며(P2-3), 정제 전후 요약이 리포트에 자동 표기된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from .models import Transaction

# 정제 룰 버전 — 룰 변경 시 반드시 증가시키고 리포트·백테스트 결과에 표기한다.
# (동일 버전이면 동일 입력에 동일 정제 결과가 보장된다)
RULES_VERSION = "clean-1.0"
RULES_SUMMARY = (
    "취소거래 제거 · 완전중복 제거 · 단지×면적버킷 MAD z>3.5 이상치 제거 · "
    "그룹 중위 대비 60% 미만 저가 특수거래 의심 제외")

MAD_Z_THRESHOLD = 3.5      # 로버스트 이상치 기준
SPECIAL_LOW_RATIO = 0.6    # 그룹 중위가 대비 이 비율 미만이면 특수거래 의심
AREA_BUCKET_M2 = 10.0      # 면적 버킷 폭


@dataclass
class CleanResult:
    kept: list[Transaction]
    removed: dict[str, list[Transaction]] = field(default_factory=dict)
    rules_version: str = RULES_VERSION

    @property
    def summary(self) -> dict[str, int]:
        out = {reason: len(v) for reason, v in self.removed.items()}
        out["사용"] = len(self.kept)
        return out


def _bucket(tx: Transaction) -> tuple[str, int]:
    return (tx.complex_id, int(tx.area_m2 // AREA_BUCKET_M2))


def clean(txs: list[Transaction]) -> CleanResult:
    removed: dict[str, list[Transaction]] = {"취소": [], "중복": [], "이상(고저가)": [], "특수 의심": []}

    # 1) 취소 거래
    alive = []
    for t in txs:
        (removed["취소"] if t.canceled else alive).append(t)  # type: ignore[attr-defined]

    # 2) 중복 (단지·일자·면적·층·가격 동일)
    seen: set[tuple] = set()
    dedup: list[Transaction] = []
    for t in alive:
        key = (t.complex_id, t.trade_date, round(t.area_m2, 1), t.floor, t.price)
        if key in seen:
            removed["중복"].append(t)
        else:
            seen.add(key)
            dedup.append(t)

    # 3) 그룹별 로버스트 이상치(MAD z) + 특수 의심(저가)
    groups: dict[tuple, list[Transaction]] = {}
    for t in dedup:
        groups.setdefault(_bucket(t), []).append(t)

    kept: list[Transaction] = []
    for _, g in groups.items():
        ppsm = [t.price / t.area_m2 for t in g]
        med = median(ppsm)
        mad = median([abs(p - med) for p in ppsm]) or 1.0
        for t, p in zip(g, ppsm):
            z = 0.6745 * (p - med) / mad
            if p < med * SPECIAL_LOW_RATIO:
                removed["특수 의심"].append(t)
            elif abs(z) > MAD_Z_THRESHOLD:
                removed["이상(고저가)"].append(t)
            else:
                kept.append(t)

    kept.sort(key=lambda t: t.trade_date)
    return CleanResult(kept=kept, removed=removed)
