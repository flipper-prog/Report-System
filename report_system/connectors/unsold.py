"""미분양 커넥터 (L12 보강) — 시군구별 미분양 주택 현황.

미분양은 공급 과잉의 후행 증거이자 수요 한계의 직접 증거로, 공급 판정(③)과
교차검증된다. 준공 후 미분양(악성)은 별도로 추적한다.

데이터 경로는 두 가지를 지원한다.
  1) 파일 수집 (CSV/JSON) — 국토교통부 통계누리 다운로드 자료의 표준 적재구
  2) (확장 지점) 통계 API — 동일한 UnsoldSeries 를 반환하도록 구현

파일 스키마 (헤더 필수)
  month        YYYY-MM   기준 월
  unsold       정수      미분양 호수
  after_done   정수      준공 후 미분양 호수 (선택, 없으면 0)
"""
from __future__ import annotations

import csv
import json
import pathlib
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

REQUIRED = ("month", "unsold")


class UnsoldFormatError(RuntimeError):
    pass


@dataclass
class UnsoldPoint:
    month: str          # YYYY-MM
    unsold: int
    after_done: int = 0


@dataclass
class UnsoldSeries:
    points: list[UnsoldPoint] = field(default_factory=list)   # 월 오름차순
    skipped: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def latest(self) -> Optional[UnsoldPoint]:
        return self.points[-1] if self.points else None

    def trend_pct(self, months: int = 6) -> Optional[float]:
        """최근 N개월 미분양 증감률(%)."""
        if len(self.points) < 2:
            return None
        window = self.points[-months:] if len(self.points) >= months else self.points
        first, last = window[0].unsold, window[-1].unsold
        if first <= 0:
            return None
        return (last - first) / first * 100

    @property
    def label(self) -> str:
        t = self.trend_pct()
        cur = self.latest
        if cur is None or t is None:
            return "판정 불가"
        if t >= 20:
            return "미분양 증가 (수요 약화 신호)"
        if t <= -20:
            return "미분양 감소 (소진 진행)"
        return "미분양 보합"

    def summary(self) -> str:
        cur = self.latest
        if cur is None:
            return "미분양 자료 없음"
        t = self.trend_pct()
        parts = [f"{cur.month} 기준 {cur.unsold:,}호"]
        if cur.after_done:
            share = cur.after_done / cur.unsold * 100 if cur.unsold else 0
            parts.append(f"준공 후 {cur.after_done:,}호({share:.0f}%)")
        if t is not None:
            parts.append(f"최근 6개월 {t:+.0f}% — {self.label}")
        return " · ".join(parts)


def _to_point(row: dict, idx: int, skipped: list[str]) -> Optional[UnsoldPoint]:
    try:
        month = str(row["month"]).strip()[:7]
        if len(month) != 7 or month[4] != "-":
            raise ValueError("month 형식")
        unsold = int(float(str(row["unsold"]).replace(",", "")))
        after = row.get("after_done", 0)
        after_done = int(float(str(after).replace(",", ""))) if str(after).strip() else 0
    except (KeyError, ValueError, TypeError) as e:
        skipped.append(f"{idx}행: 파싱 실패 ({type(e).__name__})")
        return None
    if unsold < 0 or after_done < 0:
        skipped.append(f"{idx}행: 음수 값")
        return None
    return UnsoldPoint(month, unsold, after_done)


def load(path: str, until: date | None = None) -> UnsoldSeries:
    p = pathlib.Path(path)
    if not p.exists():
        raise UnsoldFormatError(f"미분양 파일 없음: {path}")

    if p.suffix.lower() == ".json":
        doc = json.loads(p.read_text(encoding="utf-8"))
        rows = doc if isinstance(doc, list) else doc.get("data", [])
    else:
        with p.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
            if missing:
                raise UnsoldFormatError(
                    f"필수 열 누락: {', '.join(missing)} (필요: {', '.join(REQUIRED)})")
            rows = list(reader)

    series = UnsoldSeries(source=f"미분양 파일 ({p.name})")
    cutoff = f"{until.year}-{until.month:02d}" if until else None
    for i, row in enumerate(rows, start=2):
        pt = _to_point(row, i, series.skipped)
        if pt is None:
            continue
        if cutoff and pt.month > cutoff:
            series.skipped.append(f"{i}행: 기준월({cutoff}) 이후 — 제외")
            continue
        series.points.append(pt)

    series.points.sort(key=lambda p: p.month)
    return series
