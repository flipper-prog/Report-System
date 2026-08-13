"""모델 카드 (P2-7) + 드리프트 감시 (P2-1).

모델 카드: 모델별 학습 범위·피처·성능·한계·적용 범위를 1페이지로 고정 표기.
드리프트: 백테스트 적중률을 기간별로 분할해 성능 저하를 감지하고 재학습을 권고한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .backtest import BacktestReport


@dataclass
class ModelCard:
    model_id: str
    version: str
    purpose: str
    inputs: list[str]
    output: str
    training_window: str
    validation: str
    known_limits: list[str]
    applicable: list[str]
    not_applicable: list[str]

    def as_markdown(self) -> str:
        L = [f"### {self.model_id} `{self.version}`", "",
             f"| 항목 | 내용 |", "|------|------|",
             f"| 목적 | {self.purpose} |",
             f"| 입력 | {', '.join(self.inputs)} |",
             f"| 산출 | {self.output} |",
             f"| 학습·참조 범위 | {self.training_window} |",
             f"| 검증 | {self.validation} |",
             f"| 적용 가능 | {', '.join(self.applicable)} |",
             f"| 적용 불가 | {', '.join(self.not_applicable)} |", ""]
        L.append("**알려진 한계**")
        L += [f"- {x}" for x in self.known_limits]
        return "\n".join(L)


def price_band_card(n_tx: int, span: str, bt: BacktestReport | None,
                    coef=None) -> ModelCard:
    if coef is None or coef.source.startswith("초기값"):
        coef_limit = ("조정계수(연식 1%/년, 층 ±2~3%)는 백테스트 교정 전 예시값 — "
                      "`calibrate` 실행으로 교체 가능")
    else:
        coef_limit = (f"조정계수 출처: {coef.source} — 연식 "
                      f"{coef.age_per_year*100:.2f}%/년, 저층 "
                      f"{coef.floor_low*100:+.2f}%, 상층 {coef.floor_high*100:+.2f}%. "
                      "백테스트 적중률이 개선될 때만 교체된 값")
    return ModelCard(
        model_id="품질조정 가격 밴드", version="qab-0.1",
        purpose="비교 거래를 연식·층·상품 차이로 조정해 타입별 가격 구간을 산출",
        inputs=["정제 실거래(E01)", "비교단지 메타(연식·거리·분양권 여부)", "현장 타입 스펙"],
        output="타입(·층구간)별 q25/중위/q75 ㎡당 가격 밴드",
        training_window=f"거래 {n_tx}건 / {span}",
        validation=(bt.verdict() if bt else "백테스트 미실행"),
        known_limits=[
            coef_limit,
            "비교단지 선정이 설정 의존적 — 단지명 불일치 시 표본 편향",
            "표본 미달 구간은 상위 수준으로 롤업되어 정밀도가 낮아짐",
        ],
        applicable=["아파트 분양 현장", "비교 거래가 확보된 시장권"],
        not_applicable=["비교군이 없는 신규 택지", "오피스텔·지식산업센터(상품 프로파일 별도)"])


def subscription_card(n_cases: int, bt: BacktestReport | None) -> ModelCard:
    return ModelCard(
        model_id="청약 수요 전망", version="sub-empirical-0.1",
        purpose="유사 조건 청약 사례의 경험분포로 경쟁률 구간과 미달 위험을 전망",
        inputs=["청약홈 이력(E02)", "가격 갭", "동시 공급", "지역"],
        output="타입별 경쟁률 구간(q20~q80), 미달 위험, 민감 변수",
        training_window=f"유사 사례 {n_cases}건",
        validation=(bt.verdict() if bt else "백테스트 미실행"),
        known_limits=[
            "청약홈은 가격 갭·동시 공급을 제공하지 않아 해당 조건은 매칭에서 제외될 수 있음",
            "제도 변경(규제지역·가점제 비중) 전후 사례의 혼재 가능",
            "표본 5건 미만이면 수치를 제시하지 않고 정성 판정으로 전환",
        ],
        applicable=["청약 방식 공급 현장"],
        not_applicable=["임의공급·선착순 분양", "청약 이력이 없는 신설 시장권"])


def scenario_card(horizon: int) -> ModelCard:
    return ModelCard(
        model_id="조건부 가격 시나리오", version="scenario-driver-0.1",
        purpose="추세·공급·금리·촉매의 가정별 조정으로 하방/기준/상방 구간을 제시",
        inputs=["시계열 추세", "확률조정 공급배수", "금리 시나리오", "촉매 성숙도"],
        output=f"{horizon}개월 시나리오 3구간 + 민감도 순위",
        training_window="현장 시장권 실거래 시계열",
        validation="전제 기반 구간 — 확률 구간이 아니므로 적중률 산출 대상 아님",
        known_limits=[
            "드라이버 탄력성은 문헌·경험 기반 초기값이며 실적 누적으로 교정 필요",
            "정책·공급 충격 등 구조 변화는 반영하지 못함",
            "구간은 확률적 신뢰구간이 아니라 '가정별 결과'임",
        ],
        applicable=["가격 전략 검토", "판촉 시점 판단"],
        not_applicable=["감정평가", "투자 수익 보장 목적의 제시"])


# ── 드리프트 감시 (P2-1) ─────────────────────────────────────────────────────

@dataclass
class DriftReport:
    model_id: str
    windows: list[tuple[str, int, float]] = field(default_factory=list)  # (기간, n, coverage)
    verdict: str = ""

    def as_markdown(self) -> str:
        L = [f"**{self.model_id}** — {self.verdict}", "",
             "| 기간 | 표본 | 적중률 |", "|------|------|--------|"]
        L += [f"| {w} | {n} | {c:.0%} |" for w, n, c in self.windows]
        return "\n".join(L)


def detect_drift(bt: BacktestReport, split_year: int | None = None,
                 min_per_window: int = 8, degrade_threshold: float = 0.15) -> DriftReport:
    """연도별 적중률을 비교해 최근 구간의 성능 저하를 감지한다."""
    rep = DriftReport(bt.name)
    if not bt.folds:
        rep.verdict = "표본 없음 — 판정 불가"
        return rep

    by_year: dict[int, list[bool]] = {}
    for f in bt.folds:
        by_year.setdefault(f.cutoff.year, []).append(f.hit)

    usable = [(y, v) for y, v in sorted(by_year.items()) if len(v) >= min_per_window]
    for y, v in usable:
        rep.windows.append((str(y), len(v), sum(v) / len(v)))

    if len(usable) < 2:
        rep.verdict = "비교 가능한 기간 2개 미만 — 판정 유보"
        return rep

    first_cov = rep.windows[0][2]
    last_cov = rep.windows[-1][2]
    if first_cov - last_cov >= degrade_threshold:
        rep.verdict = (f"최근 구간 적중률 {last_cov:.0%}가 초기 {first_cov:.0%} 대비 "
                       f"{(first_cov-last_cov):.0%}p 하락 — **재학습·재보정 권고**")
    else:
        rep.verdict = f"기간 간 적중률 변동 {abs(first_cov-last_cov):.0%}p — 안정"
    return rep
