"""소득·구매력 커넥터 (L5) — 시군구 단위 실측 소득으로 분포를 고정한다.

실부담 시뮬레이션(구매 가능 가구 비율)은 소득 분포 위에 서 있다. 지금까지 그
분포의 중심값은 설정에 적어 넣은 추정치였고, 그러면 "구매 가능 가구 21%" 같은
수치는 사실상 입력한 사람이 정한 값이다. 본 커넥터는 그 중심값을 **실측 통계로
바꾼다**.

분포 자체는 여전히 로그정규 근사다. 공개 통계가 제공하는 것은 평균·중위 같은
대표값이지 가구별 분포가 아니기 때문이다. 다만 중심값이 실측이면 근사의 성격이
달라진다 — '전부 가정'에서 '형태만 가정'으로 좁혀진다. 산포(sigma)는 여전히
가정이며 그 사실을 리포트에 병기한다.

데이터 경로
  1) KOSIS 통계 API — `fetch_kosis()` (국세청 시군구별 근로소득 신고 현황 등)
  2) 파일 적재 (CSV/JSON) — `load()`

파일 스키마 (헤더 필수: period, median_income)
  period         YYYY            기준 연도
  median_income  정수(원)        중위 소득
  mean_income    정수(원) 선택   평균 소득
  n_filers       정수 선택       신고 인원 (표본 규모 표기용)
"""
from __future__ import annotations

import csv
import json
import math
import pathlib
import random
from dataclasses import dataclass, field
from typing import Optional

from .base import Fetcher
from .migration import KOSIS_URL, KosisApiError

REQUIRED = ("period", "median_income")
SOURCE = "소득·구매력 (L5)"

#: 소득 분포의 산포 기본값(로그 표준편차). 실측 분포가 없을 때의 가정이며,
#: 국내 근로소득 분포의 통상 범위를 근거로 한 값이다.
DEFAULT_SIGMA = 0.45

#: 상식 범위 — 연 소득 기준. 벗어나면 단위 오류(만원/천원)로 본다.
MIN_INCOME = 5_000_000
MAX_INCOME = 500_000_000

#: 평균/중위 비율이 이 범위를 벗어나면 둘 중 하나가 다른 정의일 가능성이 크다
MEAN_MEDIAN_RANGE = (1.0, 2.5)


class IncomeFormatError(RuntimeError):
    pass


@dataclass
class IncomeStats:
    period: str
    median_income: int
    mean_income: Optional[int] = None
    n_filers: Optional[int] = None
    sigma: float = DEFAULT_SIGMA
    source: str = ""
    limitations: list[str] = field(default_factory=list)

    @property
    def skew_ratio(self) -> Optional[float]:
        if not self.mean_income or self.median_income <= 0:
            return None
        return self.mean_income / self.median_income

    def sample(self, n: int = 500, seed: int = 7) -> list[float]:
        """중위 소득을 중심으로 한 로그정규 표본.

        로그정규는 중앙값이 exp(mu) 이므로 mu = ln(중위소득) 으로 두면 표본의
        중앙값이 실측 중위값과 일치한다. 산포만 가정이 남는다.
        """
        rng = random.Random(seed)
        mu = math.log(self.median_income)
        floor = self.median_income * 0.3
        return [max(floor, rng.lognormvariate(mu, self.sigma)) for _ in range(n)]

    def summary(self) -> str:
        parts = [f"{self.period}년 중위 {self.median_income/1e4:,.0f}만원"]
        if self.mean_income:
            parts.append(f"평균 {self.mean_income/1e4:,.0f}만원")
        if self.skew_ratio:
            parts.append(f"평균/중위 {self.skew_ratio:.2f}")
        if self.n_filers:
            parts.append(f"신고 {self.n_filers:,}명")
        parts.append(f"산포 가정 σ={self.sigma:.2f}")
        return " · ".join(parts)


def _validate(st: IncomeStats) -> IncomeStats:
    if not (MIN_INCOME <= st.median_income <= MAX_INCOME):
        raise IncomeFormatError(
            f"중위 소득 {st.median_income:,} 이 상식 범위를 벗어남 — "
            "원 단위인지 확인 필요(만원·천원 단위 입력 의심)")
    r = st.skew_ratio
    if r is not None and not (MEAN_MEDIAN_RANGE[0] <= r <= MEAN_MEDIAN_RANGE[1]):
        st.limitations.append(
            f"평균/중위 비율 {r:.2f} 이 통상 범위를 벗어남 — 두 값의 정의가 다를 수 있음")
    st.limitations.append(
        "시군구 단위 대표값 — 현장 생활권 소득과 다를 수 있음 [LIMITATION]")
    st.limitations.append(
        f"분포 형태는 로그정규 근사이며 산포(σ={st.sigma:.2f})는 가정 [LIMITATION]")
    return st


def load(path: str, sigma: float = DEFAULT_SIGMA) -> IncomeStats:
    """소득 통계 파일을 적재한다. 여러 연도가 있으면 가장 최근 연도를 쓴다."""
    p = pathlib.Path(path)
    if not p.exists():
        raise IncomeFormatError(f"소득 파일 없음: {path}")

    if p.suffix.lower() == ".json":
        doc = json.loads(p.read_text(encoding="utf-8"))
        rows = doc if isinstance(doc, list) else list(doc.get("data", []))
    else:
        with p.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
            if missing:
                raise IncomeFormatError(
                    f"필수 열 누락: {', '.join(missing)} (필요: {', '.join(REQUIRED)})")
            rows = list(reader)
    if not rows:
        raise IncomeFormatError("소득 자료가 비어 있습니다")

    def _int(v) -> Optional[int]:
        s = str(v or "").replace(",", "").strip()
        return int(float(s)) if s else None

    best = None
    for row in rows:
        try:
            period = str(row["period"]).strip()[:4]
            median = _int(row["median_income"])
            if median is None:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        if best is None or period > best[0]:
            best = (period, median, _int(row.get("mean_income")),
                    _int(row.get("n_filers")))
    if best is None:
        raise IncomeFormatError("해석 가능한 소득 행이 없습니다")

    period, median, mean, n = best
    return _validate(IncomeStats(period, median, mean, n, sigma,
                                 source=f"소득 파일 ({p.name})"))


def fetch_kosis(fetcher: Fetcher, key: str, spec: dict,
                sigma: float = DEFAULT_SIGMA) -> IncomeStats:
    """KOSIS 소득 통계를 수집한다.

    spec 에 표를 특정하는 KOSIS 파라미터와 항목 코드
    `items = {"median": "T1", "mean": "T2", "filers": "T3"}` 를 넣는다.
    """
    items = spec.get("items") or {}
    if not items.get("median"):
        raise KosisApiError("spec['items']['median'] (KOSIS 항목 코드) 필요")

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

    code_to_key = {str(v): k for k, v in items.items()}
    latest: dict[str, dict[str, int]] = {}
    for row in doc:
        period = str(row.get("PRD_DE") or "")[:4]
        which = code_to_key.get(str(row.get("ITM_ID") or ""))
        if not period or not which:
            continue
        try:
            latest.setdefault(period, {})[which] = int(
                float(str(row.get("DT") or 0).replace(",", "")))
        except ValueError:
            continue
    if not latest:
        raise KosisApiError(f"{SOURCE}: 매칭되는 항목 코드가 응답에 없음")

    period = max(latest)
    vals = latest[period]
    if "median" not in vals:
        raise KosisApiError(f"{SOURCE}: {period}년 중위 소득 항목이 응답에 없음")
    return _validate(IncomeStats(period, vals["median"], vals.get("mean"),
                                 vals.get("filers"), sigma,
                                 source="KOSIS 소득 통계 API"))
