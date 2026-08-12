"""예측 이력 장부 (P0-3, 부록 E.3) + 구간 적중률 관리 (P0-2, 부록 E.2).

규칙
- 모든 FORECAST 산출은 발행 시점에 봉인(payload 해시)되어 저장되며,
  수정·삭제 API를 제공하지 않는다 (INSERT와 실적 대조만 가능).
- 실적 확정 시 구간 포함 여부(hit)를 자동 판정한다.
- coverage(): 명목 신뢰수준 대비 실제 적중률을 산출한다.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,             -- 'subscription' | 'price' ...
    target TEXT NOT NULL,           -- 현장/타입 식별
    lo REAL NOT NULL,
    hi REAL NOT NULL,
    confidence REAL NOT NULL,
    model_version TEXT NOT NULL,
    data_asof TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (
    forecast_id TEXT PRIMARY KEY REFERENCES forecasts(id),
    actual REAL NOT NULL,
    hit INTEGER NOT NULL,
    resolved_at TEXT NOT NULL
);
-- 봉인 원칙의 DB 수준 강제: 갱신·삭제 차단 트리거
CREATE TRIGGER IF NOT EXISTS forecasts_no_update
BEFORE UPDATE ON forecasts
BEGIN SELECT RAISE(ABORT, 'sealed: forecasts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS forecasts_no_delete
BEFORE DELETE ON forecasts
BEGIN SELECT RAISE(ABORT, 'sealed: forecasts are immutable'); END;
"""


@dataclass
class CoverageReport:
    kind: str
    nominal_confidence: float
    resolved: int
    hits: int

    @property
    def achieved(self) -> Optional[float]:
        return self.hits / self.resolved if self.resolved else None

    def verdict(self) -> str:
        if self.resolved < 10:
            return f"실적 확정 {self.resolved}건 — 표본 누적 중(판정 유보)"
        a = self.achieved or 0.0
        gap = a - self.nominal_confidence
        if gap < -0.10:
            return f"적중률 {a:.0%} < 명목 {self.nominal_confidence:.0%} — 구간 재보정 필요 (E.2 조치)"
        return f"적중률 {a:.0%} (명목 {self.nominal_confidence:.0%}) — 정합"


class ForecastLedger:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(_SCHEMA)

    # ── 봉인 ────────────────────────────────────────────────────────────
    def seal(self, kind: str, target: str, lo: float, hi: float,
             confidence: float, model_version: str, data_asof: str,
             payload: dict) -> str:
        issued = datetime.now(timezone.utc).isoformat(timespec="seconds")
        body = json.dumps(
            {"kind": kind, "target": target, "lo": lo, "hi": hi,
             "confidence": confidence, "model": model_version,
             "data_asof": data_asof, "issued_at": issued, "payload": payload},
            ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(body.encode()).hexdigest()
        fid = f"{kind}:{target}:{digest[:12]}"
        self.conn.execute(
            "INSERT INTO forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fid, kind, target, lo, hi, confidence, model_version,
             data_asof, issued, body, digest))
        self.conn.commit()
        return fid

    # ── 실적 대조 ────────────────────────────────────────────────────────
    def resolve(self, forecast_id: str, actual: float) -> bool:
        row = self.conn.execute(
            "SELECT lo, hi FROM forecasts WHERE id=?", (forecast_id,)).fetchone()
        if row is None:
            raise KeyError(forecast_id)
        lo, hi = row
        hit = int(lo <= actual <= hi)
        self.conn.execute(
            "INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?)",
            (forecast_id, actual, hit,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))
        self.conn.commit()
        return bool(hit)

    # ── 적중률 (P0-2) ───────────────────────────────────────────────────
    def coverage(self, kind: str, nominal_confidence: float) -> CoverageReport:
        row = self.conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(o.hit),0)
               FROM outcomes o JOIN forecasts f ON f.id=o.forecast_id
               WHERE f.kind=?""", (kind,)).fetchone()
        resolved, hits = row
        return CoverageReport(kind, nominal_confidence, resolved, hits)

    def history(self, kind: Optional[str] = None) -> list[tuple]:
        q = ("SELECT f.id, f.target, f.lo, f.hi, f.issued_at, o.actual, o.hit "
             "FROM forecasts f LEFT JOIN outcomes o ON o.forecast_id=f.id")
        args: tuple = ()
        if kind:
            q += " WHERE f.kind=?"
            args = (kind,)
        return self.conn.execute(q + " ORDER BY f.issued_at", args).fetchall()

    def verify_seal(self, forecast_id: str) -> bool:
        row = self.conn.execute(
            "SELECT payload, payload_hash FROM forecasts WHERE id=?",
            (forecast_id,)).fetchone()
        if row is None:
            raise KeyError(forecast_id)
        payload, digest = row
        return hashlib.sha256(payload.encode()).hexdigest() == digest
