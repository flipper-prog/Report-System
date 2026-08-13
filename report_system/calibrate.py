"""조정계수 교정 엔진 — 예시값을 실데이터 추정치로 바꾼다.

품질조정 밴드는 비교 거래를 연식·층 차이만큼 보정해 표준화한다. 그 보정률이
문헌·경험 기반 예시값이면 밴드 전체가 예시값 위에 서 있는 셈이다. 본 모듈은
실거래로 계수를 추정하고, **추정치가 백테스트에서 더 나을 때만** 채택한다.

절차
  1) 헤도닉 회귀 — ln(㎡단가) 를 연식·층·면적·거리·브랜드·분양권·시간에 회귀.
     연식과 시간 추세를 동시에 넣어 '구축 할인'과 '시장 등락'을 분리한다.
  2) 게이트 — 계수마다 표본 수·유의성(|t|≥2)·부호·크기 상한을 검사한다.
     하나라도 통과하지 못하면 그 계수는 **기존 예시값을 유지**하고 사유를 남긴다.
  3) 홀드아웃 검증 — 앞 70% 기간으로만 계수를 재추정하고, 뒤 30% 기간의
     거래에 적용해 **조정 후 잔차 분산**을 잰다. 조정이 옳으면 연식·층이 다른
     거래들이 서로 가까워지므로 분산이 줄어든다. 줄지 않으면 기각한다.

     적중률(coverage)을 채택 기준으로 쓰지 않는 이유: 적중률은 밴드 '폭'이
     제대로 보정됐는지를 재는 지표다. 계수를 바꾸면 밴드와 실현값이 같은
     방향으로 함께 움직이므로 적중률은 거의 변하지 않는다. 실제로 참값을
     복원한 합성 데이터에서도 적중률은 52%→53%로만 움직였다. 따라서 적중률은
     '악화되지 않았는지' 확인하는 안전장치로만 쓰고, 채택 여부는 분산으로
     판단한다.

회귀는 stdlib 만으로 정규방정식을 Gauss-Jordan 소거로 푼다. 표본이 수백~수천
건 규모이고 설명변수가 10개 미만이므로 수치적으로 안정적이다.
"""
from __future__ import annotations

import json
import math
import pathlib
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .backtest import backtest_price_bands, quarterly_cutoffs
from .models import Comparable, Site, Transaction
from .pricing import DEFAULT_COEF, Coefficients, adjusted_ppsm
from .transactions import clean

#: 회귀에 필요한 최소 관측 수 — 이보다 적으면 교정을 시도하지 않는다
MIN_OBS = 100

#: 계수 채택 기준 t값 (양측 5% 근사)
MIN_T = 2.0

#: 경제적으로 납득 가능한 계수 범위 (연간 %, 층 조정 %)
AGE_RANGE = (0.000, 0.050)
FLOOR_LOW_RANGE = (0.000, 0.150)
FLOOR_HIGH_RANGE = (-0.150, 0.000)

#: 층 구간을 나누는 단지 내 백분위
LOW_PCTL, HIGH_PCTL = 0.20, 0.80

#: 홀드아웃 학습 구간 비율
TRAIN_SHARE = 0.70

#: 채택에 필요한 최소 분산 개선률 (1% 미만은 잡음으로 본다)
MIN_IMPROVE = 0.01

#: 적중률이 이보다 크게 나빠지면 분산이 좋아져도 채택하지 않는다
MAX_COVERAGE_LOSS = 0.05

#: 연식 조정 상한을 정하는 기준 연수.
#: 상한을 고정값으로 두면 추정된 연간 조정률이 클수록 오래된 비교단지들이
#: 모두 상한에 걸려 서로 구분되지 않는다(조정이 무의미해진다). 따라서 상한은
#: '이 연수까지는 외삽하지 않고 그대로 적용한다'는 뜻으로 계수에 비례시킨다.
CAP_AGE_YEARS = 30

#: 그래도 넘지 않는 절대 상한(로그) — exp(0.8) ≈ 2.2배
MAX_LOG_CAP = 0.80

#: 시점수정 연간 변화율의 납득 범위 (±30%/년을 넘으면 추정 오류로 본다)
TIME_RANGE = (-0.30, 0.30)

#: 시점수정 상한을 정하는 기준 연수
CAP_TIME_YEARS = 3


class CalibrationError(RuntimeError):
    pass


# ── 선형대수 (stdlib) ────────────────────────────────────────────────────────

def _invert(m: list[list[float]]) -> list[list[float]]:
    """Gauss-Jordan 역행렬. 특이행렬이면 예외."""
    n = len(m)
    a = [row[:] + [1.0 if i == j else 0.0 for j in range(n)]
         for i, row in enumerate(m)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise CalibrationError(
                f"설계행렬이 특이(singular)합니다 — 열 {col} 이 다른 열과 선형종속. "
                "비교단지가 1개뿐이거나 층·연식 변동이 없는 경우입니다.")
        a[col], a[pivot] = a[pivot], a[col]
        p = a[col][col]
        a[col] = [v / p for v in a[col]]
        for r in range(n):
            if r == col:
                continue
            f = a[r][col]
            if f:
                a[r] = [v - f * w for v, w in zip(a[r], a[col])]
    return [row[n:] for row in a]


@dataclass
class OLSFit:
    names: list[str]
    beta: list[float]
    stderr: list[float]
    n: int
    r2: float

    def get(self, name: str) -> tuple[float, float, float]:
        """(계수, 표준오차, t값)."""
        i = self.names.index(name)
        se = self.stderr[i]
        t = self.beta[i] / se if se > 0 else 0.0
        return self.beta[i], se, t


def ols(X: list[list[float]], y: list[float], names: list[str]) -> OLSFit:
    n, k = len(X), len(X[0])
    if n <= k:
        raise CalibrationError(f"관측 {n}건 ≤ 변수 {k}개 — 회귀 불가")

    xtx = [[sum(X[r][i] * X[r][j] for r in range(n)) for j in range(k)]
           for i in range(k)]
    xty = [sum(X[r][i] * y[r] for r in range(n)) for i in range(k)]
    inv = _invert(xtx)
    beta = [sum(inv[i][j] * xty[j] for j in range(k)) for i in range(k)]

    resid = [y[r] - sum(X[r][i] * beta[i] for i in range(k)) for r in range(n)]
    rss = sum(e * e for e in resid)
    ybar = sum(y) / n
    tss = sum((v - ybar) ** 2 for v in y)
    sigma2 = rss / (n - k)
    stderr = [math.sqrt(max(0.0, sigma2 * inv[i][i])) for i in range(k)]
    r2 = 1 - rss / tss if tss > 0 else 0.0
    return OLSFit(names, beta, stderr, n, r2)


# ── 설계행렬 ─────────────────────────────────────────────────────────────────

def _percentile(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[i]


def _floor_cutoffs(txs: list[Transaction]) -> dict[str, tuple[float, float]]:
    """단지별 층 분포의 20/80 백분위 — 저층·상층 판정 기준."""
    by_c: dict[str, list[float]] = {}
    for t in txs:
        by_c.setdefault(t.complex_id, []).append(float(t.floor))
    return {c: (_percentile(v, LOW_PCTL), _percentile(v, HIGH_PCTL))
            for c, v in by_c.items()}


COLUMNS = ["const", "age", "floor_low", "floor_high", "ln_area",
           "dist_km", "brand", "presale", "t_year"]


def build_design(txs: list[Transaction], comps: dict[str, Comparable]
                 ) -> tuple[list[list[float]], list[float], list[str]]:
    """정제 거래 → (X, y, 열이름). 비교단지 메타가 없는 거래는 제외."""
    usable = [t for t in txs
              if t.complex_id in comps and t.area_m2 > 0 and t.price > 0
              and comps[t.complex_id].built_year > 0]
    if not usable:
        raise CalibrationError("비교단지 메타(건축년도)를 가진 거래가 없습니다")

    cuts = _floor_cutoffs(usable)
    t0 = min(t.trade_date for t in usable)

    X: list[list[float]] = []
    y: list[float] = []
    for t in usable:
        c = comps[t.complex_id]
        lo, hi = cuts[t.complex_id]
        age = max(0.0, t.trade_date.year - c.built_year)
        X.append([
            1.0,
            age,
            1.0 if t.floor <= lo else 0.0,
            1.0 if t.floor >= hi else 0.0,
            math.log(t.area_m2),
            c.dist_m / 1000.0,
            float(c.brand_tier),
            1.0 if c.is_presale_right else 0.0,
            (t.trade_date - t0).days / 365.25,
        ])
        y.append(math.log(t.price / t.area_m2))
    return X, y, list(COLUMNS)


def _drop_constant_columns(X: list[list[float]], names: list[str]
                           ) -> tuple[list[list[float]], list[str], list[str]]:
    """변동이 없는 열은 상수항과 선형종속이므로 제거한다 (const 자체는 유지)."""
    keep, dropped = [], []
    for j, nm in enumerate(names):
        col = [row[j] for row in X]
        if nm != "const" and max(col) - min(col) < 1e-12:
            dropped.append(nm)
        else:
            keep.append(j)
    Xk = [[row[j] for j in keep] for row in X]
    return Xk, [names[j] for j in keep], dropped


# ── 게이트 ───────────────────────────────────────────────────────────────────

@dataclass
class CoefCheck:
    name: str
    label: str
    default: float
    estimate: Optional[float]
    t: Optional[float]
    accepted: bool
    reason: str


def _gate(fit: OLSFit, col: str, label: str, default: float,
          transform, rng: tuple[float, float],
          dropped: list[str]) -> CoefCheck:
    if col in dropped:
        return CoefCheck(col, label, default, None, None, False,
                         "설명변수 변동 없음 — 추정 불가")
    if col not in fit.names:
        return CoefCheck(col, label, default, None, None, False,
                         "설계행렬에서 제외됨")
    beta, _se, t = fit.get(col)
    est = transform(beta)
    if abs(t) < MIN_T:
        return CoefCheck(col, label, default, est, t, False,
                         f"t 절댓값 {abs(t):.1f} < {MIN_T} — 통계적으로 구분되지 않음")
    if not (rng[0] <= est <= rng[1]):
        return CoefCheck(col, label, default, est, t, False,
                         f"추정 {est:+.3f} 가 납득 범위 [{rng[0]:+.3f}, {rng[1]:+.3f}] 밖 — 기각")
    return CoefCheck(col, label, default, est, t, True,
                     f"t 절댓값 {abs(t):.1f}, 범위 내 — 채택 후보")


def _fit_and_gate(txs: list[Transaction], comps: dict[str, Comparable],
                  tag: str) -> tuple[Optional[OLSFit], list[CoefCheck], Coefficients]:
    """설계행렬 구성 → 회귀 → 게이트 → 후보 계수. 실패 시 (None, [], 기본값)."""
    try:
        X, y, names = build_design(txs, comps)
        X, names, dropped = _drop_constant_columns(X, names)
        fit = ols(X, y, names)
    except CalibrationError:
        return None, [], DEFAULT_COEF

    checks = [
        _gate(fit, "age", "연식 1년당", DEFAULT_COEF.age_per_year,
              lambda b: -b, AGE_RANGE, dropped),
        _gate(fit, "floor_low", "저층 조정", DEFAULT_COEF.floor_low,
              lambda b: -b, FLOOR_LOW_RANGE, dropped),
        _gate(fit, "floor_high", "상층 조정", DEFAULT_COEF.floor_high,
              lambda b: -b, FLOOR_HIGH_RANGE, dropped),
        # 시점수정은 '과거 거래를 기준일로 끌어오는' 방향이라 부호가 그대로다.
        _gate(fit, "t_year", "시점수정 연 변화율", DEFAULT_COEF.time_per_year,
              lambda b: b, TIME_RANGE, dropped),
    ]
    by = {c.name: c for c in checks}
    age_rate = (by["age"].estimate if by["age"].accepted
                else DEFAULT_COEF.age_per_year)
    time_rate = (by["t_year"].estimate if by["t_year"].accepted
                 else DEFAULT_COEF.time_per_year)
    cand = Coefficients(
        age_per_year=age_rate,
        age_cap=(min(MAX_LOG_CAP, age_rate * CAP_AGE_YEARS)
                 if by["age"].accepted else DEFAULT_COEF.age_cap),
        time_per_year=time_rate,
        time_cap=(min(MAX_LOG_CAP, abs(time_rate) * CAP_TIME_YEARS)
                  if by["t_year"].accepted else DEFAULT_COEF.time_cap),
        floor_low=(by["floor_low"].estimate if by["floor_low"].accepted
                   else DEFAULT_COEF.floor_low),
        floor_high=(by["floor_high"].estimate if by["floor_high"].accepted
                    else DEFAULT_COEF.floor_high),
        source=tag)
    return fit, checks, cand


def _log_mad(vals: list[float]) -> Optional[float]:
    """로그 중위편차 — 조정 후 값들이 서로 얼마나 흩어져 있는지."""
    if len(vals) < 4:
        return None
    logs = sorted(math.log(v) for v in vals if v > 0)
    if len(logs) < 4:
        return None
    med = logs[len(logs) // 2]
    devs = sorted(abs(v - med) for v in logs)
    return devs[len(devs) // 2]


def holdout_dispersion(site: Site, comps: dict[str, Comparable],
                       test_txs: list[Transaction],
                       coef: Coefficients) -> Optional[float]:
    """검증 구간 거래를 coef 로 조정한 뒤의 잔차 분산(로그 MAD).

    조정이 옳을수록 연식·층이 다른 거래들이 한 점으로 모이므로 값이 작아진다.
    타입별로 재고 표본 가중 평균한다.
    """
    parts: list[tuple[float, int]] = []
    for t in site.types:
        adj = [adjusted_ppsm(tx, comps[tx.complex_id], max(x.trade_date for x in test_txs),
                             t.floors, coef)
               for tx in test_txs
               if tx.complex_id in comps
               and t.area_m2 * 0.8 <= tx.area_m2 <= t.area_m2 * 1.2]
        m = _log_mad(adj)
        if m is not None:
            parts.append((m, len(adj)))
    if not parts:
        return None
    total = sum(n for _, n in parts)
    return sum(m * n for m, n in parts) / total


@dataclass
class CalibrationResult:
    fit: Optional[OLSFit]
    checks: list[CoefCheck] = field(default_factory=list)
    candidate: Coefficients = DEFAULT_COEF
    adopted: Coefficients = DEFAULT_COEF
    coverage_before: Optional[float] = None
    coverage_after: Optional[float] = None
    nominal: float = 0.50
    dispersion_before: Optional[float] = None
    dispersion_after: Optional[float] = None
    n_train: int = 0
    n_test: int = 0
    decision: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def improvement(self) -> Optional[float]:
        """분산 개선률 (양수면 조정이 나아짐)."""
        if not self.dispersion_before or self.dispersion_after is None:
            return None
        return (self.dispersion_before - self.dispersion_after) / self.dispersion_before

    @property
    def changed(self) -> bool:
        return self.adopted.as_dict() != DEFAULT_COEF.as_dict()

    def as_markdown(self) -> str:
        L = ["# 조정계수 교정 보고서", "",
             f"판정: **{self.decision}**", ""]
        if self.fit is not None:
            L += [f"헤도닉 회귀 — 관측 {self.fit.n:,}건 · R² {self.fit.r2:.3f}", "",
                  "| 변수 | 계수 | 표준오차 | t |",
                  "|------|------|----------|---|"]
            for nm, b, se in zip(self.fit.names, self.fit.beta, self.fit.stderr):
                t = b / se if se > 0 else 0.0
                L.append(f"| {nm} | {b:+.5f} | {se:.5f} | {t:+.1f} |")
            L.append("")

        L += ["## 계수별 게이트", "",
              "| 계수 | 기존(예시값) | 추정값 | t | 채택 | 사유 |",
              "|------|--------------|--------|---|------|------|"]
        for c in self.checks:
            est = f"{c.estimate:+.4f}" if c.estimate is not None else "—"
            t = f"{c.t:+.1f}" if c.t is not None else "—"
            L.append(f"| {c.label} | {c.default:+.4f} | {est} | {t} | "
                     f"{'예' if c.accepted else '아니오'} | {c.reason} |")
        L.append("")

        L += [f"## 홀드아웃 검증 (학습 {self.n_train:,}건 → 검증 {self.n_test:,}건)", "",
              "*앞 70% 기간으로만 계수를 재추정해 뒤 30% 기간에 적용했습니다. "
              "조정이 옳을수록 연식·층이 다른 거래가 서로 가까워져 잔차 분산이 "
              "줄어듭니다.*", "",
              "| 계수 | 검증 구간 잔차 분산 (로그 MAD) |",
              "|------|-------------------------------|"]
        for tag, dv in (("기존(예시값)", self.dispersion_before),
                        ("후보(교정값)", self.dispersion_after)):
            L.append(f"| {tag} | {dv:.5f} |" if dv is not None
                     else f"| {tag} | 산출 불가 |")
        if self.improvement is not None:
            L.append(f"| **개선률** | **{self.improvement:+.1%}** "
                     f"(채택 기준 {MIN_IMPROVE:.0%}) |")
        L += ["", "### 안전장치 — 적중률 (채택 기준 아님)", "",
              "*적중률은 밴드 '폭'의 보정 상태를 재는 지표라 계수 변경에 둔감합니다. "
              "따라서 채택 기준이 아니라, 크게 나빠지지 않았는지 확인하는 용도로만 "
              f"봅니다 (허용 {MAX_COVERAGE_LOSS:.0%}p).*", "",
              f"| 계수 | 적중률 (명목 {self.nominal:.0%}) | 명목과의 거리 |",
              "|------|--------|----------------|"]
        for tag, cov in (("기존", self.coverage_before), ("후보", self.coverage_after)):
            if cov is None:
                L.append(f"| {tag} | 산출 불가 | — |")
            else:
                L.append(f"| {tag} | {cov:.0%} | {abs(cov - self.nominal):.1%}p |")
        L += ["", "## 채택된 계수", "",
              "| 항목 | 값 |", "|------|-----|"]
        for k, v in self.adopted.as_dict().items():
            L.append(f"| {k} | {v:.5f} |" if isinstance(v, float)
                     else f"| {k} | {v} |")
        if self.notes:
            L += ["", "## 참고"] + [f"- {n}" for n in self.notes]
        L += ["", "*계수는 홀드아웃 검증 구간에서 잔차 분산이 실제로 줄어들 때만 "
              "교체됩니다. 교체되지 않은 계수는 예시값 그대로이며 리포트의 모델 "
              "카드에 그렇게 표기됩니다.*"]
        return "\n".join(L)


def calibrate(site: Site, comps: dict[str, Comparable],
              txs: list[Transaction], asof: date,
              min_obs: int = MIN_OBS) -> CalibrationResult:
    """계수를 추정하고 백테스트로 검증한 뒤 채택 여부를 결정한다."""
    kept = clean(txs).kept
    res = CalibrationResult(fit=None)

    if len(kept) < min_obs:
        res.decision = f"교정 미실시 — 정제 거래 {len(kept)}건 < 최소 {min_obs}건"
        res.notes.append("표본이 쌓이면 재실행하십시오. 그때까지 계수는 예시값입니다.")
        return res

    # 1) 전체 표본 적합 — 최종 보고용 계수
    res.fit, res.checks, res.candidate = _fit_and_gate(
        kept, comps, f"실데이터 교정 ({asof} 기준, 관측 {len(kept):,}건)")
    if res.fit is None:
        res.decision = "교정 불가 — 회귀를 구성할 수 없음(비교단지·변동 부족)"
        return res
    if not any(c.accepted for c in res.checks):
        res.decision = "교정 없음 — 게이트를 통과한 계수가 없음"
        res.adopted = DEFAULT_COEF
        return res

    # 2) 홀드아웃 — 앞 70% 로만 재추정해 뒤 30% 에서 조정 품질을 잰다.
    #    전체 표본으로 추정한 계수를 같은 표본에서 검증하면 당연히 좋아 보인다.
    ordered = sorted(kept, key=lambda t: t.trade_date)
    cut = int(len(ordered) * TRAIN_SHARE)
    train, test = ordered[:cut], ordered[cut:]
    res.n_train, res.n_test = len(train), len(test)
    if len(test) < MIN_OBS // 2:
        res.decision = f"교정 보류 — 검증 구간 {len(test)}건으로 부족"
        res.adopted = DEFAULT_COEF
        res.notes.append("검증 없이 계수를 바꾸지 않습니다.")
        return res

    _, _, train_cand = _fit_and_gate(train, comps, "홀드아웃 학습")
    res.dispersion_before = holdout_dispersion(site, comps, test, DEFAULT_COEF)
    res.dispersion_after = holdout_dispersion(site, comps, test, train_cand)
    imp = res.improvement

    # 3) 안전장치 — 적중률이 크게 나빠지지 않았는지 함께 본다(채택 기준은 아님).
    cuts = quarterly_cutoffs(min(t.trade_date for t in kept), asof)
    if cuts:
        before = backtest_price_bands(site, comps, kept, cuts, coef=DEFAULT_COEF)
        after = backtest_price_bands(site, comps, kept, cuts, coef=res.candidate)
        res.nominal = before.nominal
        res.coverage_before, res.coverage_after = before.coverage, after.coverage

    if imp is None:
        res.decision = "교정 보류 — 검증 구간 분산을 산출할 수 없음"
        res.adopted = DEFAULT_COEF
        return res

    cov_loss = 0.0
    if res.coverage_before is not None and res.coverage_after is not None:
        cov_loss = (abs(res.coverage_after - res.nominal)
                    - abs(res.coverage_before - res.nominal))

    if imp < MIN_IMPROVE:
        res.adopted = DEFAULT_COEF
        res.decision = (f"교정 기각 — 검증 구간 잔차 분산 {imp:+.1%} "
                        f"(최소 {MIN_IMPROVE:.0%} 개선 필요). 예시값 유지")
    elif cov_loss > MAX_COVERAGE_LOSS:
        res.adopted = DEFAULT_COEF
        res.decision = (f"교정 기각 — 분산은 {imp:+.1%} 개선했으나 적중률이 명목에서 "
                        f"{cov_loss:.1%}p 멀어짐. 예시값 유지")
    else:
        res.adopted = res.candidate
        res.decision = (f"교정 채택 — 검증 구간 잔차 분산 {imp:+.1%} 개선 "
                        f"(학습 {res.n_train:,}건 / 검증 {res.n_test:,}건)")
    return res


# ── 저장·적재 ────────────────────────────────────────────────────────────────

def save(coef: Coefficients, path: str = "out/coefficients.json") -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(coef.as_dict(), ensure_ascii=False, indent=2),
                 encoding="utf-8")


def load(path: str = "out/coefficients.json") -> Coefficients:
    """교정 결과를 불러온다. 파일이 없으면 예시값을 그대로 쓴다."""
    p = pathlib.Path(path)
    if not p.exists():
        return DEFAULT_COEF
    return Coefficients.from_dict(json.loads(p.read_text(encoding="utf-8")))
