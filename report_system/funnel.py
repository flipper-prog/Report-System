"""퍼널 병목 진단 (제안서 10.5).

성과 지표의 가치는 총량이 아니라 **병목의 위치와 원인을 구분**하는 데 있습니다.
"계약이 부진하다"는 관찰은 대응을 지시하지 않습니다. 광고를 늘려야 하는지,
상담을 교정해야 하는지, 분양 조건을 재검토해야 하는지가 갈리기 때문입니다.

본 모듈은 단계별 전환율을 기준선과 대조해 가장 크게 미달한 단계를 병목으로
지목하고, 그 단계에 대응하는 원인 추정과 우선 개선 항목을 반환합니다.

**기준선 없이는 병목을 판정하지 않습니다.** 전환율 20%가 좋은지 나쁜지는
기준선 없이 말할 수 없습니다. 기준선이 설정되지 않았으면 실측값만 제시하고
판정을 유보합니다 — 근거 없는 병목 지목은 잘못된 예산 배분으로 직결됩니다.

**운영 문제와 조건 문제를 구분합니다.** 방문까지 도달했는데 계약이 나오지 않는
병목은 광고로 해결되지 않습니다. 이 경우 판촉 반복을 권하지 않고 **시행사
조건 검토 안건**으로 승격합니다 (제안서 3.5·10.5).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

#: 전환율을 판정하기 위한 최소 분모. 미달 단계는 판정 유보한다.
MIN_DENOMINATOR = 30

#: 기준선 대비 이만큼(비율) 밑돌면 미달로 본다
SHORTFALL_MARGIN = 0.20

#: 연락 지연이 이 비중을 넘으면 광고가 아닌 운영 병목으로 분류 (표 10-7)
CONTACT_DELAY_ALERT = 0.20

#: 단계 정의 — (코드, 표시명, 분자 필드, 분모 필드)
STAGES = [
    ("S1", "클릭 → 유효 DB", "leads", "clicks"),
    ("S2", "유효 DB → 상담완료", "consulted", "leads"),
    ("S3", "상담완료 → 방문", "visited", "consulted"),
    ("S4", "방문 → 계약", "contracted", "visited"),
]

#: 제안서 [표 10-6] 관찰 결과별 원인 추정과 우선 개선
DIAGNOSIS = {
    "S1": ("후킹과 실제 정보의 불일치, 폼 품질",
           "메시지 정합성, 질문 항목, 중복 필터"),
    "S2": ("연락 지연, 시간대, 배정 과부하",
           "연락 기준시간, 재배정, 후속 메시지 보조"),
    "S3": ("가격 저항, 가족 협의, 정보 부족",
           "비교 자료, 방문 이유 설계, 일정 확인"),
    "S4": ("상품·가격·자금·경쟁 현장",
           "현장 조건·상담·후속 제안 검토 (시행사 협의 안건)"),
}

#: 병목이 이 단계면 광고 운영으로 해결되지 않는다
CONDITION_STAGE = "S4"


@dataclass
class FunnelSnapshot:
    """기간 내 퍼널 실적. 선택 필드는 0이면 해당 점검을 건너뛴다."""
    clicks: int = 0
    leads: int = 0            # 유효 DB
    consulted: int = 0        # 상담완료
    visited: int = 0
    contracted: int = 0
    spend: int = 0            # 실집행 광고비(원)
    contact_delayed: int = 0  # 기준시간 초과 연락 건수
    followups_sent: int = 0

    def get(self, name: str) -> int:
        return int(getattr(self, name, 0) or 0)

    @property
    def cost_per_contract(self) -> Optional[float]:
        return self.spend / self.contracted if self.contracted else None

    @property
    def cost_per_lead(self) -> Optional[float]:
        return self.spend / self.leads if self.leads else None


@dataclass
class StageResult:
    code: str
    name: str
    numerator: int
    denominator: int
    rate: Optional[float]
    benchmark: Optional[float]
    note: str = ""

    @property
    def measurable(self) -> bool:
        return self.rate is not None

    @property
    def gap(self) -> Optional[float]:
        """기준선 대비 상대 격차. 음수면 미달."""
        if self.rate is None or not self.benchmark:
            return None
        return (self.rate - self.benchmark) / self.benchmark

    @property
    def shortfall(self) -> bool:
        g = self.gap
        return g is not None and g <= -SHORTFALL_MARGIN


@dataclass
class FunnelDiagnosis:
    stages: list[StageResult] = field(default_factory=list)
    bottleneck: Optional[StageResult] = None
    cause: str = ""
    action: str = ""
    is_condition_issue: bool = False
    corroboration: list[str] = field(default_factory=list)
    operational_flags: list[str] = field(default_factory=list)
    verdict: str = ""
    limitations: list[str] = field(default_factory=list)

    def as_markdown(self) -> str:
        L = ["| 단계 | 전환율 | 기준선 | 격차 | 표본 |",
             "|------|--------|--------|------|------|"]
        for s in self.stages:
            if not s.measurable:
                L.append(f"| {s.name} | 판정 불가 | — | — | 분모 {s.denominator}건 |")
                continue
            bm = f"{s.benchmark:.1%}" if s.benchmark else "미설정"
            gap = f"{s.gap:+.0%}" if s.gap is not None else "—"
            mark = " **←병목**" if s is self.bottleneck else ""
            L.append(f"| {s.name}{mark} | {s.rate:.1%} | {bm} | {gap} | "
                     f"{s.numerator:,}/{s.denominator:,} |")
        L += ["", f"**판정**: {self.verdict}"]
        if self.bottleneck is not None:
            L += ["", f"- 원인 추정: {self.cause}",
                  f"- 우선 개선: {self.action}"]
            if self.is_condition_issue:
                L.append("- **분류: 조건 문제** — 광고 운영으로 해결되지 않습니다. "
                         "판촉 반복 대신 시행사 조건 검토 안건으로 제기합니다")
            else:
                L.append("- 분류: 운영 문제 — 광고·상담 운영 범위에서 개선 가능")
        for c in self.corroboration:
            L.append(f"- 교차 확인: {c}")
        for f in self.operational_flags:
            L.append(f"- 운영 점검: {f}")
        if self.limitations:
            L.append("")
            L += [f"- {x}" for x in self.limitations]
        return "\n".join(L)


def _corroborate(bottleneck: Optional[StageResult], feedback,
                 verdicts) -> list[str]:
    """병목 추정을 현장 반응·분석 판정과 대조한다.

    퍼널 수치만으로는 원인이 여러 개일 수 있습니다. 거절 사유와 가격 판정이
    같은 방향을 가리키면 추정의 확신이 올라가고, 어긋나면 그 사실 자체가
    재검토 대상입니다 (5.11.3과 같은 규율).
    """
    out: list[str] = []
    if bottleneck is None or not feedback or not feedback.rejections:
        return out
    total = sum(feedback.rejections.values()) or 1
    price_share = feedback.rejections.get("가격", 0) / total
    comp_share = feedback.rejections.get("경쟁현장", 0) / total
    loan_share = feedback.rejections.get("대출", 0) / total

    if bottleneck.code in ("S3", "S4"):
        if price_share >= 0.30:
            out.append(f"거절 사유 중 가격 비중 {price_share:.0%} — 병목 추정과 정합")
            v1 = next((v for v in verdicts if v.name.startswith("①")), None)
            if v1 is not None and v1.direction == "긍정":
                out.append("다만 가격 판정은 '긍정'입니다 — 가격 자체보다 "
                           "총취득원가 설명 방식의 문제일 수 있어 양쪽을 함께 점검")
        if comp_share >= 0.25:
            out.append(f"경쟁현장 사유 이탈 {comp_share:.0%} — 경쟁 물량 평가와 "
                       "비교 논리 갱신 필요")
        if loan_share >= 0.20 and bottleneck.code == "S4":
            out.append(f"대출 사유 {loan_share:.0%} — 실부담 시뮬레이션 기준으로 "
                       "자금 안내 재점검")
    return out


def diagnose(snap: FunnelSnapshot,
             benchmarks: "dict[str, float] | None" = None,
             feedback=None, verdicts=None,
             contact_sla_hours: Optional[int] = None) -> FunnelDiagnosis:
    """단계별 전환율을 기준선과 대조해 병목을 지목한다.

    benchmarks 는 {"S1": 0.35, "S2": 0.6, ...} 형태이며 계약 시 확정된
    기준선(제안서 10.4)을 넘긴다. 없으면 병목을 판정하지 않는다.
    """
    d = FunnelDiagnosis()
    verdicts = verdicts or []

    for code, name, num_f, den_f in STAGES:
        num, den = snap.get(num_f), snap.get(den_f)
        rate = num / den if den > 0 else None
        note = ""
        if den > 0 and den < MIN_DENOMINATOR:
            rate, note = None, f"분모 {den}건(<{MIN_DENOMINATOR}) — 판정 유보"
        elif den == 0:
            note = "분모 0건 — 측정 불가"
        d.stages.append(StageResult(
            code, name, num, den, rate,
            (benchmarks or {}).get(code), note))

    measurable = [s for s in d.stages if s.measurable]
    if not benchmarks:
        d.verdict = ("기준선 미설정 — 병목을 판정하지 않습니다. "
                     "전환율 20%가 좋은지 나쁜지는 기준선 없이 말할 수 없습니다")
        d.limitations.append(
            "계약 시 확정한 기준선(제안서 10.4)을 입력하면 병목 판정이 활성화됩니다")
    elif not measurable:
        d.verdict = "측정 가능한 단계 없음 — 표본 누적 필요"
    else:
        short = [s for s in measurable if s.shortfall]
        if not short:
            d.verdict = "기준선을 크게 밑도는 단계 없음 — 특정 병목이 지목되지 않습니다"
        else:
            d.bottleneck = min(short, key=lambda s: s.gap)
            d.cause, d.action = DIAGNOSIS[d.bottleneck.code]
            d.is_condition_issue = d.bottleneck.code == CONDITION_STAGE
            d.verdict = (f"{d.bottleneck.name} 단계가 기준선 대비 "
                         f"{d.bottleneck.gap:+.0%}로 가장 크게 미달")
            if len(short) > 1:
                others = ", ".join(s.name for s in short if s is not d.bottleneck)
                d.limitations.append(
                    f"미달 단계가 복수입니다({others}) — 상류 단계가 개선되면 "
                    "하류 병목의 크기도 달라질 수 있습니다")

    d.corroboration = _corroborate(d.bottleneck, feedback, verdicts)

    # 표 10-7 — 운영 점검 항목
    if snap.leads and snap.contact_delayed:
        share = snap.contact_delayed / snap.leads
        if share >= CONTACT_DELAY_ALERT:
            label = f"기준시간({contact_sla_hours}시간) 초과" if contact_sla_hours \
                else "연락 기준시간 초과"
            d.operational_flags.append(
                f"{label} {share:.0%} — 광고가 아닌 운영 병목으로 분류. "
                "배정·알림·예외 처리 규칙 개선 대상")
    if snap.followups_sent and snap.visited is not None and snap.consulted:
        per = snap.followups_sent / max(1, snap.consulted)
        if per >= 3 and (snap.visited / snap.consulted) < 0.3:
            d.operational_flags.append(
                f"상담 1건당 후속 발송 {per:.1f}회인데 방문 전환은 "
                f"{snap.visited / snap.consulted:.0%} — 대상·시점·메시지의 "
                "상태 불일치 점검 (발송량 증가로 해결되지 않음)")

    cpc = snap.cost_per_contract
    if cpc is not None and snap.cost_per_lead is not None:
        d.limitations.append(
            f"DB 단가 {snap.cost_per_lead/1e4:,.0f}만원 · 계약당 비용 "
            f"{cpc/1e4:,.0f}만원 — DB 단가만으로 매체를 감액하지 않고 "
            "계약당 비용으로 판단합니다 (표 10-6)")
    d.limitations.append(
        "퍼널 수치는 원인을 지목하지 않고 위치만 지목합니다. 원인 확정에는 "
        "거절 사유·상담 기록과의 대조가 필요합니다 [LIMITATION]")
    return d
