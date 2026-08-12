"""사전 점검 (doctor) — 실데이터 실행 전 설정·연결·데이터 가용성 진단.

키 수령 직후 `live` 를 바로 돌리기 전에 이 명령으로 다음을 확인한다.
  1) 설정 파일의 필수 키·타입·값 범위
  2) 인증키 존재 및 두 API의 실제 응답 (소량 호출)
  3) 대상 지역의 실제 단지명 목록 → comparables 이름 교정
  4) 예상 커버리지 (어떤 레이어가 활성화되는지)

네트워크 호출은 각 API당 1회로 제한한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .connectors import applyhome, molit
from .connectors.base import Fetcher, MissingApiKeyError, api_key

OK, WARN, FAIL = "OK", "주의", "실패"


@dataclass
class Check:
    name: str
    status: str
    detail: str

    def line(self) -> str:
        mark = {OK: "✓", WARN: "!", FAIL: "✗"}[self.status]
        return f" {mark} [{self.status}] {self.name} — {self.detail}"


REQUIRED = {
    "asof": str, "lawd_cd": str, "site": dict, "comparables": list,
}
SITE_REQUIRED = {
    "id": str, "name": str, "address": str, "lat": (int, float),
    "lng": (int, float), "total_units": int, "types": list,
}
TYPE_REQUIRED = {"name": str, "area_m2": (int, float), "units": int,
                 "base_price": int}


def check_config(cfg: dict[str, Any]) -> list[Check]:
    out: list[Check] = []

    for k, t in REQUIRED.items():
        if k not in cfg:
            out.append(Check(f"설정 키 `{k}`", FAIL, "누락"))
        elif not isinstance(cfg[k], t):
            out.append(Check(f"설정 키 `{k}`", FAIL, f"타입 불일치 (기대 {t.__name__})"))
    if any(c.status == FAIL for c in out):
        return out

    lawd = str(cfg["lawd_cd"])
    out.append(Check("법정동 코드", OK if (lawd.isdigit() and len(lawd) == 5) else FAIL,
                     f"{lawd} ({'5자리 숫자' if lawd.isdigit() and len(lawd)==5 else '5자리 숫자여야 함'})"))

    try:
        asof = date.fromisoformat(cfg["asof"])
        out.append(Check("분석 기준일", OK if asof <= date.today() else WARN,
                         f"{asof}{'' if asof <= date.today() else ' — 미래 날짜'}"))
    except ValueError:
        out.append(Check("분석 기준일", FAIL, "YYYY-MM-DD 형식이 아님"))

    site = cfg["site"]
    for k, t in SITE_REQUIRED.items():
        if k not in site:
            out.append(Check(f"site.{k}", FAIL, "누락"))
        elif not isinstance(site[k], t):
            out.append(Check(f"site.{k}", FAIL, "타입 불일치"))

    types = site.get("types", [])
    if types:
        unit_sum = sum(int(t.get("units", 0)) for t in types)
        total = int(site.get("total_units", 0))
        out.append(Check("세대수 정합", OK if unit_sum == total else FAIL,
                         f"타입 합 {unit_sum} vs 총 {total}"))
        for t in types:
            for k, tt in TYPE_REQUIRED.items():
                if k not in t or not isinstance(t[k], tt):
                    out.append(Check(f"types[{t.get('name','?')}].{k}", FAIL, "누락/타입 오류"))
            price = t.get("base_price", 0)
            if isinstance(price, int) and price < 10_000_000:
                out.append(Check(f"types[{t.get('name','?')}].base_price", FAIL,
                                 f"{price:,} — 단위 확인 필요(원 단위)"))

    comps = cfg["comparables"]
    out.append(Check("비교단지 수", OK if len(comps) >= 2 else WARN,
                     f"{len(comps)}개{'' if len(comps) >= 2 else ' — 2개 이상 권고'}"))
    if not any(c.get("units") for c in comps):
        out.append(Check("비교단지 units", WARN,
                         "미입력 — 환금성(회전율) 산출 불가"))

    if not cfg.get("supply"):
        out.append(Check("공급 파이프라인", WARN, "미입력 — 공급 판정 신뢰도 낮음"))
    if not cfg.get("catalysts"):
        out.append(Check("개발계획", WARN, "미입력 — 촉매 판정 '평가 대상 없음'"))
    return out


def check_apis(cfg: dict[str, Any], cache_dir: str = "out/cache") -> tuple[list[Check], list[str]]:
    """API 연결 확인. 반환: (체크 목록, 해당 지역 단지명 후보)."""
    checks: list[Check] = []
    names: list[str] = []
    try:
        key = api_key()
    except MissingApiKeyError as e:
        checks.append(Check("인증키", FAIL, str(e).split("\n")[0]))
        return checks, names

    checks.append(Check("인증키", OK, f"환경변수 설정됨 (길이 {len(key)})"))
    f = Fetcher(cache_dir=cache_dir)

    asof = date.fromisoformat(cfg["asof"])
    probe = asof.replace(day=1) - timedelta(days=1)   # 직전 월 (자료 확정 가능성 높음)
    try:
        body = f.get(molit.SOURCE, molit.URL, {
            "serviceKey": key, "LAWD_CD": str(cfg["lawd_cd"]),
            "DEAL_YMD": f"{probe.year}{probe.month:02d}",
            "pageNo": 1, "numOfRows": 100})
        rows, total = molit.parse_response(body)
        names = sorted({r.apt_nm for r in rows})
        checks.append(Check("E01 실거래 API", OK if rows else WARN,
                            f"{probe.year}-{probe.month:02d} 응답 {len(rows)}건 (총 {total}건)"))
    except Exception as e:  # noqa: BLE001 — 진단 목적상 모든 예외를 표시
        checks.append(Check("E01 실거래 API", FAIL, f"{type(e).__name__}: {e}"))

    try:
        rows = applyhome._fetch_all(  # noqa: SLF001 — 진단용 소량 호출
            f, key, applyhome.DETAIL_URL, applyhome.SOURCE_DETAIL)
        checks.append(Check("E02 청약 API", OK if rows else WARN,
                            f"분양정보 {len(rows)}건 수신"))
    except Exception as e:  # noqa: BLE001
        checks.append(Check("E02 청약 API", FAIL, f"{type(e).__name__}: {e}"))

    return checks, names


def match_comparables(cfg: dict[str, Any], names: list[str]) -> list[Check]:
    """설정의 비교단지명이 실데이터에 존재하는지 확인하고 유사 후보를 제안."""
    if not names:
        return [Check("비교단지 매칭", WARN, "단지명 목록을 받지 못해 확인 불가")]

    out: list[Check] = []
    for c in cfg["comparables"]:
        want = str(c.get("apt_nm", ""))
        if want in names:
            out.append(Check(f"비교단지 `{want}`", OK, "실데이터에서 확인됨"))
            continue
        cand = [n for n in names if want and (want in n or n in want)]
        if not cand:
            core = want[:2]
            cand = [n for n in names if core and core in n][:5]
        hint = f" 후보: {', '.join(cand[:5])}" if cand else " 후보 없음"
        out.append(Check(f"비교단지 `{want}`", FAIL, f"실데이터에 없음.{hint}"))
    return out


def summarize(checks: list[Check]) -> tuple[int, int, int]:
    return (sum(c.status == OK for c in checks),
            sum(c.status == WARN for c in checks),
            sum(c.status == FAIL for c in checks))
