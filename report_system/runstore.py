"""실행 이력 저장소 — 판정의 '변화' 속성과 회차 간 비교를 지원한다.

제안서 5.4.5의 판단 4속성 중 '변화(직전 분석 대비)'는 이전 실행 결과가
있어야 산출된다. 본 저장소는 실행 시점의 판정과 핵심 지표를 남겨
다음 실행에서 델타를 계산한다. (예측 이력 장부(ledger)와 목적이 다르다:
장부는 전망의 봉인·대조, 여기는 운영 회차 간 상태 비교)
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id TEXT NOT NULL,
    asof TEXT NOT NULL,
    created_at TEXT NOT NULL,
    verdicts TEXT NOT NULL,   -- {name: {direction, strength, confidence}}
    metrics TEXT NOT NULL     -- {anchor_ppsm, supply_ratio, sub_mid, turnover, ...}
);
CREATE INDEX IF NOT EXISTS idx_runs_site ON runs(site_id, asof);
"""


@dataclass
class RunSnapshot:
    site_id: str
    asof: str
    verdicts: dict[str, dict[str, str]]
    metrics: dict[str, float]


class RunStore:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(_SCHEMA)

    def latest(self, site_id: str, before_asof: str) -> Optional[RunSnapshot]:
        row = self.conn.execute(
            """SELECT site_id, asof, verdicts, metrics FROM runs
               WHERE site_id=? AND asof < ? ORDER BY asof DESC, id DESC LIMIT 1""",
            (site_id, before_asof)).fetchone()
        if row is None:
            return None
        return RunSnapshot(row[0], row[1], json.loads(row[2]), json.loads(row[3]))

    def save(self, snap: RunSnapshot) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (site_id, asof, created_at, verdicts, metrics) VALUES (?,?,?,?,?)",
            (snap.site_id, snap.asof,
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             json.dumps(snap.verdicts, ensure_ascii=False),
             json.dumps(snap.metrics, ensure_ascii=False)))
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def history(self, site_id: str) -> list[RunSnapshot]:
        rows = self.conn.execute(
            "SELECT site_id, asof, verdicts, metrics FROM runs WHERE site_id=? ORDER BY asof",
            (site_id,)).fetchall()
        return [RunSnapshot(r[0], r[1], json.loads(r[2]), json.loads(r[3])) for r in rows]


# ── 변화 산출 ────────────────────────────────────────────────────────────────

_MATERIAL = 0.05   # 5% 이상이면 유의한 변화로 본다

_METRIC_LABEL = {
    "anchor_ppsm": "품질조정 앵커",
    "supply_ratio": "공급배수",
    "sub_mid": "청약 전망 중위",
    "turnover": "회전율",
}

# 판정별로 관련 있는 지표만 변화 문구에 포함한다.
# (모든 판정에 모든 델타를 붙이면 무관한 정보가 섞여 해석을 흐린다)
_VERDICT_METRICS = {
    "① 현재 가격 위치": ("anchor_ppsm",),
    "② 수요 지속성": ("sub_mid",),
    "③ 공급·환금성 위험": ("supply_ratio", "turnover"),
    "④ 촉매·실행 가능성": (),
}


def describe_change(verdict_name: str, current: dict[str, str],
                    prev: Optional[RunSnapshot],
                    cur_metrics: dict[str, float]) -> str:
    """판정별 '변화' 문구를 생성한다."""
    if prev is None:
        return "최초"

    before = prev.verdicts.get(verdict_name)
    if before is None:
        return "직전 회차에 해당 판정 없음"

    parts: list[str] = []
    if before.get("direction") != current.get("direction"):
        parts.append(f"방향 {before.get('direction')}→{current.get('direction')}")
    if before.get("strength") != current.get("strength"):
        parts.append(f"강도 {before.get('strength')}→{current.get('strength')}")
    if before.get("confidence") != current.get("confidence"):
        parts.append(f"신뢰도 {before.get('confidence')}→{current.get('confidence')}")

    for key in _VERDICT_METRICS.get(verdict_name, tuple(_METRIC_LABEL)):
        old = prev.metrics.get(key)
        new = cur_metrics.get(key)
        if old is None or new is None or old == 0:
            continue
        delta = (new - old) / abs(old)
        if abs(delta) >= _MATERIAL:
            parts.append(f"{_METRIC_LABEL[key]} {delta:+.0%}")

    if not parts:
        return f"직전({prev.asof}) 대비 변동 없음"
    return f"직전({prev.asof}) 대비 " + ", ".join(parts)
