"""판정 강건성 검사 — "이 결론은 가정이 바뀌면 뒤집히나?"

리포트의 4개 판정은 데이터만으로 나오지 않는다. 연식 조정계수, LTV·DSR 한도,
단계별 공급 실현률, 촉매 실현성처럼 **선택된 가정**이 함께 들어간다. 가정은
리포트에 LIMITATION으로 적혀 있지만, 적혀 있다는 것과 그 가정이 결론을
좌우한다는 것을 독자가 아는 것은 다르다.

이 모듈은 각 가정을 합리적 범위 안에서 흔든 뒤 **같은 함수로 판정을 다시
계산**한다. 방향이 바뀌면 그 판정은 '가정 의존'이고, 바뀌지 않으면 '데이터
지지'다. 둘을 구분해 표기하는 것이 목적이며, 어느 쪽이 유리한지는 보지 않는다.

설계 원칙
  - 판정 함수를 복제하지 않는다. 운영과 동일한 `verdicts.*` 를 호출한다.
    (복제하면 검사 대상과 검사 도구가 갈라져 검사가 무의미해진다)
  - 흔들 수 없는 가정은 '검사 안 함'으로 남긴다. 조용히 빼지 않는다.
  - 흔드는 폭은 상수로 노출한다. 폭 자체가 또 하나의 가정이기 때문이다.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

from .acquisition import HEAVY_RATE, RATE_LOW
from .affordability import EQUITY_INCOME_MULTIPLE, simulate
from .catalyst import STAGE_FEASIBILITY, assess
from .liquidity import analyze as analyze_liquidity
from .models import SupplyStage
from .pricing import Coefficients, market_positions, quality_adjusted_bands
from .supply import STAGE_REALIZATION, probability_adjusted
from .verdicts import (Verdict, catalyst_verdict, demand_verdict, price_verdict,
                       supply_verdict)

#: 연식 조정계수를 흔드는 배수. 교정 전 초기값(1.0%/년)의 불확실성이 이 폭보다
#: 작다고 볼 근거가 없다.
AGE_FACTORS = (0.5, 1.5)
#: 시점수정 연간 변화율 가감폭(로그). ±2%/년은 국내 아파트 연간 변동의
#: 보수적 하단에 해당한다.
TIME_DELTA = 0.02
#: 계수를 흔들 때 상한도 함께 움직인다 — 교정 엔진과 같은 규칙(rate×30년, 최대 0.80).
CAP_AGE_YEARS = 30
CAP_TIME_YEARS = 3
MAX_LOG_CAP = 0.80

#: 규제 강화 시나리오. 현행 한도가 유지된다는 보장이 없다.
TIGHT_LTV = 0.4
TIGHT_DSR = 0.3
#: 가용 자기자본 배수 — 리포트가 이미 LIMITATION으로 밝힌 최대 가정.
TIGHT_EQUITY_MULTIPLE = 3.0

#: 단계별 실현률 가감폭(절대). 인허가 물량의 실제 착공 전환율은 시기별로
#: 이 폭 이상 움직인 사례가 있다.
REALIZATION_DELTA = 0.20
#: 환금성 '양호' 기준을 보수적으로 올린 값(%).
STRICT_TURNOVER = 9.0
#: 촉매 실현 가능성 가감폭(절대).
FEASIBILITY_DELTA = 0.10

#: 취득 부대비용 전제 — 기본은 1주택 유상거래다. 매수층이 다주택자로 기울면
#: 세율이 크게 달라지므로, 그 전제가 판정 ①을 좌우하는지 확인한다.
#: (양쪽에 같은 세율을 적용하므로 뒤집힌다면 세율 자체가 아니라 구간 누진의
#:  비선형성이 원인이다 — 그 사실을 아는 것이 검사의 목적이다)
HEAVY_TAX_BASE = HEAVY_RATE

V1 = "① 현재 가격 위치"
V2 = "② 수요 지속성"
V3 = "③ 공급·환금성 위험"
V4 = "④ 촉매·실행 가능성"


@dataclass(frozen=True)
class Perturbation:
    """가정 하나를 흔든 뒤의 판정 변화."""
    verdict: str
    assumption: str          # 흔든 가정의 이름
    change: str              # 무엇을 어떻게 바꿨는가
    base_direction: str
    new_direction: str
    base_strength: str
    new_strength: str

    @property
    def flipped(self) -> bool:
        return self.new_direction != self.base_direction

    @property
    def weakened(self) -> bool:
        """방향은 같으나 강도가 낮아진 경우."""
        order = {"강": 3, "중": 2, "약": 1}
        return (not self.flipped
                and order.get(self.new_strength, 0) < order.get(self.base_strength, 0))

    @property
    def token(self) -> str:
        """한 단어 결과 — 표에서 배지로 강조되는 값."""
        if self.flipped:
            return "뒤집힘"
        if self.weakened:
            return "강도 하락"
        return "방향 유지"

    @property
    def detail(self) -> str:
        if self.flipped:
            return f"{self.base_direction} → {self.new_direction}"
        if self.weakened:
            return f"{self.base_direction} 유지, 강도 {self.base_strength} → {self.new_strength}"
        return f"{self.base_direction}·{self.base_strength} 유지"

    @property
    def result_label(self) -> str:
        return f"{self.token} ({self.detail})"


@dataclass
class RobustnessReport:
    checks: list[Perturbation] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (가정, 사유)

    @property
    def fragile(self) -> list[str]:
        """가정 하나로 방향이 뒤집히는 판정 이름 (등장 순서 유지)."""
        out: list[str] = []
        for c in self.checks:
            if c.flipped and c.verdict not in out:
                out.append(c.verdict)
        return out

    @property
    def tested_verdicts(self) -> list[str]:
        out: list[str] = []
        for c in self.checks:
            if c.verdict not in out:
                out.append(c.verdict)
        return out

    @property
    def label(self) -> str:
        if not self.checks:
            return "강건성 미검사"
        if not self.fragile:
            return "가정 변화에 강건"
        return f"가정 의존 판정 {len(self.fragile)}건"

    def summary(self) -> str:
        if not self.checks:
            return "강건성 검사 미실행 — 흔들 수 있는 가정 없음"
        n_flip = sum(1 for c in self.checks if c.flipped)
        return (f"가정 {len(self.checks)}건 교란 · 방향 전환 {n_flip}건 · "
                f"{self.label}")

    def as_markdown(self) -> str:
        L: list[str] = []
        L.append(f"**{self.summary()}**")
        L.append("")
        if self.checks:
            L.append("| 판정 | 흔든 가정 | 변경 내용 | 결과 | 상세 |")
            L.append("|------|-----------|-----------|------|------|")
            for c in self.checks:
                L.append(f"| {c.verdict} | {c.assumption} | {c.change} | "
                         f"{c.token} | {c.detail} |")
            L.append("")
        if self.fragile:
            L.append("**가정 의존 판정 — 해석 시 주의**")
            L.append("")
            for name in self.fragile:
                causes = [c.assumption for c in self.checks
                          if c.flipped and c.verdict == name]
                L.append(f"- {name}: {', '.join(causes)} 변경만으로 방향이 바뀝니다. "
                         "결론보다 해당 가정의 실측 교체를 우선하십시오. [LIMITATION]")
            L.append("")
        else:
            L.append("- 검사한 가정 범위 안에서 방향이 뒤집힌 판정은 없습니다.")
            L.append("")
        if self.skipped:
            L.append("**검사하지 않은 가정**")
            L.append("")
            for name, reason in self.skipped:
                L.append(f"- {name}: {reason}")
            L.append("")
        return "\n".join(L)


def _shift_coef(coef: Coefficients, *, age_factor: float = 1.0,
                time_delta: float = 0.0) -> Coefficients:
    """계수를 흔들되 상한도 교정 엔진과 동일한 규칙으로 재계산한다.

    상한을 고정한 채 계수만 흔들면 큰 계수는 상한에 눌려 변화가 사라지고,
    '강건하다'는 잘못된 결론이 나온다.
    """
    age = coef.age_per_year * age_factor
    t = coef.time_per_year + time_delta
    return replace(
        coef,
        age_per_year=age,
        age_cap=min(MAX_LOG_CAP, age * CAP_AGE_YEARS) if age > 0 else coef.age_cap,
        time_per_year=t,
        time_cap=(min(MAX_LOG_CAP, abs(t) * CAP_TIME_YEARS) if t else coef.time_cap),
        source=coef.source + " (강건성 교란)")


def _record(out: list[Perturbation], base: Verdict, new: Verdict,
            assumption: str, change: str) -> None:
    out.append(Perturbation(
        verdict=base.name, assumption=assumption, change=change,
        base_direction=base.direction, new_direction=new.direction,
        base_strength=base.strength, new_strength=new.strength))


def analyze(
    *,
    base_verdicts: list[Verdict],
    site,
    comps: dict,
    txs: list,
    asof: date,
    profile,
    coef: Coefficients,
    jeonse=None,
    incomes: list[float] | None = None,
    sub_forecast=None,
    supply_items: list | None = None,
    catalyst_plans: list | None = None,
    liquidity=None,
    region_stats=None,
    commerce=None,
    migration=None,
    mobility=None,
    transit=None,
    unsold=None,
    housing=None,
) -> RobustnessReport:
    """각 판정을 지배하는 가정을 흔들어 방향 전환 여부를 기록한다."""
    base = {v.name: v for v in base_verdicts}
    rep = RobustnessReport()

    # ── 판정 ① 가격 위치: 품질조정 계수
    b1 = base.get(V1)
    if b1 is not None and txs and comps:
        for factor in AGE_FACTORS:
            c = _shift_coef(coef, age_factor=factor)
            bands = quality_adjusted_bands(site, comps, txs, asof,
                                           profile=profile, coef=c)
            v = price_verdict(market_positions(site, bands), jeonse=jeonse)
            _record(rep.checks, b1, v, "연식 조정계수",
                    f"{coef.age_per_year:.3f} → {c.age_per_year:.3f} /년 "
                    f"(×{factor:g})")
        for delta in (+TIME_DELTA, -TIME_DELTA):
            c = _shift_coef(coef, time_delta=delta)
            bands = quality_adjusted_bands(site, comps, txs, asof,
                                           profile=profile, coef=c)
            v = price_verdict(market_positions(site, bands), jeonse=jeonse)
            _record(rep.checks, b1, v, "시점수정률",
                    f"{coef.time_per_year:+.3f} → {c.time_per_year:+.3f} /년")
        for tax_label, tb in (("다주택 중과", HEAVY_TAX_BASE),
                              ("전 구간 최저세율", RATE_LOW)):
            bands = quality_adjusted_bands(site, comps, txs, asof,
                                           profile=profile, coef=coef,
                                           tax_base=tb)
            v = price_verdict(market_positions(site, bands, tax_base=tb),
                              jeonse=jeonse)
            _record(rep.checks, b1, v, "취득 부대비용 전제",
                    f"1주택 누진 → {tax_label}({tb:.0%} 기본세율, 양쪽 동일 적용)")
    elif b1 is not None:
        rep.skipped.append(("품질조정 계수", "비교 거래·비교단지 부족으로 밴드 재계산 불가"))

    # ── 판정 ② 수요: 금융 규제·자기자본 가정
    b2 = base.get(V2)
    if b2 is not None and incomes and sub_forecast is not None and site.types:
        def _demand(afford):
            return demand_verdict(afford, sub_forecast, region_stats=region_stats,
                                  commerce=commerce, migration=migration,
                                  mobility=mobility, transit=transit)

        tight = [simulate(t, incomes, ltv_cap=TIGHT_LTV, dsr_cap=TIGHT_DSR)
                 for t in site.types]
        _record(rep.checks, b2, _demand(tight), "LTV·DSR 한도",
                f"LTV 0.5→{TIGHT_LTV:.1f} · DSR 0.4→{TIGHT_DSR:.1f} (규제 강화)")

        lean = [simulate(t, incomes, equity_multiple=TIGHT_EQUITY_MULTIPLE)
                for t in site.types]
        _record(rep.checks, b2, _demand(lean), "가용 자기자본 배수",
                f"연소득×{EQUITY_INCOME_MULTIPLE:g} → ×{TIGHT_EQUITY_MULTIPLE:g}")
    elif b2 is not None:
        rep.skipped.append(("금융 규제·자기자본 가정",
                            "소득 표본 또는 청약 전망 부재로 수요 판정 재계산 불가"))

    # ── 판정 ③ 공급·환금성: 단계별 실현률, 환금성 기준
    b3 = base.get(V3)
    if b3 is not None and supply_items:
        for delta in (+REALIZATION_DELTA, -REALIZATION_DELTA):
            rates = {s: max(0.05, min(1.0, r + delta))
                     for s, r in STAGE_REALIZATION.items()}
            sa = probability_adjusted(supply_items, window_months=36,
                                      realization=rates)
            v = supply_verdict(sa, site.total_units, liquidity, unsold=unsold,
                               housing=housing)
            _record(rep.checks, b3, v, "단계별 공급 실현률",
                    f"전 단계 {delta:+.0%}p (인허가 "
                    f"{STAGE_REALIZATION[SupplyStage.PERMIT]:.0%} → "
                    f"{rates[SupplyStage.PERMIT]:.0%})")
    elif b3 is not None:
        rep.skipped.append(("단계별 공급 실현률", "공급 목록 미입력"))

    if b3 is not None and txs and comps:
        sa = probability_adjusted(supply_items or [], window_months=36)
        strict = analyze_liquidity(txs, comps, asof,
                                   good_threshold=STRICT_TURNOVER)
        v = supply_verdict(sa, site.total_units, strict, unsold=unsold,
                           housing=housing)
        base_thr = getattr(liquidity, "good_threshold", 6.0)
        _record(rep.checks, b3, v, "환금성 양호 기준",
                f"연 회전율 {base_thr:.0f}% → {STRICT_TURNOVER:.0f}%")
    elif b3 is not None:
        rep.skipped.append(("환금성 양호 기준", "거래 표본 부족으로 회전율 재계산 불가"))

    # ── 판정 ④ 촉매: 단계별 실현 가능성
    b4 = base.get(V4)
    if b4 is not None and catalyst_plans:
        for delta in (+FEASIBILITY_DELTA, -FEASIBILITY_DELTA):
            table = {s: max(0.05, min(1.0, f + delta))
                     for s, f in STAGE_FEASIBILITY.items()}
            cards = [assess(p, feasibility=table) for p in catalyst_plans]
            _record(rep.checks, b4, catalyst_verdict(cards), "촉매 실현 가능성",
                    f"단계별 실현성 {delta:+.0%}p")
    elif b4 is not None:
        rep.skipped.append(("촉매 실현 가능성", "평가 대상 개발계획 없음"))

    return rep
