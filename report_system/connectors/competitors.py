"""경쟁 현장 스냅숏 적재 (P2-6 입력부).

경쟁 현장의 분양가·혜택·잔여 세대는 공개 API가 없다. 현장 담당자가 주기적으로
답사·전화로 확인해 기록하는 자료이므로, 이 커넥터는 그 기록을 파일로 받아
회차별 스냅숏으로 정리한다.

핵심은 **같은 현장의 두 시점을 짝지어 주는 것**이다. 변화 감지(가격 인하, 혜택
추가, 잔여 세대 증가)는 두 시점이 있어야 성립하고, 한 시점만 있으면 '변화 없음'이
아니라 '비교 불가'다. 그 구분을 여기서 만든다.

파일 스키마 (헤더 필수: name, asof, price_per_m2, remaining_units)
  name             문자열     경쟁 현장명 (시점 간 동일해야 짝지어진다)
  asof             YYYY-MM-DD 수집 일자
  price_per_m2     숫자(원)   대표 타입 ㎡당 분양가
  remaining_units  정수       잔여 세대
  incentives       문자열     혜택, 쉼표 구분 (선택)
  note             문자열     비고 (선택)
"""
from __future__ import annotations

import csv
import json
import pathlib
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from ..competitor import CompetitorSnapshot

REQUIRED = ("name", "asof", "price_per_m2", "remaining_units")


class CompetitorFormatError(RuntimeError):
    pass


@dataclass
class CompetitorRounds:
    """최신 회차와 그 직전 회차. 비교가 불가능한 경우도 사유와 함께 남긴다."""
    latest: list[CompetitorSnapshot] = field(default_factory=list)
    previous: list[CompetitorSnapshot] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    source: str = ""
    limitations: list[str] = field(default_factory=list)

    @property
    def comparable(self) -> bool:
        """같은 이름이 두 회차에 모두 있어야 변화를 말할 수 있다."""
        return bool(self.previous) and bool(
            {s.name for s in self.latest} & {s.name for s in self.previous})

    def summary(self) -> str:
        if not self.latest:
            return "경쟁 현장 자료 없음"
        parts = [f"{len(self.latest)}개 현장 (기준 {self.latest[0].asof})"]
        if self.comparable:
            paired = len({s.name for s in self.latest}
                         & {s.name for s in self.previous})
            parts.append(f"직전 회차 {self.previous[0].asof} 대비 {paired}개 비교")
        else:
            parts.append("직전 회차 없음 — 변화 감지 불가")
        return " · ".join(parts)


def _to_snapshot(row: dict, idx: int, skipped: list[str],
                 until: Optional[date]) -> Optional[CompetitorSnapshot]:
    try:
        name = str(row["name"]).strip()
        when = date.fromisoformat(str(row["asof"]).strip()[:10])
        price = float(str(row["price_per_m2"]).replace(",", ""))
        remaining = int(float(str(row["remaining_units"]).replace(",", "")))
    except (KeyError, ValueError, TypeError) as e:
        skipped.append(f"{idx}행: 파싱 실패 ({type(e).__name__})")
        return None
    if not name:
        skipped.append(f"{idx}행: 현장명 없음")
        return None
    if price <= 0 or remaining < 0:
        skipped.append(f"{idx}행: 가격·잔여 세대 값 오류")
        return None
    if until and when > until:
        skipped.append(f"{idx}행: 기준일({until}) 이후 수집 — 제외")
        return None
    incentives = [x.strip() for x in str(row.get("incentives") or "").split(",")
                  if x.strip()]
    return CompetitorSnapshot(name, when, price, remaining, incentives,
                              str(row.get("note") or "").strip())


def load(path: str, until: date | None = None) -> CompetitorRounds:
    """스냅숏 파일에서 최신 회차와 직전 회차를 뽑아낸다."""
    p = pathlib.Path(path)
    if not p.exists():
        raise CompetitorFormatError(f"경쟁 현장 파일 없음: {path}")

    if p.suffix.lower() == ".json":
        doc = json.loads(p.read_text(encoding="utf-8"))
        rows = doc if isinstance(doc, list) else list(doc.get("data", []))
    else:
        with p.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
            if missing:
                raise CompetitorFormatError(
                    f"필수 열 누락: {', '.join(missing)} (필요: {', '.join(REQUIRED)})")
            rows = list(reader)

    out = CompetitorRounds(source=f"경쟁 현장 파일 ({p.name})")
    by_date: dict[date, list[CompetitorSnapshot]] = {}
    for i, row in enumerate(rows, start=2):
        s = _to_snapshot(row, i, out.skipped, until)
        if s is not None:
            by_date.setdefault(s.asof, []).append(s)

    dates = sorted(by_date, reverse=True)
    if dates:
        out.latest = by_date[dates[0]]
    if len(dates) > 1:
        out.previous = by_date[dates[1]]

    if not out.latest:
        out.limitations.append("유효한 경쟁 현장 스냅숏 없음")
    elif not out.comparable:
        out.limitations.append(
            "직전 회차가 없거나 현장명이 일치하지 않아 변화 감지 불가 — "
            "회차 간 현장명을 동일하게 기록해야 한다 [LIMITATION]")
    out.limitations.append(
        "경쟁 현장 자료는 수기 수집이므로 수집 시점·정확도가 현장 담당자에게 "
        "의존한다 [LIMITATION]")
    return out
