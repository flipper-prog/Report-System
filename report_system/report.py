"""리포트 조립 — 진단리포트(마크다운) 생성.

부록 B의 표준 구성(결론 요약 → 커버리지 → 가격 → 수요 → 공급 → 촉매 → 판정)
을 축약 구현한다. 모든 페이지 공통 표기(기준일·버전·한계)를 포함하며,
표현 린트 게이트를 통과하지 못한 문장은 리포트에 실리지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from . import __version__
from .affordability import AffordabilityResult
from .alerts import Alert
from .backtest import BacktestReport
from .catalyst import CatalystCard
from .claims import LintResult
from .feedback import ConsistencyFlag
from .models import DataGrade, DatasetMeta, Site
from .pricing import Band, MarketPosition
from .quality import grade, score
from .scenarios import ScenarioSet
from .subscription import SubscriptionForecast
from .supply import SupplyAssessment
from .timeseries import TrendResult
from .transactions import CleanResult
from .verdicts import Verdict


@dataclass
class ReportInputs:
    site: Site
    asof: date
    clean: CleanResult
    dataset_meta: list[DatasetMeta]
    bands: list[Band]
    positions: list[MarketPosition]
    afford: list[AffordabilityResult]
    sub_forecast: SubscriptionForecast
    sub_forecast_id: str
    supply: SupplyAssessment
    catalysts: list[CatalystCard]
    verdicts: list[Verdict]
    alerts: list[Alert]
    feedback_flags: list[ConsistencyFlag]
    lint: LintResult
    trend: TrendResult | None = None
    scenarios: ScenarioSet | None = None
    scenario_id: str = ""
    backtests: list[BacktestReport] = None  # type: ignore[assignment]
    coverage_rows: list = None  # type: ignore[assignment]
    model_cards: list = None    # type: ignore[assignment]
    drifts: list = None         # type: ignore[assignment]
    liquidity: object | None = None
    profile_note: str = ""
    profile_notes: list = None  # type: ignore[assignment]
    region_stats: object | None = None
    commerce: object | None = None
    unsold: object | None = None

    def __post_init__(self):
        for f in ("backtests", "coverage_rows", "model_cards", "drifts",
                  "profile_notes"):
            if getattr(self, f) is None:
                setattr(self, f, [])


def _fmt_won(v: float) -> str:
    return f"{v/1e4:,.0f}만원"


def generate_markdown(x: ReportInputs) -> str:
    L: list[str] = []
    add = L.append

    add(f"# 현장 진단리포트 — {x.site.name}")
    add("")
    add(f"| 분석 기준일 | {x.asof} | 리포트 버전 | report-system v{__version__} |")
    add("|---|---|---|---|")
    add("")
    add("> 본 리포트는 가격 상승·수익·계약을 보장하지 않습니다. 모든 전망(FORECAST)은 "
        "조건부 구간이며 예측 이력 장부에 봉인되어 실적과 대조됩니다.")
    add("")
    if x.profile_note:
        add(f"**{x.profile_note}**")
        add("")
        for n in x.profile_notes:
            add(f"- {n}")
        add("")

    # 1. 결론 요약 (4개 독립 판정 — 단일 점수로 합산하지 않음)
    add("## 1. 결론 요약 — 4개 독립 판정")
    add("")
    add("| 판정 | 방향 | 강도 | 신뢰도 | 변화 |")
    add("|------|------|------|--------|------|")
    for v in x.verdicts:
        add(f"| {v.name} | {v.direction} | {v.strength} | {v.confidence} | {v.change} |")
    add("")
    for v in x.verdicts:
        add(f"**{v.name}**")
        for r in v.rationale:
            add(f"- {r}")
        add("")

    # 2. 데이터 커버리지
    add("## 2. 데이터 커버리지와 적합성 등급")
    add("")
    add("| 레이어 | 점수 | 등급 | 비고 |")
    add("|--------|------|------|------|")
    for m in x.dataset_meta:
        g = grade(m)
        add(f"| {m.layer} | {score(m)}/100 | {g.value} | {m.note or '-'} |")
    add("")
    if any(grade(m) == DataGrade.D for m in x.dataset_meta):
        add("*D등급 레이어는 본 리포트의 수치·판정에 사용되지 않았습니다.*")
        add("")
    if x.coverage_rows:
        from .coverage_table import as_markdown as cov_md
        add("### 2-1. 15개 레이어 커버리지표 (계약 전 제출용)")
        add("")
        add(cov_md(x.coverage_rows))
        add("")

    # 3. 거래 정제
    add("## 3. 거래 데이터 정제 내역")
    add("")
    from .transactions import RULES_SUMMARY
    add(f"*정제 룰 버전 `{x.clean.rules_version}` — {RULES_SUMMARY}*")
    add("")
    add("| 구분 | 건수 |")
    add("|------|------|")
    for k, v in x.clean.summary.items():
        add(f"| {k} | {v} |")
    add("")

    # 4. 가격
    add("## 4. 품질조정 가격 밴드와 시장 위치")
    add("")
    add("| 수준 | 타입 | 층구간 | q25 | 중위 | q75 | n | 롤업 |")
    add("|------|------|--------|-----|------|-----|---|------|")
    for b in x.bands:
        add(f"| {b.level} | {b.type_name} | {b.floor_band or '-'} | "
            f"{_fmt_won(b.q25)}/㎡ | {_fmt_won(b.q50)}/㎡ | {_fmt_won(b.q75)}/㎡ | "
            f"{b.n} | {'예' if b.rolled_up else '-'} |")
    add("")
    for b in x.bands:
        if b.note:
            add(f"- {b.type_name}: {b.note}")
    add("")
    add("| 타입 | 총취득원가 기준 ㎡당 | 판정 |")
    add("|------|---------------------|------|")
    for p in x.positions:
        add(f"| {p.type_name} | {_fmt_won(p.subject_ppsm)} | {p.label} |")
    add("")

    # 5. 실부담
    add("## 5. 실부담 시뮬레이션")
    add("")
    add("| 타입 | 금리 | 필요 자기자본 | 월 상환(만원) | 구매 가능 가구 비율 |")
    add("|------|------|---------------|----------------|---------------------|")
    for a in x.afford:
        for s in a.scenarios:
            add(f"| {a.type_name} | {s['rate']:.1%} | {_fmt_won(s['equity_required'])} | "
                f"{s['monthly']/1e4:,.0f} | {s['eligible_share']:.0%} |")
    add("")
    add("*가정: 가용 자기자본 ≈ 연소득 ×4, 30년 원리금균등. [LIMITATION] — 가정 변경 시 결과가 달라집니다.*")
    add("")

    # 6. 청약 전망 (FORECAST)
    add("## 6. 청약 수요 전망 [FORECAST]")
    add("")
    if x.sub_forecast.ok:
        f = x.sub_forecast
        add(f"- 예상 경쟁률 구간: **{f.lo:.1f} ~ {f.hi:.1f} : 1** (중위 {f.mid:.1f}, 명목 신뢰수준 {f.confidence:.0%})")
        add(f"- 미달 위험(유사 사례 기준): {f.shortfall_prob:.0%} · 유사 사례 {f.n_cases}건 · 필터 완화 {f.filters_relaxed}단계")
        for s in f.sensitivities:
            add(f"- 민감도: {s}")
        add(f"- 예측 이력 장부 봉인 ID: `{x.sub_forecast_id}` (모델 {f.model_version})")
    else:
        add(f"- {x.sub_forecast.reason}")
    add("")

    # 6-2. 가격 시나리오
    if x.scenarios:
        s = x.scenarios
        add(f"## 6-2. 조건부 가격 시나리오 ({s.horizon_months}개월) [FORECAST]")
        add("")
        if x.trend:
            add(f"- 시장 추세: 전체 {x.trend.slope_pct_per_year:+.1f}%/년, "
                f"최근 {x.trend.recent_slope_pct_per_year:+.1f}%/년"
                f"{' — **국면 전환 신호**' if x.trend.regime_shift else ''} "
                f"(관측 {x.trend.n_months}개월)")
            add("")
        add("| 시나리오 | 연 변화율 | 기간 누적 | 기간말 ㎡당 | 전제 |")
        add("|----------|-----------|-----------|-------------|------|")
        for leg in s.legs:
            add(f"| {leg.name} | {leg.annual_pct:+.1f}% | {leg.horizon_pct:+.1f}% | "
                f"{_fmt_won(leg.price_ppsm)} | {' / '.join(leg.assumptions)} |")
        add("")
        add(f"- 앵커(품질조정 중위): {_fmt_won(s.anchor_ppsm)}/㎡ · "
            f"결과를 가장 크게 가르는 변수: **{s.top_driver()}**")
        add("")
        add("| 민감도 순위 | 드라이버 | 영향 폭(연 %p) |")
        add("|-------------|----------|----------------|")
        for i, (drv, mag) in enumerate(s.sensitivities, 1):
            add(f"| {i} | {drv} | {mag:.1f} |")
        add("")
        if x.scenario_id:
            add(f"- 예측 이력 장부 봉인 ID: `{x.scenario_id}`")
        for lim in s.limitations:
            add(f"- {lim}")
        add("")

    # 6-3. 백테스트
    if x.backtests:
        add("## 6-3. 모델 검증 (백테스트)")
        add("")
        add("*과거 시점에서 그 시점의 정보만으로 산출한 구간을 이후 실현값과 대조한 결과입니다.*")
        add("")
        for bt in x.backtests:
            add(bt.as_markdown())
            add("")
        if x.drifts:
            add("### 6-3-1. 드리프트 감시")
            add("")
            for d in x.drifts:
                add(d.as_markdown())
                add("")
    if x.model_cards:
        add("## 6-4. 모델 카드")
        add("")
        for mc in x.model_cards:
            add(mc.as_markdown())
            add("")

    # 7. 공급
    add("## 7. 확률조정 공급")
    add("")
    add(f"- {x.supply.window_months}개월 내 발표 물량 {x.supply.nominal_units:,}세대 → "
        f"확률조정 {x.supply.adjusted_units:,.0f}세대 ({x.supply.burden_label})")
    add("")
    add("| 공급 | 세대 | 단계 | 조정치 |")
    add("|------|------|------|--------|")
    for name, units, stage, adj in x.supply.items:
        add(f"| {name} | {units:,} | {stage} | {adj:,.0f} |")
    add("")

    # 7-2. 환금성
    if x.liquidity is not None:
        liq = x.liquidity
        add("## 7-2. 환금성")
        add("")
        add("| 지표 | 값 |")
        add("|------|-----|")
        add(f"| 연환산 거래 회전율 | "
            f"{f'{liq.turnover_pct_year:.1f}%' if liq.turnover_pct_year is not None else '산출 불가'} |")
        add(f"| 판정 | {liq.label} (기준 {liq.good_threshold:.0f}%) |")
        add(f"| 가격 분산 | {f'{liq.dispersion:.2f}' if liq.dispersion is not None else '-'} |")
        add(f"| 세대당 평균 거래 간격 | "
            f"{f'{liq.months_between_trades:.0f}개월' if liq.months_between_trades is not None else '-'} |")
        add(f"| 관측 거래 | {liq.n_trades}건 / 최근 {liq.window_months}개월 |")
        add("")
        for lim in liq.limitations:
            add(f"- {lim}")
        add("")

    # 7-3. 지역 기반 통계
    if x.region_stats is not None or x.commerce is not None or x.unsold is not None:
        add("## 7-3. 지역 기반 통계 (L1·L2·L4·L9·L12)")
        add("")
        add("| 레이어 | 요약 |")
        add("|--------|------|")
        if x.region_stats is not None:
            add(f"| L1·L2·L4 인구·가구·사업체 | {x.region_stats.summary()} |")
        if x.commerce is not None:
            add(f"| L9 상권 | {x.commerce.summary()} |")
        if x.unsold is not None:
            add(f"| L12 미분양 | {x.unsold.summary()} |")
        add("")
        for obj in (x.region_stats, x.commerce, x.unsold):
            for lim in (getattr(obj, "limitations", []) or []):
                add(f"- {lim}")
        add("")

    # 8. 촉매카드
    add("## 8. 개발계획 촉매카드")
    add("")
    for c in x.catalysts:
        add(f"### {c.name}")
        add(f"- 공식 단계: {c.stage} ({c.stage_group}) · 실현 가능성 {c.feasibility:.0%} · 현장 관련성 {c.relevance}")
        add(f"- 재정: {c.budget_note}")
        add(f"- 광고 취급: **{c.ad_grade.value}** — {c.ad_rule}")
        if c.negatives:
            add(f"- 반대근거: {', '.join(c.negatives)}")
        if c.sources:
            add(f"- 근거 원문: {', '.join(c.sources)}")
        add("")

    # 9. 조기경보·현장 피드백
    add("## 9. 조기경보 및 현장 반응 정합성")
    add("")
    if x.alerts:
        add("| 유형 | 내용 | 갱신 대상 |")
        add("|------|------|-----------|")
        for a in x.alerts:
            add(f"| {a.category} | {a.message} | {', '.join(a.refresh_targets)} |")
    else:
        add("- 감지된 경보 없음")
    add("")
    if x.feedback_flags:
        add("**현장 반응과 판정의 불일치 (재검토 대상)**")
        add("")
        for fl in x.feedback_flags:
            add(f"- {fl.verdict}: {fl.signal} → {fl.action}")
    else:
        add("- 현장 반응과 판정 간 유의한 불일치 없음")
    add("")

    # 10. 표현 통제 결과
    add("## 10. 광고·상담 사용 문장 (표현 린트 통과분)")
    add("")
    for c in x.lint.passed:
        add(f"- [{c.grade.value} · {c.ad_grade.value}] {c.text}")
    if x.lint.blocked:
        add("")
        add("**차단된 문장 (게시 불가)**")
        add("")
        for c, reason in x.lint.blocked:
            add(f"- ~~{c.text}~~ — {reason}")
    add("")

    add("---")
    add("*본 리포트의 분석은 분양 마케팅·판매전략 목적이며 감정평가·투자자문이 아닙니다. "
        "수치·판정은 기재된 기준일의 데이터와 가정에 따르며, 조기경보 갱신 시 재발행됩니다.*")
    return "\n".join(L)
