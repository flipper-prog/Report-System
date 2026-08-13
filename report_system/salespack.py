"""판매 논리 산출물 — 분석을 영업 언어로 옮기는 통제 지점 (제안서 제6장).

진단리포트는 분석가·발주처가 읽습니다. 그러나 실제로 고객을 만나는 것은 상담
조직이고, 그들이 쓰는 문장이 리포트와 어긋나면 분석 품질과 무관하게 신뢰가
무너집니다. 본 모듈은 분석 결과에서 다음을 **자동 생성**하여 그 간극을 없앱니다.

  · 메시지맵    고객 질문 6축 × 근거 × 답변 문장 × 표현 등급
  · 근거카드    결론·근거·산출·반대·영업 지침의 5항목 (5.9.2)
  · 거절 대응   빈출 거절 사유별 사실 기반 대응 논리

**설계의 핵심은 답하지 않는 능력입니다.** 각 질문 축은 그 축을 지지하는 분석
결과가 실제로 산출되었을 때만 답변을 생성합니다. 근거가 없으면 그럴듯한 문장을
만들지 않고 "이 질문에는 아직 근거로 답할 수 없다"를 그대로 내보냅니다 —
상담원이 근거 없는 문장을 손에 쥐는 것이 가장 위험하기 때문입니다.

생성된 모든 문장은 표현 린트(claims.lint)를 통과해야 하며, 차단된 문장은
산출물에 실리지 않고 차단 사유와 함께 별도 표기됩니다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .claims import LintResult, lint
from .models import AdGrade, Claim, ClaimGrade

#: 제안서 [표 6-3] 고객 질문 6축
QUESTIONS = [
    ("Q1", "분양가가 비싼 것 아닌가?", "같은 가격이 아니라 같은 조건으로 비교"),
    ("Q2", "입주 시점에도 수요가 있는가?", "어떤 고객층이 왜 필요한지를 데이터로 설명"),
    ("Q3", "공급이 많아지지 않는가?", "공급 위험과 흡수 조건을 함께 제시"),
    ("Q4", "교통호재가 실제로 되는가?", "추진 단계와 현장 효과를 분리하여 제시"),
    ("Q5", "나중에 팔 수 있는가?", "환금성의 근거와 제약을 함께 설명"),
    ("Q6", "왜 지금 검토해야 하는가?", "과장된 긴급성 대신 확인 가능한 판단 시점"),
]

#: 거절 사유별 대응의 출발점. 실제 문장은 분석 결과로 채워진다.
REJECTION_FRAMES = {
    "가격": "총취득원가와 품질조정 비교로 되돌린다",
    "대출": "실부담 시뮬레이션의 자기자본·월 상환 구간으로 답한다",
    "가족협의": "판단에 필요한 근거를 문서로 넘겨 재논의가 가능하게 한다",
    "경쟁현장": "경쟁 현장의 확인된 변동과 비교 기준을 제시한다",
    "교통": "성숙도 단계와 현장 효과를 분리해 설명한다",
}

#: 거절 사유가 이 비중을 넘으면 대응 자료를 우선 항목으로 배치
PRIORITY_SHARE = 0.15


@dataclass
class Answer:
    """질문 축 하나에 대한 답변 단위 = 근거카드 1장."""
    code: str
    question: str
    direction: str                      # 설득 방향 (제안서 [표 6-3])
    headline: str = ""                  # 한 줄 답. 근거가 없으면 비어 있다
    evidence: list[str] = field(default_factory=list)
    counter: list[str] = field(default_factory=list)
    claim: Optional[Claim] = None
    unanswerable_reason: str = ""

    @property
    def answerable(self) -> bool:
        return bool(self.headline)


@dataclass
class Rebuttal:
    reason: str
    count: int
    share: float
    frame: str
    response: str
    evidence: list[str] = field(default_factory=list)

    @property
    def priority(self) -> bool:
        return self.share >= PRIORITY_SHARE


@dataclass
class SalesPack:
    answers: list[Answer] = field(default_factory=list)
    rebuttals: list[Rebuttal] = field(default_factory=list)
    lint: Optional[LintResult] = None
    notes: list[str] = field(default_factory=list)

    @property
    def answered(self) -> int:
        return sum(1 for a in self.answers if a.answerable)

    def as_markdown(self) -> str:
        passed = {c.text for c in (self.lint.passed if self.lint else [])}
        L = [f"*질문 6축 중 {self.answered}축 답변 가능 · "
             f"{len(self.answers) - self.answered}축 근거 부족*", "",
             "### 메시지맵", "",
             "| 축 | 고객 질문 | 답변 | 표현 등급 |",
             "|----|-----------|------|-----------|"]
        for a in self.answers:
            if not a.answerable:
                L.append(f"| {a.code} | {a.question} | **답변 불가** — "
                         f"{a.unanswerable_reason} | — |")
                continue
            grade = (f"{a.claim.grade.value} · {a.claim.ad_grade.value}"
                     if a.claim else "—")
            blocked = a.claim is not None and a.claim.text not in passed
            text = f"~~{a.headline}~~ (린트 차단)" if blocked else a.headline
            L.append(f"| {a.code} | {a.question} | {text} | {grade} |")

        L += ["", "### 근거카드", ""]
        for a in self.answers:
            L.append(f"**{a.code}. {a.question}**")
            L.append("")
            if not a.answerable:
                L += [f"- 결론: 답변 불가 — {a.unanswerable_reason}",
                      "- 영업: 이 축에 대해서는 근거 없는 답변을 하지 않는다. "
                      "확인 후 회신을 원칙으로 한다", ""]
                continue
            L.append(f"- 결론: {a.headline}")
            L.append(f"- 설득 방향: {a.direction}")
            for e in a.evidence:
                L.append(f"- 근거: {e}")
            for c in a.counter:
                L.append(f"- 반대·한계: {c}")
            if a.claim:
                L.append(f"- 영업: [{a.claim.grade.value} · "
                         f"{a.claim.ad_grade.value}] 등급으로 사용")
            L.append("")

        if self.rebuttals:
            L += ["### 거절 대응 자료", "",
                  "| 거절 사유 | 비중 | 대응 논리 |",
                  "|-----------|------|-----------|"]
            for r in self.rebuttals:
                mark = " **우선**" if r.priority else ""
                L.append(f"| {r.reason}{mark} | {r.share:.0%} ({r.count}건) | "
                         f"{r.response} |")
            L.append("")

        if self.lint and self.lint.blocked:
            L += ["**린트 차단 문장 (사용 금지)**", ""]
            L += [f"- ~~{c.text}~~ — {why}" for c, why in self.lint.blocked]
            L.append("")

        L += ["*근거가 산출되지 않은 축은 답변을 생성하지 않습니다. "
              "상담원이 근거 없는 문장을 손에 쥐는 것이 가장 위험하기 때문입니다.*"]
        if self.notes:
            L += [""] + [f"- {n}" for n in self.notes]
        return "\n".join(L)


# ── 축별 답변 생성 ───────────────────────────────────────────────────────────

def _q1_price(positions, jeonse, decision) -> Answer:
    a = Answer("Q1", QUESTIONS[0][1], QUESTIONS[0][2])
    if not positions:
        a.unanswerable_reason = "비교 표본 부족으로 가격 위치 미산출"
        return a
    labels = [p.label for p in positions]
    lower = sum("하단" in l for l in labels)
    upper = sum("상단" in l for l in labels)
    where = "하단" if lower > upper else ("상단" if upper > lower else "내")
    n = min(p.band.n for p in positions)
    a.headline = (f"총취득원가 기준으로 비교하면 품질조정 밴드 {where}에 "
                  f"위치합니다(비교 거래 {n}건)")
    a.evidence = [f"{p.type_name}: 취득원가 {p.subject_ppsm/1e4:,.0f}만원/㎡ → {p.label}"
                  for p in positions]
    if jeonse is not None and getattr(jeonse, "ratio_pct", None) is not None:
        a.evidence.append(f"전세가율 {jeonse.ratio_pct:.0f}% — {jeonse.label}")
        if jeonse.label == "하방 완충 얇음":
            a.counter.append("전세 완충이 얇아 가격 조정 시 하방 여지가 큼")
    if decision is not None and decision.recommended is not None:
        r = decision.recommended
        if r.multiplier < -0.001:
            a.counter.append(
                f"내부 시뮬레이션상 현재가 대비 {r.multiplier:+.1%} 구간이 "
                "미달 위험·밴드 조건을 모두 만족 — 조건 협의 시 참고")
    a.counter.append("비교는 총취득원가 기준입니다. 분양가만 비교하면 결과가 달라집니다")
    a.claim = Claim(text=a.headline, grade=ClaimGrade.CALCULATION,
                    ad_grade=AdGrade.ALLOWED, evidence=["pricing:positions"],
                    counter=a.counter)
    return a


def _q2_demand(verdicts, afford, sub_fc, layers: dict) -> Answer:
    a = Answer("Q2", QUESTIONS[1][1], QUESTIONS[1][2])
    v2 = next((v for v in verdicts if v.name.startswith("②")), None)
    grounds = [k for k, v in layers.items() if v is not None]
    share = None
    if afford:
        vals = [x.scenarios[1]["eligible_share"] for x in afford
                if len(x.scenarios) > 1 and x.scenarios[1]["eligible_share"] is not None]
        share = sum(vals) / len(vals) if vals else None
    if v2 is None or (not grounds and share is None and not sub_fc.ok):
        a.unanswerable_reason = "수요 레이어가 수집되지 않아 근거 제시 불가"
        return a

    a.headline = f"수요 지속성 판정은 '{v2.direction}'이며, 근거는 아래와 같습니다"
    a.evidence = list(v2.rationale)
    if share is not None:
        a.counter.append(
            f"기준 금리에서 구매 가능 가구 비율은 {share:.0%}입니다 — "
            "이 비율이 곧 수요 규모는 아니며 자기자본 가정에 따라 달라집니다")
    else:
        a.counter.append("구매 가능 가구 비율은 소득 표본 부족으로 미산출")
    if not sub_fc.ok:
        a.counter.append(f"청약 전망: {sub_fc.reason}")
    a.claim = Claim(text=a.headline, grade=ClaimGrade.INFERENCE,
                    ad_grade=AdGrade.CONDITIONAL, evidence=["verdict:demand"],
                    counter=a.counter)
    return a


def _q3_supply(supply, site_units, unsold, housing) -> Answer:
    a = Answer("Q3", QUESTIONS[2][1], QUESTIONS[2][2])
    if supply is None or not site_units:
        a.unanswerable_reason = "공급 목록 또는 현장 세대수 미입력"
        return a
    ratio = supply.adjusted_units / site_units
    a.headline = (f"{supply.window_months}개월 내 확률조정 공급은 현장 세대수의 "
                  f"{ratio:.1f}배입니다")
    a.evidence = [f"발표 물량 {supply.nominal_units:,}세대 → 단계별 실현 가능성 "
                  f"가중 후 {supply.adjusted_units:,.0f}세대"]
    if unsold is not None and getattr(unsold, "latest", None) is not None:
        a.evidence.append(f"미분양: {unsold.summary()}")
        t = unsold.trend_pct()
        if t is not None and t >= 20:
            a.counter.append("미분양이 증가 추세여서 흡수 속도가 느려질 수 있음")
    if housing is not None and getattr(housing, "points", None):
        a.evidence.append(f"인허가 실적: {housing.summary()}")
        y = housing.yoy_pct("permit")
        if y is not None and y >= 30:
            a.counter.append("인허가 급증으로 2~3년 후 추가 공급 압력이 예상됨")
    a.counter.append("확률조정 공급의 단계별 실현률은 실적 누적 전까지 초기값입니다")
    a.claim = Claim(text=a.headline, grade=ClaimGrade.CALCULATION,
                    ad_grade=AdGrade.CONDITIONAL, evidence=["supply:adjusted"],
                    counter=a.counter)
    return a


def _q4_catalyst(cards) -> Answer:
    a = Answer("Q4", QUESTIONS[3][1], QUESTIONS[3][2])
    if not cards:
        a.unanswerable_reason = "평가 대상 개발계획이 등록되지 않음"
        return a
    usable = [c for c in cards if c.ad_grade != AdGrade.FORBIDDEN]
    if not usable:
        a.unanswerable_reason = ("등록된 개발계획이 모두 검토 단계로, "
                                 "광고·상담에서 확정적으로 언급할 수 없음")
        return a
    a.headline = (f"등록된 개발계획 {len(cards)}건 중 {len(usable)}건이 "
                  "조건부로 언급 가능한 단계입니다")
    a.evidence = [f"{c.name}: {c.stage}({c.stage_group}) · 실현성 {c.feasibility:.0%} · "
                  f"{c.budget_note} → 광고 {c.ad_grade.value}" for c in cards]
    for c in cards:
        for neg in c.negatives:
            a.counter.append(f"{c.name}: {neg}")
    a.counter.append("추진 단계 사업은 일정과 내용이 변경될 수 있습니다")
    a.claim = Claim(text=a.headline, grade=ClaimGrade.INFERENCE,
                    ad_grade=AdGrade.CONDITIONAL, evidence=["catalyst:cards"],
                    counter=a.counter)
    return a


def _q5_liquidity(liq, jeonse) -> Answer:
    a = Answer("Q5", QUESTIONS[4][1], QUESTIONS[4][2])
    if liq is None or liq.turnover_pct_year is None:
        a.unanswerable_reason = "비교단지 세대수 미입력 또는 거래 부족으로 회전율 미산출"
        return a
    a.headline = (f"인근 시장의 연환산 거래 회전율은 {liq.turnover_pct_year:.1f}%로 "
                  f"{liq.label} 수준입니다")
    a.evidence = [liq.as_rationale()]
    if jeonse is not None and getattr(jeonse, "ratio_pct", None) is not None:
        a.evidence.append(f"전세가율 {jeonse.ratio_pct:.0f}% — 전세 수요가 "
                          "매도 시 하방을 받치는 정도")
    a.counter += list(liq.limitations)
    a.counter.append("회전율은 과거 실적이며 매도 시점의 시장 여건을 보장하지 않습니다")
    a.claim = Claim(text=a.headline, grade=ClaimGrade.CALCULATION,
                    ad_grade=AdGrade.CONDITIONAL, evidence=["liquidity:turnover"],
                    counter=a.counter)
    return a


def _q6_timing(supply, site_units, cards, alerts) -> Answer:
    a = Answer("Q6", QUESTIONS[5][1], QUESTIONS[5][2])
    signals: list[str] = []
    if supply is not None and site_units:
        ratio = supply.adjusted_units / site_units
        signals.append(f"{supply.window_months}개월 내 확률조정 공급 {ratio:.1f}배 — "
                       "경쟁 물량의 시기 축")
    staged = [c for c in (cards or []) if c.ad_grade != AdGrade.FORBIDDEN]
    if staged:
        signals.append(f"개발계획 {len(staged)}건이 추진 단계 — 단계 변동 시 "
                       "판단 근거가 갱신됨")
    if alerts:
        signals.append(f"조기경보 {len(alerts)}건 감지 — 근거 갱신 대상 존재")
    if not signals:
        a.unanswerable_reason = "판단 시점을 지지할 공급·계획·경보 근거 없음"
        return a
    a.headline = ("지금 검토해야 할 이유는 마감 임박이 아니라 아래 조건들이 "
                  "확인 가능한 시점이기 때문입니다")
    a.evidence = signals
    a.counter.append("위 조건은 변동될 수 있으며, 변동 시 판단 근거도 함께 갱신됩니다")
    a.counter.append("긴급성을 근거 없이 강조하는 표현은 사용하지 않습니다")
    a.claim = Claim(text=a.headline, grade=ClaimGrade.INFERENCE,
                    ad_grade=AdGrade.CONDITIONAL, evidence=["timing:signals"],
                    counter=a.counter)
    return a


# ── 거절 대응 ────────────────────────────────────────────────────────────────

def _rebuttals(feedback, answers: dict[str, Answer]) -> list[Rebuttal]:
    if not feedback or not feedback.rejections:
        return []
    total = sum(feedback.rejections.values())
    if total <= 0:
        return []

    by_reason = {
        "가격": ("Q1", "총취득원가·품질조정 비교로 되돌리고, 전세 완충과 실부담을 함께 제시"),
        "대출": ("Q2", "실부담 시뮬레이션의 자기자본·월 상환 구간을 금리 시나리오별로 제시"),
        "가족협의": ("Q1", "근거카드를 문서로 전달해 재논의 시 같은 기준으로 검토되게 함"),
        "경쟁현장": ("Q3", "확인된 경쟁 현장 변동과 공급 흡수 조건을 비교 기준으로 제시"),
        "교통": ("Q4", "성숙도 단계와 현장 효과를 분리해 설명하고 반대근거를 함께 제시"),
    }
    out: list[Rebuttal] = []
    for reason, count in sorted(feedback.rejections.items(), key=lambda kv: -kv[1]):
        qcode, resp = by_reason.get(
            reason, ("", "해당 사유에 대응할 표준 논리가 정의되지 않음 — 신규 정의 필요"))
        src = answers.get(qcode)
        if qcode and (src is None or not src.answerable):
            resp = (f"{qcode} 축의 근거가 산출되지 않아 사실 기반 대응 불가 — "
                    "확인 후 회신 원칙")
        out.append(Rebuttal(
            reason=reason, count=count, share=count / total,
            frame=REJECTION_FRAMES.get(reason, "표준 프레임 미정의"),
            response=resp,
            evidence=list(src.evidence[:2]) if src and src.answerable else []))
    return out


def build(*, positions, verdicts, afford, sub_forecast, supply, site_units,
          catalysts, alerts, feedback, liquidity=None, jeonse=None,
          unsold=None, housing=None, price_decision=None,
          region_stats=None, migration=None, mobility=None, transit=None,
          commerce=None) -> SalesPack:
    """분석 결과에서 판매 논리 산출물을 생성하고 표현 린트를 적용한다."""
    layers = {"region_stats": region_stats, "migration": migration,
              "mobility": mobility, "transit": transit, "commerce": commerce}

    answers = [
        _q1_price(positions, jeonse, price_decision),
        _q2_demand(verdicts, afford, sub_forecast, layers),
        _q3_supply(supply, site_units, unsold, housing),
        _q4_catalyst(catalysts),
        _q5_liquidity(liquidity, jeonse),
        _q6_timing(supply, site_units, catalysts, alerts),
    ]
    by_code = {a.code: a for a in answers}

    pack = SalesPack(answers=answers,
                     rebuttals=_rebuttals(feedback, by_code))
    pack.lint = lint([a.claim for a in answers if a.claim is not None])

    unanswered = [a.code for a in answers if not a.answerable]
    if unanswered:
        pack.notes.append(
            f"답변 불가 축: {', '.join(unanswered)} — 해당 질문은 상담에서 "
            "'확인 후 회신'으로 처리하고, 근거 확보 후 재생성합니다")
    if pack.lint.blocked:
        pack.notes.append(
            f"린트 차단 {len(pack.lint.blocked)}건 — 차단된 문장은 산출물에서 "
            "제외되며 광고·상담에 사용할 수 없습니다")
    return pack
