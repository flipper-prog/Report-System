"""인구이동 커넥터 (L3) — 전입·전출·순이동, 유입 출발지 구성.

인구이동은 상주인구(L1)의 **선행 신호**다. 인구 총량은 정체 상태라도 순유입이
지속되면 주거 수요는 유지되며, 반대로 순유출 국면에서는 인구 총량이 아직
줄지 않았어도 수요가 먼저 꺾인다. 따라서 L1과 분리해 별도 판단한다.

유입 출발지 구성은 현장 방문객 거주지(FieldFeedback.visitor_home_regions)와
교차검증된다. 통계상 유입이 거의 없는 지역에서 방문객이 몰린다면 분석이
틀렸거나 광고 타겟팅이 실제 수요와 어긋난 것이므로, 어느 쪽이든 재검토
대상이다 (제안서 5.11.3 현장 반응 정합성).

데이터 경로
  1) KOSIS 통계 API — `fetch_kosis()` (KOSIS_API_KEY, 표/항목 코드는 설정 주입)
  2) 파일 적재 (CSV/JSON) — `load()`. 통계누리·KOSIS 다운로드 자료의 표준 적재구

파일 스키마 (헤더 필수)
  period       YYYY 또는 YYYY-MM   기준 시점
  moved_in     정수                전입자 수
  moved_out    정수                전출자 수
  from_region  문자열 (선택)       유입 출발지 — 있으면 출발지 구성 집계

같은 period 의 행이 여러 개면 moved_in·moved_out 을 합산한다. 따라서 출발지별로
행을 나눌 때 전출(moved_out)은 해당 월에 **한 번만** 기재한다(나머지 행은 0).
전출은 출발지가 아니라 목적지별로 갈리므로 출발지 행에 분배할 수 없다.
"""
from __future__ import annotations

import csv
import json
import os
import pathlib
from dataclasses import dataclass, field
from typing import Optional

from .base import Fetcher

REQUIRED = ("period", "moved_in", "moved_out")

KOSIS_URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"
KOSIS_KEY_ENV = "KOSIS_API_KEY"
SOURCE = "인구이동 (L3)"

#: 순이동률(인구 1천명당) 판정 기준 — 통계청 시군구 분포의 상·하위 구간
INFLOW_RATE = 2.0
OUTFLOW_RATE = -2.0

#: 인구 미상 시 대체 기준 — 순이동 / 총이동(전입+전출)
INFLOW_SHARE = 0.05


class MigrationFormatError(RuntimeError):
    pass


class KosisApiError(RuntimeError):
    pass


@dataclass
class MigrationPoint:
    period: str
    moved_in: int
    moved_out: int

    @property
    def net(self) -> int:
        return self.moved_in - self.moved_out


@dataclass
class MigrationSeries:
    points: list[MigrationPoint] = field(default_factory=list)   # 시점 오름차순
    inflow_sources: dict[str, int] = field(default_factory=dict)
    population: Optional[int] = None
    skipped: list[str] = field(default_factory=list)
    source: str = ""
    limitations: list[str] = field(default_factory=list)

    # ── 집계 ────────────────────────────────────────────────────────────────
    @property
    def latest(self) -> Optional[MigrationPoint]:
        return self.points[-1] if self.points else None

    def net_recent(self, n: int = 12) -> Optional[int]:
        """최근 n개 시점의 순이동 합계."""
        if not self.points:
            return None
        return sum(p.net for p in self.points[-n:])

    def net_rate_per_1000(self, n: int = 12) -> Optional[float]:
        """최근 n개 시점 순이동을 인구 1천명당 비율로 환산한다."""
        net = self.net_recent(n)
        if net is None or not self.population:
            return None
        return net / self.population * 1000

    @property
    def label(self) -> str:
        net = self.net_recent()
        if net is None:
            return "판정 불가"
        rate = self.net_rate_per_1000()
        if rate is not None:
            if rate >= INFLOW_RATE:
                return "순유입 지속"
            if rate <= OUTFLOW_RATE:
                return "순유출 지속 (수요 기반 약화)"
            return "이동 균형"
        # 인구 미상 — 총이동 대비 순이동 비중으로 대체 판정
        gross = sum(p.moved_in + p.moved_out for p in self.points[-12:])
        if gross <= 0:
            return "판정 불가"
        share = net / gross
        if share >= INFLOW_SHARE:
            return "순유입 지속"
        if share <= -INFLOW_SHARE:
            return "순유출 지속 (수요 기반 약화)"
        return "이동 균형"

    def top_sources(self, n: int = 5) -> list[tuple[str, float]]:
        """유입 출발지 상위 n개와 비중."""
        total = sum(self.inflow_sources.values())
        if total <= 0:
            return []
        ranked = sorted(self.inflow_sources.items(), key=lambda kv: -kv[1])[:n]
        return [(k, v / total) for k, v in ranked]

    def visitor_alignment(self, visitor_home_regions: dict[str, int],
                          top_n: int = 5) -> Optional[tuple[float, str]]:
        """현장 방문객 거주지가 통계상 유입 출발지와 정합하는지 평가한다.

        반환: (정합 비율, 판정 문장). 어느 한쪽 자료가 없으면 None.
        """
        if not visitor_home_regions or not self.inflow_sources:
            return None
        top = {k for k, _ in self.top_sources(top_n)}
        total_visits = sum(visitor_home_regions.values())
        if total_visits <= 0:
            return None
        matched = sum(v for k, v in visitor_home_regions.items()
                      if any(t in k or k in t for t in top))
        share = matched / total_visits
        if share >= 0.5:
            note = (f"방문객의 {share:.0%}가 통계상 주요 유입 출발지에서 유입 — "
                    "타겟 지역과 실제 수요가 정합")
        elif share >= 0.25:
            note = (f"방문객의 {share:.0%}만 주요 유입 출발지와 일치 — "
                    "광고 타겟 지역 재검토 권고")
        else:
            note = (f"방문객의 {share:.0%}만 주요 유입 출발지와 일치 — "
                    "인구이동 기반 수요 가정과 현장 반응이 어긋남. 재검토 필요")
        return share, note

    def summary(self) -> str:
        if not self.points:
            return "인구이동 자료 없음"
        net = self.net_recent()
        parts = [f"{self.points[-1].period} 기준 최근 {min(12, len(self.points))}개 시점 "
                 f"순이동 {net:+,}명"]
        rate = self.net_rate_per_1000()
        if rate is not None:
            parts.append(f"인구 1천명당 {rate:+.1f}명")
        parts.append(self.label)
        top = self.top_sources(3)
        if top:
            parts.append("유입 상위: " + ", ".join(f"{k} {s:.0%}" for k, s in top))
        return " · ".join(parts)


# ── 파일 적재 ────────────────────────────────────────────────────────────────

def _to_point(row: dict, idx: int, skipped: list[str]) -> Optional[MigrationPoint]:
    try:
        period = str(row["period"]).strip()[:7]
        if len(period) not in (4, 7):
            raise ValueError("period 형식")
        moved_in = int(float(str(row["moved_in"]).replace(",", "")))
        moved_out = int(float(str(row["moved_out"]).replace(",", "")))
    except (KeyError, ValueError, TypeError) as e:
        skipped.append(f"{idx}행: 파싱 실패 ({type(e).__name__})")
        return None
    if moved_in < 0 or moved_out < 0:
        skipped.append(f"{idx}행: 음수 값")
        return None
    return MigrationPoint(period, moved_in, moved_out)


def _read_rows(p: pathlib.Path) -> list[dict]:
    if p.suffix.lower() == ".json":
        doc = json.loads(p.read_text(encoding="utf-8"))
        return doc if isinstance(doc, list) else list(doc.get("data", []))
    with p.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise MigrationFormatError(
                f"필수 열 누락: {', '.join(missing)} (필요: {', '.join(REQUIRED)})")
        return list(reader)


def load(path: str, until: str | None = None,
         population: int | None = None) -> MigrationSeries:
    """인구이동 파일을 적재한다. until(YYYY-MM) 이후 시점은 제외한다."""
    p = pathlib.Path(path)
    if not p.exists():
        raise MigrationFormatError(f"인구이동 파일 없음: {path}")

    rows = _read_rows(p)
    series = MigrationSeries(source=f"인구이동 파일 ({p.name})", population=population)
    merged: dict[str, MigrationPoint] = {}
    for i, row in enumerate(rows, start=2):
        pt = _to_point(row, i, series.skipped)
        if pt is None:
            continue
        if until and pt.period[:len(until)] > until:
            series.skipped.append(f"{i}행: 기준 시점({until}) 이후 — 제외")
            continue
        # 출발지별로 행이 나뉜 자료를 시점 단위로 합산한다
        cur = merged.get(pt.period)
        if cur is None:
            merged[pt.period] = pt
        else:
            merged[pt.period] = MigrationPoint(
                pt.period, cur.moved_in + pt.moved_in, cur.moved_out + pt.moved_out)
        src = str(row.get("from_region") or "").strip()
        if src:
            series.inflow_sources[src] = series.inflow_sources.get(src, 0) + pt.moved_in

    series.points = [merged[k] for k in sorted(merged)]
    if not series.inflow_sources:
        series.limitations.append(
            "유입 출발지(from_region) 미제공 — 방문객 거주지 교차검증 불가 [LIMITATION]")
    if population is None:
        series.limitations.append(
            "기준 인구 미지정 — 순이동률 대신 총이동 대비 비중으로 판정 [LIMITATION]")
    return series


# ── KOSIS API ────────────────────────────────────────────────────────────────

def kosis_key() -> str:
    key = os.environ.get(KOSIS_KEY_ENV, "").strip()
    if not key:
        raise KosisApiError(
            f"환경변수 {KOSIS_KEY_ENV} 가 설정되지 않았습니다.\n"
            "  https://kosis.kr/openapi 에서 사용자 등록 후 활용신청하십시오.")
    return key


def fetch_kosis(fetcher: Fetcher, key: str, spec: dict,
                population: int | None = None) -> MigrationSeries:
    """KOSIS 통계 API로 인구이동 시계열을 수집한다.

    spec 에는 표를 특정하는 KOSIS 파라미터를 그대로 넘긴다
    (orgId, tblId, objL1, prdSe, startPrdDe, endPrdDe 등).
    항목 코드는 spec['item_in'] / spec['item_out'] 으로 지정한다 — 표마다
    항목 코드가 다르므로 커넥터가 값을 고정하지 않는다.
    """
    item_in = str(spec.get("item_in", ""))
    item_out = str(spec.get("item_out", ""))
    if not item_in or not item_out:
        raise KosisApiError("spec 에 item_in / item_out (KOSIS 항목 코드) 필요")

    params = {k: v for k, v in spec.items()
              if k not in ("item_in", "item_out", "population")}
    params.update({"method": "getList", "apiKey": key,
                   "format": "json", "jsonVD": "Y"})
    body = fetcher.get(SOURCE, KOSIS_URL, params)
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise KosisApiError(f"{SOURCE}: JSON 해석 실패 — {e}") from e
    if isinstance(doc, dict):   # 오류 응답은 dict 로 온다
        raise KosisApiError(f"KOSIS 오류 [{doc.get('err')}] {doc.get('errMsg')}")

    series = MigrationSeries(source="KOSIS 인구이동 API", population=population)
    buckets: dict[str, dict[str, int]] = {}
    for row in doc:
        period = str(row.get("PRD_DE") or "")
        if len(period) == 6:
            period = f"{period[:4]}-{period[4:]}"
        itm = str(row.get("ITM_ID") or "")
        try:
            val = int(float(str(row.get("DT") or 0).replace(",", "")))
        except ValueError:
            series.skipped.append(f"{period}/{itm}: 수치 해석 실패")
            continue
        b = buckets.setdefault(period, {"in": 0, "out": 0})
        if itm == item_in:
            b["in"] += val
        elif itm == item_out:
            b["out"] += val

    series.points = [MigrationPoint(p, v["in"], v["out"])
                     for p, v in sorted(buckets.items())]
    series.limitations.append(
        "KOSIS 표 단위(시군구·월)로 수집 — 생활권 단위 이동은 반영되지 않음 [LIMITATION]")
    if population is None:
        series.limitations.append("기준 인구 미지정 — 순이동률 미산출 [LIMITATION]")
    return series
