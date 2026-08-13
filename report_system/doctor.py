"""사전 점검 (doctor) — 실데이터 실행 전 설정·연결·데이터 가용성 진단.

키 수령 직후 `live` 를 바로 돌리기 전에 이 명령으로 다음을 확인한다.
  1) 설정 파일의 필수 키·타입·값 범위
  2) 선택 레이어 설정·파일 존재 (어떤 레이어가 활성화되는지)
  3) 인증키 존재 및 두 API의 실제 응답 (소량 호출)
  4) 대상 지역의 실제 단지명 목록 → comparables 이름 교정

네트워크 호출은 각 API당 1회로 제한한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .connectors import applyhome, molit, rent
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


#: 선택 레이어 — (설정 키, 표시명, 파일 여부)
OPTIONAL_LAYERS = [
    ("listings_file", "매물·호가 선행 신호", True),
    ("sgis_adm_cd", "L1·L2·L4 인구·가구·사업체 (SGIS)", False),
    ("migration_file", "L3 인구이동 (파일)", True),
    ("kosis_migration", "L3 인구이동 (KOSIS API)", False),
    ("mobility_file", "L7 생활이동·O/D", True),
    ("stations_file", "L8 역 좌표", True),
    ("transit_radius_m", "L8 정류장 수집 반경", False),
    ("commerce_radius_m", "L9 상권 수집 반경", False),
    ("unsold_file", "L12 미분양", True),
    ("housing_file", "L13 주택건설실적 (파일)", True),
    ("kosis_housing", "L13 주택건설실적 (KOSIS API)", False),
]


def check_layers(cfg: dict[str, Any]) -> list[Check]:
    """선택 레이어의 설정 여부와 파일 존재를 확인한다.

    미지정은 실패가 아니라 '미수집'이다 — 리포트 커버리지표에 그대로 표기된다.
    지정했는데 파일이 없는 경우만 실패로 본다.
    """
    import pathlib as _pl

    out: list[Check] = []
    # L3는 KOSIS API·파일 중 하나만 있으면 되므로, 다른 경로가 잡혀 있으면 묻지 않는다
    l3_alt = {"migration_file": "kosis_migration",
              "kosis_migration": "migration_file",
              "housing_file": "kosis_housing",
              "kosis_housing": "housing_file"}
    for key, label, is_file in OPTIONAL_LAYERS:
        val = cfg.get(key)
        if not val:
            if key in l3_alt and cfg.get(l3_alt[key]):
                continue
            out.append(Check(label, WARN, f"`{key}` 미지정 — 해당 레이어 미수집"))
            continue
        if is_file and not _pl.Path(str(val)).exists():
            out.append(Check(label, FAIL, f"파일 없음: {val}"))
        else:
            out.append(Check(label, OK, f"{key} = {val}"))

    if cfg.get("mobility_file") and not (cfg.get("mobility_focus")
                                         or cfg.get("site", {}).get("region")):
        out.append(Check("L7 focus 지역", FAIL,
                         "mobility_focus 또는 site.region 필요 — 집계 기준 지역 미상"))
    if cfg.get("kosis_migration"):
        spec = cfg["kosis_migration"]
        missing = [k for k in ("item_in", "item_out") if not spec.get(k)]
        if missing:
            out.append(Check("L3 KOSIS 항목 코드", FAIL,
                             f"kosis_migration.{'/'.join(missing)} 누락"))
    if (cfg.get("migration_file") or cfg.get("kosis_migration")) \
            and not cfg.get("region_population"):
        out.append(Check("L3 기준 인구", WARN,
                         "`region_population` 미지정 — 순이동률 대신 비중으로 판정"))
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

    if cfg.get("collect_rent", True):
        try:
            body = f.get(rent.SOURCE, rent.URL, {
                "serviceKey": key, "LAWD_CD": str(cfg["lawd_cd"]),
                "DEAL_YMD": f"{probe.year}{probe.month:02d}",
                "pageNo": 1, "numOfRows": 100})
            rrows, rtotal = rent.parse_response(body)
            n_j = sum(1 for r in rrows if r.is_jeonse)
            checks.append(Check("E01-R 전월세 API", OK if rrows else WARN,
                                f"{probe.year}-{probe.month:02d} 응답 {len(rrows)}건 "
                                f"(전세 {n_j}건 / 총 {rtotal}건)"))
        except Exception as e:  # noqa: BLE001
            checks.append(Check("E01-R 전월세 API", FAIL, f"{type(e).__name__}: {e}"))

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
