"""개발호재 검증 — 성숙도 엔진 (제안서 5.6, 부록 A.13~A.15).

기대효과 = 실현 가능성 × 현장 관련성 × 조건부 영향.
수치 정밀도보다 구성 요소와 불확실성의 분리를 우선한다.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import AdGrade, CatalystPlan, MaturityStage

STAGE_FEASIBILITY = {
    MaturityStage.IDEA: 0.10,
    MaturityStage.PLANNED: 0.30,
    MaturityStage.FEASIBILITY: 0.45,
    MaturityStage.DESIGN: 0.65,
    MaturityStage.CONTRACT: 0.80,
    MaturityStage.CONSTRUCTION: 0.92,
    MaturityStage.OPEN: 1.00,
}

STAGE_GROUP = {
    MaturityStage.IDEA: "검토 단계",
    MaturityStage.PLANNED: "추진 단계",
    MaturityStage.FEASIBILITY: "추진 단계",
    MaturityStage.DESIGN: "추진 단계",
    MaturityStage.CONTRACT: "확정·실행 단계",
    MaturityStage.CONSTRUCTION: "확정·실행 단계",
    MaturityStage.OPEN: "확정·실행 단계",
}

AD_TREATMENT = {
    "검토 단계": (AdGrade.FORBIDDEN, "가능성으로만 표시. 확정적 표현 금지"),
    "추진 단계": (AdGrade.CONDITIONAL, "일정과 변경 위험 병기"),
    "확정·실행 단계": (AdGrade.CONDITIONAL, "현장 영향·가격 반영 검증 후 표시"),
}


@dataclass
class CatalystCard:
    """고객용 촉매카드 (제안서 5.9.2)."""
    plan_id: str
    name: str
    stage: str
    stage_group: str
    feasibility: float          # 실현 가능성 (0~1)
    relevance: str              # 현장 관련성 (상/중/하)
    budget_note: str
    ad_grade: AdGrade
    ad_rule: str
    negatives: list[str]
    sources: list[str]


def _relevance(plan: CatalystPlan) -> str:
    if plan.dist_m <= 800 and plan.time_saving_min >= 10:
        return "상"
    if plan.dist_m <= 2000 and plan.time_saving_min >= 5:
        return "중"
    return "하"


def assess(plan: CatalystPlan) -> CatalystCard:
    group = STAGE_GROUP[plan.stage]
    ad_grade, rule = AD_TREATMENT[group]
    secured_ratio = (plan.budget_secured / plan.budget_total) if plan.budget_total else 0.0
    feas = STAGE_FEASIBILITY[plan.stage]
    # 재정 집행이 뒷받침되지 않는 추진 단계는 실현 가능성 하향 (A.14)
    if group == "추진 단계" and secured_ratio < 0.10:
        feas *= 0.7
    return CatalystCard(
        plan_id=plan.id,
        name=plan.name,
        stage=plan.stage.value,
        stage_group=group,
        feasibility=round(feas, 2),
        relevance=_relevance(plan),
        budget_note=f"총사업비 {plan.budget_total/1e8:,.0f}억 중 확보 {secured_ratio:.0%}",
        ad_grade=ad_grade,
        ad_rule=rule,
        negatives=list(plan.negatives),
        sources=list(plan.source_docs),
    )
