"""커버리지표 자동 생성 (P2-4, 제안서 5.10.1).

계약 전 제출용: 15개 레이어별로 '확보 가능 · 조건부 · 미확보 · 대체 가능'을
현재 수집 상태로부터 자동 판정한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Coverage(str, Enum):
    AVAILABLE = "확보 가능"
    CONDITIONAL = "조건부"
    MISSING = "미확보"
    SUBSTITUTE = "대체 가능"


@dataclass
class LayerCoverage:
    layer: str
    coverage: Coverage
    source: str
    note: str


LAYERS = [
    ("L1 상주인구·인구구조", "SGIS 격자·집계구"),
    ("L2 가구·주거수요", "SGIS 가구 통계"),
    ("L3 인구이동", "통계청 인구이동"),
    ("L4 사업체·고용", "SGIS 사업체"),
    ("L5 소득·구매력", "공공 대체 근사"),
    ("L6 유동인구", "민간 라이선스"),
    ("L7 생활이동·O/D", "KTDB"),
    ("L8 교통망·접근성", "대중교통 API"),
    ("L9 상권 활동", "소상공인 상권정보"),
    ("L10 카드소비", "민간 라이선스"),
    ("L11 매매·전월세 실거래", "국토부 실거래 (E01)"),
    ("L12 청약·미분양", "청약홈 (E02)"),
    ("L13 공급 파이프라인", "설정 입력 + 인허가 통계"),
    ("L14 개발계획·공공투자", "설정 입력 + 고시·예산 원문"),
    ("L15 도시계획·토지이용", "토지이음 고시"),
]


def build(*, tx_count: int, sub_count: int, supply_items: int,
          catalyst_items: int, income_model: bool,
          listings_connected: bool = False,
          region_stats_connected: bool = False,
          commerce_connected: bool = False,
          unsold_connected: bool = False,
          migration_connected: bool = False,
          mobility_connected: bool = False,
          transit_connected: bool = False,
          rent_connected: bool = False) -> list[LayerCoverage]:
    """현재 파이프라인의 실제 수집 상태로 커버리지표를 생성한다."""
    out: list[LayerCoverage] = []
    for layer, source in LAYERS:
        cov, note = Coverage.MISSING, "커넥터 미구현"

        if layer.startswith("L11"):
            cov = Coverage.AVAILABLE if tx_count else Coverage.MISSING
            note = f"매매 {tx_count}건" if tx_count else "수집 0건 — 설정 확인 필요"
            note += " · 전월세 연동됨(전세가율 산출)" if rent_connected \
                else " · 전월세 미수집 — 전세 기반 하방 점검 불가"
        elif layer.startswith("L12"):
            cov = Coverage.AVAILABLE if sub_count else Coverage.MISSING
            note = (f"수집 {sub_count}건 — 가격 갭·동시 공급 미제공"
                    if sub_count else "수집 0건")
            if unsold_connected:
                note += " · 미분양 시계열 연동됨"
        elif layer.startswith("L13"):
            cov = Coverage.CONDITIONAL if supply_items else Coverage.MISSING
            note = f"설정 입력 {supply_items}건 — 인허가 통계 연동 시 자동화 가능"
        elif layer.startswith("L14") or layer.startswith("L15"):
            cov = Coverage.CONDITIONAL if catalyst_items else Coverage.MISSING
            note = f"설정 입력 {catalyst_items}건 — 원문 링크 수기 등록"
        elif layer.startswith("L5"):
            cov = Coverage.SUBSTITUTE if income_model else Coverage.MISSING
            note = "로그정규 근사 — 실측 소득 데이터로 교체 권고 [LIMITATION]"
        elif layer.startswith(("L6", "L10")):
            cov, note = Coverage.MISSING, "민간 라이선스 별도 협의 대상"
        elif layer.startswith(("L1 ", "L2", "L4")):
            if region_stats_connected:
                cov = Coverage.CONDITIONAL
                note = "SGIS 시군구 단위 — 생활권 격자 권한 확보 시 상향"
            else:
                cov, note = Coverage.MISSING, "SGIS 인증·행정구역 코드 미지정"
        elif layer.startswith("L9"):
            cov = Coverage.AVAILABLE if commerce_connected else Coverage.MISSING
            note = ("반경 내 업소 수집" if commerce_connected
                    else "commerce_radius_m 미지정")
        elif layer.startswith("L3"):
            if migration_connected:
                cov = Coverage.AVAILABLE
                note = "전입·전출·순이동 수집 — 유입 출발지는 자료 제공 시 집계"
            else:
                cov, note = Coverage.MISSING, "KOSIS 표 코드 또는 migration_file 미지정"
        elif layer.startswith("L7"):
            if mobility_connected:
                cov = Coverage.CONDITIONAL
                note = "O/D 파일 적재 — 계약·승인 자료로 갱신 주기가 김"
            else:
                cov, note = Coverage.MISSING, "KTDB·통신사 O/D 계약 필요 (mobility_file)"
        elif layer.startswith("L8"):
            if transit_connected:
                cov = Coverage.AVAILABLE
                note = "정류장(TAGO)·역 좌표 기반 도보 시간 산출 — 배차·환승 미반영"
            else:
                cov, note = Coverage.MISSING, "transit_radius_m·역 좌표 파일 미지정"

        if layer.startswith("L11") and listings_connected:
            note += " · 매물·호가 선행 신호 연동됨"
        out.append(LayerCoverage(layer, cov, source, note))
    return out


def as_markdown(rows: list[LayerCoverage]) -> str:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.coverage.value] = counts.get(r.coverage.value, 0) + 1
    head = " · ".join(f"{k} {v}" for k, v in counts.items())
    L = [f"*레이어 {len(rows)}개 — {head}*", "",
         "| 레이어 | 판정 | 출처 | 비고 |", "|--------|------|------|------|"]
    L += [f"| {r.layer} | {r.coverage.value} | {r.source} | {r.note} |" for r in rows]
    L += ["", "*미확보 레이어의 분석 주장은 리포트에서 제외되거나 대체 근거와 한계가 병기됩니다.*"]
    return "\n".join(L)
