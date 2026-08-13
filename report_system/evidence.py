"""근거원장 — 리포트의 모든 핵심 수치에 출처·산출식·표본·한계를 붙인다.

제안서가 내세우는 차별점은 "검증 가능성"이다. 그러려면 리포트를 읽는 사람이
어떤 수치든 짚어서 **이 값은 어떤 데이터로, 어떤 식으로, 표본 몇 건에서
나왔는가**를 되물을 수 있어야 한다. 근거원장은 그 질문에 대한 답을 리포트
안에 미리 넣어 둔 표다.

각 항목은 다음을 갖는다.
  · ID        본문에서 참조하는 식별자 (E-01, E-02 …)
  · 지표·값    무엇이 얼마인가
  · 산출       어느 모듈의 어떤 규칙으로 계산했는가 (버전 포함)
  · 데이터     어떤 수집 이력(Provenance)에 기반하는가 — 출처·수집시각·해시
  · 표본       몇 건에서 나왔는가
  · 등급       FACT / CALCULATION / INFERENCE / FORECAST / LIMITATION
  · 한계       이 수치를 읽을 때 반드시 함께 봐야 하는 제약

수치를 만들지 못한 항목도 **'미산출'로 등재한다.** 없는 근거를 조용히 빼면
독자는 그 지표가 검토되지 않았는지 검토했으나 표본이 없었는지 구분할 수 없다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import ClaimGrade


@dataclass
class Source:
    """수집 이력 1건에 대한 참조. Provenance 로부터 만들거나 직접 기술한다."""
    name: str
    fetched_at: str = ""
    sha256: str = ""

    def short(self) -> str:
        if not self.sha256:
            return self.name
        return f"{self.name} ({self.fetched_at[:19]}, sha256 {self.sha256[:12]}…)"


@dataclass
class Evidence:
    id: str
    metric: str
    value: str
    method: str
    grade: ClaimGrade
    sources: list[Source] = field(default_factory=list)
    n: Optional[int] = None
    limitations: list[str] = field(default_factory=list)

    @property
    def computed(self) -> bool:
        return self.value != "미산출"


class EvidenceLedger:
    """수집 이력을 출처 사전으로 들고, 지표를 순서대로 등재한다."""

    def __init__(self, provenance: "list | None" = None):
        self._items: list[Evidence] = []
        self._sources: list[Source] = []
        for p in (provenance or []):
            src = (Source(p.source, p.fetched_at, p.sha256)
                   if hasattr(p, "source")
                   else Source(str(p.get("source", "")), str(p.get("fetched_at", "")),
                               str(p.get("sha256", ""))))
            self._sources.append(src)

    # ── 출처 조회 ───────────────────────────────────────────────────────────
    def find(self, *keywords: str) -> list[Source]:
        """수집 이력에서 키워드가 포함된 출처를 찾는다(중복 출처는 최신 1건)."""
        hits: dict[str, Source] = {}
        for s in self._sources:
            if any(k in s.name for k in keywords):
                hits[s.name] = s          # 같은 출처의 마지막 수집을 대표로
        return list(hits.values())

    # ── 등재 ────────────────────────────────────────────────────────────────
    def add(self, metric: str, value: str, method: str, grade: ClaimGrade,
            sources: "list[Source] | None" = None, n: Optional[int] = None,
            limitations: "list[str] | None" = None) -> Evidence:
        ev = Evidence(id=f"E-{len(self._items) + 1:02d}", metric=metric,
                      value=value, method=method, grade=grade,
                      sources=list(sources or []), n=n,
                      limitations=list(limitations or []))
        self._items.append(ev)
        return ev

    def add_missing(self, metric: str, reason: str,
                    method: str = "—") -> Evidence:
        """산출하지 못한 지표도 사유와 함께 등재한다."""
        return self.add(metric, "미산출", method, ClaimGrade.LIMITATION,
                        limitations=[reason])

    # ── 조회·집계 ───────────────────────────────────────────────────────────
    @property
    def items(self) -> list[Evidence]:
        return list(self._items)

    @property
    def computed_count(self) -> int:
        return sum(1 for e in self._items if e.computed)

    @property
    def sourced_count(self) -> int:
        """실제 수집 이력에 연결된 항목 수 — 합성·설정 입력과 구분된다."""
        return sum(1 for e in self._items
                   if e.computed and any(s.sha256 for s in e.sources))

    def as_markdown(self) -> str:
        if not self._items:
            return "*등재된 근거 항목이 없습니다.*"
        L = [f"*총 {len(self._items)}개 항목 — 산출 {self.computed_count} · "
             f"미산출 {len(self._items) - self.computed_count} · "
             f"수집 이력 연결 {self.sourced_count}*", "",
             "| ID | 지표 | 값 | 등급 | 표본 | 산출 근거 |",
             "|----|------|-----|------|------|-----------|"]
        for e in self._items:
            n = f"{e.n:,}" if e.n is not None else "—"
            L.append(f"| {e.id} | {e.metric} | {e.value} | {e.grade.value} | "
                     f"{n} | {e.method} |")
        L += ["", "### 데이터 출처", "",
              "| ID | 수집 이력 |", "|----|-----------|"]
        any_src = False
        for e in self._items:
            if e.sources:
                any_src = True
                L.append(f"| {e.id} | " + " · ".join(s.short() for s in e.sources) + " |")
        if not any_src:
            L = L[:-3] + ["", "*수집 이력에 연결된 항목이 없습니다 "
                          "(합성 데이터 또는 설정 입력 기반 실행).*"]

        lim = [e for e in self._items if e.limitations]
        if lim:
            L += ["", "### 항목별 한계", ""]
            for e in lim:
                for x in e.limitations:
                    L.append(f"- **{e.id}** {e.metric}: {x}")
        L += ["", "*'미산출'은 검토하지 않았다는 뜻이 아니라 표본·데이터가 "
              "기준에 미달해 수치를 내지 않았다는 뜻입니다. 어느 쪽인지 구분할 수 "
              "있도록 빼지 않고 남깁니다.*"]
        return "\n".join(L)
