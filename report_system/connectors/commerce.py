"""상권 커넥터 (L9) — 소상공인시장진흥공단 상가업소 정보.

공공데이터포털 API로 현장 좌표 반경 내 업소를 수집해 업종 구성·생활 필수
업종 충족도를 산출한다. 카드소비(L10)는 민간 라이선스 대상이므로 미구현이며,
본 모듈은 '점포 수·업종 구성' 수준의 공공 대체 분석만 제공한다.

엔드포인트: apis.data.go.kr/B553077/api/open/sdsc2/storeListInRadius
인증키: DATA_GO_KR_API_KEY (실거래·청약과 동일)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from .base import Fetcher

URL = "https://apis.data.go.kr/B553077/api/open/sdsc2/storeListInRadius"
SOURCE = "소상공인시장진흥공단 상가업소 (L9)"
PAGE_SIZE = 1000
MAX_PAGES = 5

# 생활 필수 업종 — 대분류 코드가 아닌 명칭 부분일치로 판정(스키마 방어)
ESSENTIAL = {
    "식료품": ("슈퍼", "마트", "편의점", "식료품"),
    "의료": ("병원", "의원", "약국", "치과", "한의원"),
    "교육": ("학원", "교습", "어린이집", "유치원"),
    "금융": ("은행", "금융"),
    "카페·음식": ("커피", "카페", "음식", "restaurant"),
}


class CommerceApiError(RuntimeError):
    pass


@dataclass
class CommerceStats:
    total_stores: int
    radius_m: int
    by_major: dict[str, int] = field(default_factory=dict)
    essential_hits: dict[str, int] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    @property
    def essential_coverage(self) -> float:
        """생활 필수 업종 카테고리 중 최소 1개 이상 존재하는 비율."""
        if not ESSENTIAL:
            return 0.0
        return sum(1 for k in ESSENTIAL if self.essential_hits.get(k, 0) > 0) / len(ESSENTIAL)

    @property
    def label(self) -> str:
        c = self.essential_coverage
        if self.total_stores == 0:
            return "판정 불가"
        if c >= 0.8:
            return "생활 인프라 충족"
        if c >= 0.5:
            return "생활 인프라 보통"
        return "생활 인프라 부족"

    def summary(self) -> str:
        if self.total_stores == 0:
            return f"반경 {self.radius_m}m 내 업소 0건 — 수집 실패 또는 저밀도 지역"
        top = sorted(self.by_major.items(), key=lambda kv: -kv[1])[:3]
        top_s = ", ".join(f"{k} {v}건" for k, v in top)
        return (f"반경 {self.radius_m}m 내 {self.total_stores:,}개 업소 · "
                f"필수업종 충족 {self.essential_coverage:.0%} ({self.label}) · 상위: {top_s}")


def _classify(name: str) -> Optional[str]:
    low = name.lower()
    for cat, keys in ESSENTIAL.items():
        if any(k.lower() in low for k in keys):
            return cat
    return None


def fetch_radius(fetcher: Fetcher, key: str, lng: float, lat: float,
                 radius_m: int = 1000) -> CommerceStats:
    stats = CommerceStats(total_stores=0, radius_m=radius_m)
    page = 1
    while page <= MAX_PAGES:
        body = fetcher.get(SOURCE, URL, {
            "serviceKey": key, "radius": radius_m, "cx": lng, "cy": lat,
            "numOfRows": PAGE_SIZE, "pageNo": page, "type": "json"})
        try:
            doc = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise CommerceApiError(f"{SOURCE}: JSON 해석 실패 — {e}") from e

        header = (doc.get("header") or {})
        code = str(header.get("resultCode", "00"))
        if code not in ("00", "0", ""):
            raise CommerceApiError(f"{SOURCE} 오류 [{code}] {header.get('resultMsg')}")

        items = ((doc.get("body") or {}).get("items")) or []
        if not items:
            break

        for it in items:
            stats.total_stores += 1
            major = str(it.get("indsLclsNm") or it.get("indsMclsNm") or "기타")
            stats.by_major[major] = stats.by_major.get(major, 0) + 1
            label = str(it.get("indsSclsNm") or it.get("bizesNm") or major)
            cat = _classify(f"{major} {label}")
            if cat:
                stats.essential_hits[cat] = stats.essential_hits.get(cat, 0) + 1

        total = int((doc.get("body") or {}).get("totalCount") or 0)
        if page * PAGE_SIZE >= total:
            break
        page += 1

    if page > MAX_PAGES:
        stats.limitations.append(
            f"페이지 상한({MAX_PAGES})에 도달 — 업소 수가 과소 집계되었을 수 있음")
    if stats.total_stores == 0:
        stats.limitations.append("반경 내 업소 0건 — 좌표·반경 확인 필요")
    stats.limitations.append("카드소비(L10)는 민간 라이선스 대상으로 미반영 [LIMITATION]")
    return stats
