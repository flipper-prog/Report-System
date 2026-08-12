"""표현 통제 — 5등급 주장 관리와 광고 린트 게이트 (제안서 5.9, 14.5 / P2-5).

규칙
- 금지 표현(보장·단정·무근거 최상급)은 등급과 무관하게 차단한다.
- FORECAST 주장은 광고 '사용 가능' 등급을 가질 수 없고(조건부가 상한),
  조건·범위 표지가 문장에 있어야 한다.
- FACT/CALCULATION 외 등급은 근거 없이 '사용 가능'이 될 수 없다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import AdGrade, Claim, ClaimGrade

BANNED_PATTERNS = [
    r"무조건", r"확정\s*수익", r"수익\s*보장", r"가격\s*보장", r"상승\s*보장",
    r"100\s*%", r"국내\s*최초", r"AI\s*추천\s*1위", r"제일\s*싸", r"유일한\s*기회",
    r"신설\s*확정",   # 고시·개통 확인 전 사용 금지
]

CONDITION_MARKERS = ["조건", "가정", "범위", "구간", "~", "수 있", "경우", "전제", "예상", "전망"]


@dataclass
class LintResult:
    passed: list[Claim] = field(default_factory=list)
    blocked: list[tuple[Claim, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocked


def lint(claims: list[Claim]) -> LintResult:
    res = LintResult()
    for c in claims:
        reason = _check(c)
        if reason:
            res.blocked.append((c, reason))
        else:
            res.passed.append(c)
    return res


def _check(c: Claim) -> str | None:
    for pat in BANNED_PATTERNS:
        if re.search(pat, c.text):
            return f"금지 표현 감지: /{pat}/"

    if c.grade == ClaimGrade.FORECAST:
        if c.ad_grade == AdGrade.ALLOWED:
            return "FORECAST는 광고 '사용 가능' 등급 불가 (조건부가 상한)"
        if not any(m in c.text for m in CONDITION_MARKERS):
            return "FORECAST 문장에 조건·범위 표지 없음"

    if c.ad_grade == AdGrade.ALLOWED:
        if c.grade not in (ClaimGrade.FACT, ClaimGrade.CALCULATION):
            return f"{c.grade.value} 등급은 근거 검증 없이 '사용 가능' 불가"
        if not c.evidence:
            return "'사용 가능' 등급에 근거 식별자 없음"

    return None
