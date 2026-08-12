"""매물·호가 선행 신호 수집 (P1-2).

실거래는 후행 지표이므로 매물량 증감과 호가-실거래 갭을 선행 신호로 함께
추적한다. 다만 매물·호가는 **무료 공개 API가 없다.** 민간 데이터는 라이선스
협의 대상이므로, 본 커넥터는 다음 두 경로를 지원한다.

  1) 파일 수집 (CSV/JSON) — 라이선스 확보 데이터 또는 수기 집계의 표준 적재구
  2) (확장 지점) 라이선스 API — 동일한 ListingSnapshot 을 반환하도록 구현

파일 스키마 (헤더 필수, 열 순서 무관)
  asof          YYYY-MM-DD    관측 기준일
  listings      정수           매물 건수
  ask_ppsm      실수           호가 ㎡당 (원)
  traded_ppsm   실수           실거래 ㎡당 (원)

부적합 행은 건너뛰고 사유를 반환하여 리포트의 LIMITATION 으로 노출한다.
"""
from __future__ import annotations

import csv
import json
import pathlib
from dataclasses import dataclass
from datetime import date

from ..models import ListingSnapshot

REQUIRED = ("asof", "listings", "ask_ppsm", "traded_ppsm")


class ListingsFormatError(RuntimeError):
    pass


@dataclass
class LoadResult:
    snapshots: list[ListingSnapshot]      # asof 오름차순
    skipped: list[str]
    source: str

    @property
    def latest_pair(self) -> tuple[ListingSnapshot, ListingSnapshot] | None:
        """조기경보 비교용 (직전, 최신). 2건 미만이면 None."""
        if len(self.snapshots) < 2:
            return None
        return self.snapshots[-2], self.snapshots[-1]


def _row_to_snapshot(row: dict, idx: int, skipped: list[str]) -> ListingSnapshot | None:
    try:
        asof = date.fromisoformat(str(row["asof"]).strip())
        listings = int(float(str(row["listings"]).replace(",", "")))
        ask = float(str(row["ask_ppsm"]).replace(",", ""))
        traded = float(str(row["traded_ppsm"]).replace(",", ""))
    except (KeyError, ValueError, TypeError) as e:
        skipped.append(f"{idx}행: 파싱 실패 ({type(e).__name__})")
        return None
    if listings < 0 or ask <= 0 or traded <= 0:
        skipped.append(f"{idx}행: 값 범위 오류 (매물 {listings}, 호가 {ask}, 실거래 {traded})")
        return None
    return ListingSnapshot(asof, listings, ask, traded)


def load(path: str, until: date | None = None) -> LoadResult:
    """CSV 또는 JSON 파일에서 매물 스냅숏을 읽는다.

    until 이 주어지면 그 이후 관측은 제외한다(미래 정보 누출 방지).
    """
    p = pathlib.Path(path)
    if not p.exists():
        raise ListingsFormatError(f"매물 파일 없음: {path}")

    rows: list[dict]
    if p.suffix.lower() == ".json":
        doc = json.loads(p.read_text(encoding="utf-8"))
        rows = doc if isinstance(doc, list) else doc.get("data", [])
    else:
        with p.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
            if missing:
                raise ListingsFormatError(
                    f"필수 열 누락: {', '.join(missing)} (필요: {', '.join(REQUIRED)})")
            rows = list(reader)

    skipped: list[str] = []
    snaps: list[ListingSnapshot] = []
    for i, row in enumerate(rows, start=2):      # 헤더 다음 행부터
        s = _row_to_snapshot(row, i, skipped)
        if s is None:
            continue
        if until is not None and s.asof > until:
            skipped.append(f"{i}행: 기준일({until}) 이후 관측 — 제외")
            continue
        snaps.append(s)

    snaps.sort(key=lambda s: s.asof)
    return LoadResult(snaps, skipped, source=f"매물·호가 파일 ({p.name})")
