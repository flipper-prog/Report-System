"""E01-R — 국토교통부 아파트 전월세 실거래가 커넥터 (L11 완성).

엔드포인트: apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent
파라미터: serviceKey, LAWD_CD(법정동 5자리), DEAL_YMD(YYYYMM), pageNo, numOfRows

매매만으로는 가격의 **하방**을 볼 수 없다. 전세는 실거주 수요가 직접 지불하는
금액이므로 투자 기대가 섞이지 않으며, 전세가율이 높을수록 매매가의 하방 지지가
두텁다. 반대로 전세가율이 낮으면 가격 하락 시 완충 구간이 얇다.

매매 커넥터(molit)와 동일하게 신형(영문 태그)·구형(한글 태그)을 모두 파싱한다.
갱신 계약은 계약구분 필드로 식별해 분리 적재한다 — 갱신은 상한제·갱신요구권의
영향을 받아 신규 체결가와 다른 가격 논리를 따르기 때문이다.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date

from ..models import RentRecord
from .base import Fetcher

URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent"
SOURCE = "국토교통부 아파트 전월세 실거래가 OpenAPI (E01-R)"
PAGE_SIZE = 500

F = {
    "apt": ["aptNm", "아파트"],
    "deposit": ["deposit", "보증금액", "보증금"],
    "rent": ["monthlyRent", "월세금액", "월세"],
    "area": ["excluUseAr", "전용면적"],
    "floor": ["floor", "층"],
    "year": ["dealYear", "년"],
    "month": ["dealMonth", "월"],
    "day": ["dealDay", "일"],
    "build": ["buildYear", "건축년도"],
    "umd": ["umdNm", "법정동"],
    "contract": ["contractType", "계약구분"],
}

#: 계약구분 값 중 갱신을 뜻하는 표기
RENEWAL_TOKENS = ("갱신",)


class RentApiError(RuntimeError):
    pass


@dataclass
class RawRent:
    apt_nm: str
    umd: str
    deal_date: date
    area_m2: float
    floor: int
    deposit_won: int
    monthly_rent_won: int
    build_year: int
    renewal: bool

    @property
    def is_jeonse(self) -> bool:
        return self.monthly_rent_won <= 0


def _text(item: ET.Element, keys: list[str]) -> str:
    for k in keys:
        el = item.find(k)
        if el is not None and el.text:
            return el.text.strip()
    return ""


def _won(raw: str) -> int:
    """응답 단위는 만원. 빈 값·구분자·공백을 흡수한다."""
    s = raw.replace(",", "").strip()
    if not s:
        return 0
    return int(float(s)) * 10_000


def parse_response(xml_bytes: bytes) -> tuple[list[RawRent], int]:
    """(전월세 목록, totalCount). 오류 응답은 예외."""
    root = ET.fromstring(xml_bytes)

    err = root.find(".//returnAuthMsg")
    if err is not None and err.text:
        code = root.findtext(".//returnReasonCode", "")
        raise RentApiError(f"API 오류 [{code}] {err.text}")

    rc = root.findtext(".//resultCode", "")
    if rc not in ("00", "000", ""):
        raise RentApiError(f"API resultCode={rc}: {root.findtext('.//resultMsg', '')}")

    total = int(root.findtext(".//totalCount", "0") or 0)
    out: list[RawRent] = []
    for item in root.iter("item"):
        deposit = _won(_text(item, F["deposit"]))
        if deposit <= 0:
            continue                      # 보증금 없는 순수 월세는 비교 기준이 없음
        y = int(_text(item, F["year"]) or 0)
        m = int(_text(item, F["month"]) or 0)
        d = int(_text(item, F["day"]) or 1)
        if not (y and m):
            continue
        contract = _text(item, F["contract"])
        out.append(RawRent(
            apt_nm=_text(item, F["apt"]),
            umd=_text(item, F["umd"]),
            deal_date=date(y, m, d),
            area_m2=float(_text(item, F["area"]) or 0),
            floor=int(_text(item, F["floor"]) or 0),
            deposit_won=deposit,
            monthly_rent_won=_won(_text(item, F["rent"])),
            build_year=int(_text(item, F["build"]) or 0),
            renewal=any(t in contract for t in RENEWAL_TOKENS)))
    return out, total


def fetch_month(fetcher: Fetcher, key: str, lawd_cd: str,
                yyyymm: str) -> list[RawRent]:
    rows: list[RawRent] = []
    page = 1
    while True:
        body = fetcher.get(SOURCE, URL, {
            "serviceKey": key, "LAWD_CD": lawd_cd, "DEAL_YMD": yyyymm,
            "pageNo": page, "numOfRows": PAGE_SIZE})
        got, total = parse_response(body)
        rows.extend(got)
        if page * PAGE_SIZE >= total or not got:
            return rows
        page += 1


def fetch_range(fetcher: Fetcher, key: str, lawd_cd: str,
                asof: date, months: int) -> list[RawRent]:
    """asof가 속한 달부터 과거 months개월 수집 (미래 정보 누출 없음)."""
    rows: list[RawRent] = []
    y, m = asof.year, asof.month
    for _ in range(months):
        rows.extend(fetch_month(fetcher, key, lawd_cd, f"{y}{m:02d}"))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return [r for r in rows if r.deal_date <= asof]


def to_records(raws: list[RawRent],
               apt_to_complex: dict[str, str]) -> list[RentRecord]:
    """설정에 등록된 비교단지의 전월세 거래만 적재한다."""
    out: list[RentRecord] = []
    for r in raws:
        cid = apt_to_complex.get(r.apt_nm)
        if cid is None or r.area_m2 <= 0:
            continue
        out.append(RentRecord(
            complex_id=cid, deal_date=r.deal_date, area_m2=r.area_m2,
            floor=r.floor, deposit=r.deposit_won,
            monthly_rent=r.monthly_rent_won, renewal=r.renewal))
    return out
