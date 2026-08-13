"""생활이동·O/D 커넥터 (L7) — 기종점 통행량 기반 직주근접·생활권 구조.

O/D(Origin-Destination) 자료는 "이 지역 사람들이 어디로 이동하는가"를 보여준다.
분양 분석에서의 쓰임은 두 가지다.

  1) 자족성 — 지역 내부에서 통행이 끝나는 비중. 자족성이 높으면 외부 도심
     경기와 무관하게 수요가 유지되고, 낮으면 특정 도심 통근 수요에 종속된다.
  2) 유입 생활권 — 이 지역으로 들어오는 통행의 출발지. 광고 타겟 지역을
     '인접 행정구역'이 아니라 **실제 이동이 있는 지역**으로 정할 근거가 된다.

KTDB(국가교통DB)·통신사 생활이동 자료는 무료 공개 API가 없어 계약·승인 후
파일로 제공된다. 따라서 본 커넥터는 파일 적재 방식이며, API 제공 시 동일한
`ODMatrix` 를 반환하도록 확장한다.

파일 스키마 (헤더 필수)
  origin       문자열   출발지명 또는 코드
  destination  문자열   도착지명 또는 코드
  trips        숫자     통행량
  purpose      문자열   통행 목적 (선택 — 지정 시 purpose 필터 사용 가능)
"""
from __future__ import annotations

import csv
import json
import pathlib
from dataclasses import dataclass, field
from typing import Optional

REQUIRED = ("origin", "destination", "trips")

#: 자족형 생활권 판정 기준 — 내부 통행 비중
SELF_CONTAINED = 0.50

#: 단일 도심 통근 의존 판정 기준 — 최대 유출 목적지 비중
DEPENDENT_SHARE = 0.30


class MobilityFormatError(RuntimeError):
    pass


@dataclass
class ODMatrix:
    focus: str
    internal: float = 0.0                                    # 내부 → 내부
    outbound: dict[str, float] = field(default_factory=dict)  # 내부 → 외부
    inbound: dict[str, float] = field(default_factory=dict)   # 외부 → 내부
    purpose: Optional[str] = None
    skipped: list[str] = field(default_factory=list)
    source: str = ""
    limitations: list[str] = field(default_factory=list)

    # ── 집계 ────────────────────────────────────────────────────────────────
    @property
    def total_outbound(self) -> float:
        return sum(self.outbound.values())

    @property
    def total_inbound(self) -> float:
        return sum(self.inbound.values())

    @property
    def self_containment(self) -> Optional[float]:
        """지역에서 출발한 통행 중 지역 내부에서 끝나는 비중."""
        denom = self.internal + self.total_outbound
        return self.internal / denom if denom > 0 else None

    def top_destinations(self, n: int = 5) -> list[tuple[str, float]]:
        total = self.total_outbound
        if total <= 0:
            return []
        return [(k, v / total) for k, v in
                sorted(self.outbound.items(), key=lambda kv: -kv[1])[:n]]

    def top_origins(self, n: int = 5) -> list[tuple[str, float]]:
        total = self.total_inbound
        if total <= 0:
            return []
        return [(k, v / total) for k, v in
                sorted(self.inbound.items(), key=lambda kv: -kv[1])[:n]]

    @property
    def net_flow(self) -> Optional[float]:
        """유입 - 유출. 양수면 통행을 끌어들이는 지역(고용·상업 중심)."""
        if not (self.inbound or self.outbound):
            return None
        return self.total_inbound - self.total_outbound

    @property
    def label(self) -> str:
        sc = self.self_containment
        if sc is None:
            return "판정 불가"
        top = self.top_destinations(1)
        top_share = top[0][1] if top else 0.0
        if sc >= SELF_CONTAINED:
            return "자족형 생활권"
        if top_share >= DEPENDENT_SHARE:
            return f"통근 의존형 ({top[0][0]} 의존)"
        return "분산 통근형"

    def target_regions(self, n: int = 3) -> list[str]:
        """광고 타겟 후보 — 실제 유입 통행이 있는 상위 지역."""
        return [k for k, _ in self.top_origins(n)]

    def summary(self) -> str:
        sc = self.self_containment
        if sc is None:
            return "O/D 자료 없음"
        parts = [f"자족성 {sc:.0%} ({self.label})"]
        top_d = self.top_destinations(2)
        if top_d:
            parts.append("주요 통근지: " +
                         ", ".join(f"{k} {s:.0%}" for k, s in top_d))
        top_o = self.top_origins(2)
        if top_o:
            parts.append("주요 유입지: " +
                         ", ".join(f"{k} {s:.0%}" for k, s in top_o))
        net = self.net_flow
        if net is not None:
            parts.append(f"순통행 {net:+,.0f}")
        return " · ".join(parts)


def _read_rows(p: pathlib.Path) -> list[dict]:
    if p.suffix.lower() == ".json":
        doc = json.loads(p.read_text(encoding="utf-8"))
        return doc if isinstance(doc, list) else list(doc.get("data", []))
    with p.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise MobilityFormatError(
                f"필수 열 누락: {', '.join(missing)} (필요: {', '.join(REQUIRED)})")
        return list(reader)


def _matches(name: str, focus: str) -> bool:
    """행정구역 표기 차이를 흡수한 포함 매칭 ('수원시 영통구' vs '영통구')."""
    a, b = name.strip(), focus.strip()
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def load(path: str, focus: str, purpose: str | None = None) -> ODMatrix:
    """O/D 파일을 적재해 focus 지역 기준으로 집계한다.

    focus 는 현장이 속한 지역명(Site.region)을 넘긴다. purpose 를 지정하면
    해당 목적의 통행만 집계한다(예: '출근').
    """
    p = pathlib.Path(path)
    if not p.exists():
        raise MobilityFormatError(f"O/D 파일 없음: {path}")
    if not focus.strip():
        raise MobilityFormatError("focus(현장 지역명)가 비어 있습니다")

    od = ODMatrix(focus=focus, purpose=purpose,
                  source=f"생활이동·O/D 파일 ({p.name})")
    matched_rows = 0
    for i, row in enumerate(_read_rows(p), start=2):
        try:
            o = str(row["origin"]).strip()
            d = str(row["destination"]).strip()
            trips = float(str(row["trips"]).replace(",", ""))
        except (KeyError, ValueError, TypeError) as e:
            od.skipped.append(f"{i}행: 파싱 실패 ({type(e).__name__})")
            continue
        if trips < 0:
            od.skipped.append(f"{i}행: 음수 통행량")
            continue
        if purpose and str(row.get("purpose") or "").strip() != purpose:
            continue

        o_in, d_in = _matches(o, focus), _matches(d, focus)
        if o_in and d_in:
            od.internal += trips
        elif o_in:
            od.outbound[d] = od.outbound.get(d, 0.0) + trips
        elif d_in:
            od.inbound[o] = od.inbound.get(o, 0.0) + trips
        else:
            continue    # 현장과 무관한 통행 — 집계 제외
        matched_rows += 1

    if matched_rows == 0:
        od.limitations.append(
            f"'{focus}' 와 연결된 통행이 0건 — 지역명 표기가 자료와 일치하는지 확인 필요")
    if purpose:
        od.limitations.append(f"통행 목적 '{purpose}' 만 집계 — 전체 통행과 다름")
    od.limitations.append(
        "O/D 는 계약·승인 기반 제공 자료로 갱신 주기가 길다 — 최근 개통·입주 효과는 "
        "반영되지 않을 수 있음 [LIMITATION]")
    return od
