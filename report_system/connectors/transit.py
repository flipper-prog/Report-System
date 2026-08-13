"""교통망·접근성 커넥터 (L8) — 버스 정류장 근접도 · 역세권 도보 시간.

접근성은 분양 현장에서 가장 자주 과장되는 항목이다("역세권", "더블 역세권",
"도보 5분"). 본 커넥터는 그 주장을 **좌표로 검증 가능한 수치**로 바꾼다.

  · 버스 — 국토교통부 TAGO 정류소정보 API(좌표 근접 조회)로 실제 정류장을
    수집하고, 현장 좌표와의 거리를 직접 계산해 반경별로 집계한다.
  · 철도 — 무료 좌표 API가 노선별로 흩어져 있어, 역 좌표 파일(CSV/JSON)을
    적재해 최근접역·도보 시간을 산출한다.

거리는 직선거리이며 보행 보정계수(geo.DETOUR_FACTOR)를 적용해 도보 시간으로
환산한다. 배차 간격·환승 편의·실제 보행 경로는 반영되지 않으므로 한계를
항상 병기한다.

역 좌표 파일 스키마 (헤더 필수)
  name   문자열   역명
  lat    숫자     위도
  lng    숫자     경도
  lines  문자열   노선 (선택, 쉼표 구분)
"""
from __future__ import annotations

import csv
import json
import pathlib
from dataclasses import dataclass, field
from typing import Optional

from ..geo import DETOUR_FACTOR, WALK_M_PER_MIN, haversine_m, walk_minutes
from .base import Fetcher

URL = "http://apis.data.go.kr/1613000/BusSttnInfoInqireService/getCrdntPrxmtSttnList"
SOURCE = "국토교통부 TAGO 정류소정보 (L8)"
STATION_REQUIRED = ("name", "lat", "lng")

#: 역세권 판정 — 도보 시간(분) 기준
WALK_PRIME = 10.0
WALK_NEAR = 15.0

#: 버스 접근성 판정 — 도보 5분에 해당하는 직선거리(m)와 정류장 수 기준
BUS_WALK_MIN = 5.0
BUS_WALK_M = BUS_WALK_MIN * WALK_M_PER_MIN / DETOUR_FACTOR   # 약 258m
BUS_GOOD = 3
BUS_FAIR = 1


class TransitApiError(RuntimeError):
    pass


class StationFormatError(RuntimeError):
    pass


@dataclass
class Stop:
    name: str
    lat: float
    lng: float
    dist_m: float

    @property
    def walk_min(self) -> float:
        return walk_minutes(self.dist_m)


@dataclass
class Station:
    name: str
    lat: float
    lng: float
    dist_m: float
    lines: list[str] = field(default_factory=list)

    @property
    def walk_min(self) -> float:
        return walk_minutes(self.dist_m)


@dataclass
class TransitAccess:
    lat: float
    lng: float
    radius_m: int = 500
    stops: list[Stop] = field(default_factory=list)          # 거리 오름차순
    stations: list[Station] = field(default_factory=list)     # 거리 오름차순
    skipped: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    # ── 집계 ────────────────────────────────────────────────────────────────
    def stops_within(self, meters: float) -> int:
        return sum(1 for s in self.stops if s.dist_m <= meters)

    @property
    def nearest_stop(self) -> Optional[Stop]:
        return self.stops[0] if self.stops else None

    @property
    def nearest_station(self) -> Optional[Station]:
        return self.stations[0] if self.stations else None

    @property
    def station_label(self) -> str:
        st = self.nearest_station
        if st is None:
            return "역 자료 미수집"
        w = st.walk_min
        if w <= WALK_PRIME:
            return f"역세권 (도보 {w:.0f}분)"
        if w <= WALK_NEAR:
            return f"준역세권 (도보 {w:.0f}분)"
        return f"역세권 외 (도보 {w:.0f}분)"

    @property
    def bus_label(self) -> str:
        if not self.stops:
            return "정류장 자료 미수집"
        near = self.stops_within(BUS_WALK_M)
        if near >= BUS_GOOD:
            return f"버스 접근 양호 (도보 5분 내 {near}개소)"
        if near >= BUS_FAIR:
            return f"버스 접근 보통 (도보 5분 내 {near}개소)"
        return "버스 접근 취약 (도보 5분 내 없음)"

    @property
    def label(self) -> str:
        st = self.nearest_station
        if st is not None and st.walk_min <= WALK_PRIME:
            return "대중교통 접근 우수"
        if (st is not None and st.walk_min <= WALK_NEAR) or \
                self.stops_within(BUS_WALK_M) >= BUS_GOOD:
            return "대중교통 접근 보통"
        if not self.stops and st is None:
            return "판정 불가"
        return "대중교통 접근 취약"

    def ad_note(self) -> str:
        """광고 표현 가능 범위 — 검증된 수치만 문장으로 허용한다."""
        st = self.nearest_station
        if st is None:
            return "역 관련 표현은 근거 미확보 — 사용 금지"
        if st.walk_min <= WALK_PRIME:
            return (f"'{st.name} 도보 {st.walk_min:.0f}분'까지 표기 가능 "
                    f"(직선 {st.dist_m:,.0f}m · 보행 보정 적용)")
        return (f"'역세권' 표현 사용 불가 — 최근접 {st.name} 도보 {st.walk_min:.0f}분 "
                f"(직선 {st.dist_m:,.0f}m)")

    def summary(self) -> str:
        parts = [self.label]
        if self.stations:
            parts.append(self.station_label)
        if self.stops:
            parts.append(f"반경 {self.radius_m}m 정류장 {len(self.stops)}개소 · "
                         f"{self.bus_label}")
        return " · ".join(parts)


# ── 버스 정류소 (TAGO) ───────────────────────────────────────────────────────

def _items(doc: dict) -> list[dict]:
    body = ((doc.get("response") or {}).get("body") or {})
    items = body.get("items")
    if not items:
        return []
    if isinstance(items, str):      # 결과 0건일 때 빈 문자열로 오는 경우
        return []
    item = items.get("item") if isinstance(items, dict) else items
    if item is None:
        return []
    return item if isinstance(item, list) else [item]


def fetch_stops(fetcher: Fetcher, key: str, lat: float, lng: float,
                radius_m: int = 500, rows: int = 100) -> list[Stop]:
    """현장 좌표 주변 정류장을 수집하고 직선거리로 필터링한다."""
    body = fetcher.get(SOURCE, URL, {
        "serviceKey": key, "gpsLati": lat, "gpsLong": lng,
        "numOfRows": rows, "pageNo": 1, "_type": "json"})
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise TransitApiError(f"{SOURCE}: JSON 해석 실패 — {e}") from e

    header = ((doc.get("response") or {}).get("header") or {})
    code = str(header.get("resultCode", "00"))
    if code not in ("00", "0", ""):
        raise TransitApiError(f"{SOURCE} 오류 [{code}] {header.get('resultMsg')}")

    stops: list[Stop] = []
    for it in _items(doc):
        try:
            slat = float(it["gpslati"])
            slng = float(it["gpslong"])
        except (KeyError, TypeError, ValueError):
            continue
        d = haversine_m(lat, lng, slat, slng)
        if d <= radius_m:
            stops.append(Stop(str(it.get("nodenm") or "이름 미상"), slat, slng, d))
    stops.sort(key=lambda s: s.dist_m)
    return stops


# ── 역 좌표 파일 ─────────────────────────────────────────────────────────────

def load_stations(path: str, lat: float, lng: float,
                  max_m: float = 3000.0) -> tuple[list[Station], list[str]]:
    """역 좌표 파일에서 현장 반경 내 역을 거리순으로 반환한다."""
    p = pathlib.Path(path)
    if not p.exists():
        raise StationFormatError(f"역 좌표 파일 없음: {path}")

    if p.suffix.lower() == ".json":
        doc = json.loads(p.read_text(encoding="utf-8"))
        rows = doc if isinstance(doc, list) else list(doc.get("data", []))
    else:
        with p.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            missing = [c for c in STATION_REQUIRED
                       if c not in (reader.fieldnames or [])]
            if missing:
                raise StationFormatError(
                    f"필수 열 누락: {', '.join(missing)} "
                    f"(필요: {', '.join(STATION_REQUIRED)})")
            rows = list(reader)

    out: list[Station] = []
    skipped: list[str] = []
    for i, row in enumerate(rows, start=2):
        try:
            name = str(row["name"]).strip()
            slat, slng = float(row["lat"]), float(row["lng"])
        except (KeyError, TypeError, ValueError) as e:
            skipped.append(f"{i}행: 파싱 실패 ({type(e).__name__})")
            continue
        d = haversine_m(lat, lng, slat, slng)
        if d > max_m:
            continue
        lines = [s.strip() for s in str(row.get("lines") or "").split(",") if s.strip()]
        out.append(Station(name, slat, slng, d, lines))
    out.sort(key=lambda s: s.dist_m)
    return out, skipped


# ── 통합 ─────────────────────────────────────────────────────────────────────

def collect(fetcher: Fetcher | None, key: str | None, lat: float, lng: float,
            radius_m: int = 500, stations_file: str | None = None) -> TransitAccess:
    """버스(API)·철도(파일)를 합쳐 접근성 결과를 만든다.

    한쪽 자료만 있어도 결과를 반환하며, 없는 축은 한계로 표기된다.
    """
    acc = TransitAccess(lat=lat, lng=lng, radius_m=radius_m)

    if fetcher is not None and key:
        acc.stops = fetch_stops(fetcher, key, lat, lng, radius_m)
        if not acc.stops:
            acc.limitations.append(f"반경 {radius_m}m 내 정류장 0개소 — 좌표 확인 필요")
    else:
        acc.limitations.append("버스 정류장 미수집 (인증키 없음) [LIMITATION]")

    if stations_file:
        acc.stations, acc.skipped = load_stations(stations_file, lat, lng)
        if not acc.stations:
            acc.limitations.append("반경 3km 내 역 없음 — 역세권 표현 사용 불가")
    else:
        acc.limitations.append(
            "역 좌표 파일 미지정 — 역세권 판정 불가 [LIMITATION]")

    acc.limitations.append(
        "직선거리 기반 도보 환산(보정계수 1.3) — 실제 보행 경로·고저차·신호는 "
        "미반영. 배차 간격·환승 편의도 평가에 포함되지 않음 [LIMITATION]")
    return acc
