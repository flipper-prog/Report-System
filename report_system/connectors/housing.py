"""주택건설실적 커넥터 (L13) — 인허가·착공·분양·준공 물량.

공급 파이프라인은 지금까지 설정에 수기로 입력한 목록이었다. 그 목록이 맞는지
확인할 방법이 없으면, 공급 판정은 입력한 사람의 주장을 그대로 되돌려 주는
것에 지나지 않는다. 주택건설실적 통계는 두 가지 역할을 한다.

  1) 추세 — 인허가는 착공·분양의 **선행 지표**다. 인허가가 늘고 있으면 2~3년
     뒤 공급 압력이 커진다. 지금 미분양이 없어도 그렇다.
  2) 교차검증 — 설정에 입력된 공급 물량이 통계상 인허가 실적과 맞는지 본다.
     실적을 크게 웃도는 목록은 중복 집계나 취소된 사업이 섞였을 가능성이 있고,
     크게 밑돌면 누락이 있을 가능성이 있다. 어느 쪽이든 알려야 한다.

데이터 경로
  1) KOSIS 통계 API — `fetch_kosis()` (KOSIS_API_KEY, 표·항목 코드는 설정 주입)
  2) 파일 적재 (CSV/JSON) — `load()`

파일 스키마 (헤더 필수: period, permit)
  period  YYYY-MM 또는 YYYY   기준 시점
  permit  정수                인허가 호수
  start   정수 (선택)         착공 호수
  sale    정수 (선택)         분양 호수
  done    정수 (선택)         준공 호수
"""
from __future__ import annotations

import csv
import json
import pathlib
from dataclasses import dataclass, field
from typing import Optional

from .base import Fetcher
from .migration import KOSIS_URL, KosisApiError

REQUIRED = ("period", "permit")
FIELDS = ("permit", "start", "sale", "done")
LABELS = {"permit": "인허가", "start": "착공", "sale": "분양", "done": "준공"}
SOURCE = "주택건설실적 (L13)"

#: 전년 동기 대비 증감 판정 기준(%)
SURGE_PCT = 30.0
DROP_PCT = -30.0

#: 설정 공급 목록이 인허가 실적과 이만큼 어긋나면 교차검증 경고
PIPELINE_TOLERANCE = 0.50


class HousingFormatError(RuntimeError):
    pass


@dataclass
class HousingPoint:
    period: str
    permit: int
    start: int = 0
    sale: int = 0
    done: int = 0

    def get(self, name: str) -> int:
        return int(getattr(self, name, 0) or 0)


@dataclass
class HousingSeries:
    points: list[HousingPoint] = field(default_factory=list)   # 시점 오름차순
    skipped: list[str] = field(default_factory=list)
    source: str = ""
    limitations: list[str] = field(default_factory=list)

    # ── 집계 ────────────────────────────────────────────────────────────────
    @property
    def latest(self) -> Optional[HousingPoint]:
        return self.points[-1] if self.points else None

    def recent_sum(self, name: str = "permit", n: int = 12) -> Optional[int]:
        if not self.points:
            return None
        return sum(p.get(name) for p in self.points[-n:])

    def yoy_pct(self, name: str = "permit", n: int = 12) -> Optional[float]:
        """최근 n개 시점 합계의 전년 동기 대비 증감률(%)."""
        if len(self.points) < n * 2:
            return None
        recent = sum(p.get(name) for p in self.points[-n:])
        prior = sum(p.get(name) for p in self.points[-2 * n:-n])
        if prior <= 0:
            return None
        return (recent - prior) / prior * 100

    @property
    def label(self) -> str:
        y = self.yoy_pct("permit")
        if y is None:
            return "추세 판정 불가"
        if y >= SURGE_PCT:
            return "인허가 급증 (2~3년 후 공급 압력 예고)"
        if y <= DROP_PCT:
            return "인허가 감소 (중기 공급 축소)"
        return "인허가 보합"

    def has(self, name: str) -> bool:
        return any(p.get(name) for p in self.points)

    def pipeline_check(self, declared_units: int,
                       months: int = 36) -> Optional[str]:
        """설정에 입력된 공급 물량을 인허가 실적과 대조한다.

        범위가 다르다는 점이 이 검사의 전제다. 설정 목록은 현장 **인근**
        물량이고 인허가 실적은 **시군구 전체**이므로, 설정이 실적보다 작은 것은
        정상이다. 뒤집혀 있을 때 — 인근 목록이 시군구 전체 인허가를 넘어설 때 —
        만 문제이며, 그때는 중복 집계나 취소된 사업이 섞였을 가능성이 높다.
        비중이 얼마나 되는지는 '현장 인근에 얼마나 몰려 있는가'로 읽는다.
        """
        actual = self.recent_sum("permit", months)
        if not actual or declared_units <= 0:
            return None
        ratio = declared_units / actual
        if ratio > 1 + PIPELINE_TOLERANCE:
            return (f"설정 공급 {declared_units:,}세대가 최근 {months}개월 시군구 전체 "
                    f"인허가 {actual:,}호의 {ratio:.1f}배 — 인근 물량이 시군구 실적을 "
                    "넘어설 수 없으므로 중복 집계·취소 사업 포함 여부 확인 필요")
        return (f"설정 공급 {declared_units:,}세대 = 최근 {months}개월 시군구 인허가 "
                f"{actual:,}호의 {ratio:.0%} (범위가 다르므로 100% 미만이 정상)")

    def summary(self) -> str:
        if not self.points:
            return "주택건설실적 자료 없음"
        parts = [f"{self.points[-1].period} 기준"]
        for name in FIELDS:
            if self.has(name):
                s = self.recent_sum(name)
                parts.append(f"최근 12개월 {LABELS[name]} {s:,}호")
        y = self.yoy_pct("permit")
        if y is not None:
            parts.append(f"인허가 전년 대비 {y:+.0f}% — {self.label}")
        return " · ".join(parts)


# ── 파일 적재 ────────────────────────────────────────────────────────────────

def _to_point(row: dict, idx: int, skipped: list[str]) -> Optional[HousingPoint]:
    try:
        period = str(row["period"]).strip()[:7]
        if len(period) not in (4, 7):
            raise ValueError("period 형식")
        vals = {}
        for name in FIELDS:
            raw = row.get(name, 0)
            s = str(raw).replace(",", "").strip()
            vals[name] = int(float(s)) if s else 0
    except (KeyError, ValueError, TypeError) as e:
        skipped.append(f"{idx}행: 파싱 실패 ({type(e).__name__})")
        return None
    if any(v < 0 for v in vals.values()):
        skipped.append(f"{idx}행: 음수 값")
        return None
    return HousingPoint(period, **vals)


def load(path: str, until: str | None = None) -> HousingSeries:
    p = pathlib.Path(path)
    if not p.exists():
        raise HousingFormatError(f"주택건설실적 파일 없음: {path}")

    if p.suffix.lower() == ".json":
        doc = json.loads(p.read_text(encoding="utf-8"))
        rows = doc if isinstance(doc, list) else list(doc.get("data", []))
    else:
        with p.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
            if missing:
                raise HousingFormatError(
                    f"필수 열 누락: {', '.join(missing)} (필요: {', '.join(REQUIRED)})")
            rows = list(reader)

    series = HousingSeries(source=f"주택건설실적 파일 ({p.name})")
    merged: dict[str, HousingPoint] = {}
    for i, row in enumerate(rows, start=2):
        pt = _to_point(row, i, series.skipped)
        if pt is None:
            continue
        if until and pt.period[:len(until)] > until:
            series.skipped.append(f"{i}행: 기준 시점({until}) 이후 — 제외")
            continue
        cur = merged.get(pt.period)
        merged[pt.period] = pt if cur is None else HousingPoint(
            pt.period, *(cur.get(n) + pt.get(n) for n in FIELDS))

    series.points = [merged[k] for k in sorted(merged)]
    missing_fields = [LABELS[n] for n in FIELDS[1:] if not series.has(n)]
    if missing_fields:
        series.limitations.append(
            f"{'·'.join(missing_fields)} 물량 미제공 — 인허가만으로 추세 판단 [LIMITATION]")
    series.limitations.append(
        "인허가는 시군구 단위 실적으로, 현장 생활권 공급과 범위가 다르다 [LIMITATION]")
    return series


# ── KOSIS API ────────────────────────────────────────────────────────────────

def fetch_kosis(fetcher: Fetcher, key: str, spec: dict) -> HousingSeries:
    """KOSIS 주택건설실적 표를 수집한다.

    spec 에 표를 특정하는 KOSIS 파라미터(orgId, tblId, objL1, prdSe …)와
    항목 코드 매핑 `items = {"permit": "T1", "start": "T2", ...}` 를 넣는다.
    표마다 항목 코드가 달라 커넥터가 값을 고정하지 않는다.
    """
    items = spec.get("items") or {}
    if not items.get("permit"):
        raise KosisApiError("spec['items']['permit'] (KOSIS 항목 코드) 필요")
    code_to_field = {str(v): k for k, v in items.items() if k in FIELDS}

    params = {k: v for k, v in spec.items() if k != "items"}
    params.update({"method": "getList", "apiKey": key,
                   "format": "json", "jsonVD": "Y"})
    body = fetcher.get(SOURCE, KOSIS_URL, params)
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise KosisApiError(f"{SOURCE}: JSON 해석 실패 — {e}") from e
    if isinstance(doc, dict):
        raise KosisApiError(f"KOSIS 오류 [{doc.get('err')}] {doc.get('errMsg')}")

    series = HousingSeries(source="KOSIS 주택건설실적 API")
    buckets: dict[str, dict[str, int]] = {}
    for row in doc:
        period = str(row.get("PRD_DE") or "")
        if len(period) == 6:
            period = f"{period[:4]}-{period[4:]}"
        fieldname = code_to_field.get(str(row.get("ITM_ID") or ""))
        if not fieldname:
            continue
        try:
            val = int(float(str(row.get("DT") or 0).replace(",", "")))
        except ValueError:
            series.skipped.append(f"{period}/{fieldname}: 수치 해석 실패")
            continue
        buckets.setdefault(period, {})[fieldname] = \
            buckets.setdefault(period, {}).get(fieldname, 0) + val

    series.points = [HousingPoint(p, **{n: v.get(n, 0) for n in FIELDS})
                     for p, v in sorted(buckets.items())]
    series.limitations.append(
        "인허가는 시군구 단위 실적으로, 현장 생활권 공급과 범위가 다르다 [LIMITATION]")
    return series
