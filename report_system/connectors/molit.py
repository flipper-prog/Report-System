"""E01 — 국토교통부 아파트 매매 실거래가 커넥터.

엔드포인트: apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade
파라미터: serviceKey, LAWD_CD(법정동 5자리), DEAL_YMD(YYYYMM), pageNo, numOfRows

파싱은 신형(영문 태그: aptNm, dealAmount, excluUseAr, floor, dealYear...)과
구형(한글 태그: 아파트, 거래금액, 전용면적, 층, 년...)을 모두 지원한다.
해제 거래(cdealType='O' / 해제여부='O')는 canceled=True 로 적재되어
정제 단계(transactions.clean)에서 제거·집계된다.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date

from ..models import Comparable, Transaction
from .base import Fetcher

URL = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
SOURCE = "국토교통부 아파트 매매 실거래가 OpenAPI (E01)"
PAGE_SIZE = 500

# 신형/구형 태그 후보
F = {
    "apt": ["aptNm", "아파트"],
    "amount": ["dealAmount", "거래금액"],
    "area": ["excluUseAr", "전용면적"],
    "floor": ["floor", "층"],
    "year": ["dealYear", "년"],
    "month": ["dealMonth", "월"],
    "day": ["dealDay", "일"],
    "build": ["buildYear", "건축년도"],
    "umd": ["umdNm", "법정동"],
    "cancel": ["cdealType", "해제여부"],
}


class MolitApiError(RuntimeError):
    pass


@dataclass
class RawTrade:
    apt_nm: str
    umd: str
    deal_date: date
    area_m2: float
    floor: int
    price_won: int
    build_year: int
    canceled: bool


def _text(item: ET.Element, keys: list[str]) -> str:
    for k in keys:
        el = item.find(k)
        if el is not None and el.text:
            return el.text.strip()
    return ""


def parse_response(xml_bytes: bytes) -> tuple[list[RawTrade], int]:
    """(거래 목록, totalCount). 오류 응답은 예외."""
    root = ET.fromstring(xml_bytes)

    # 공공데이터포털 공통 오류 포맷
    err = root.find(".//returnAuthMsg")
    if err is not None and err.text:
        code = root.findtext(".//returnReasonCode", "")
        raise MolitApiError(f"API 오류 [{code}] {err.text}")

    rc = root.findtext(".//resultCode", "")
    if rc not in ("00", "000", ""):
        raise MolitApiError(f"API resultCode={rc}: {root.findtext('.//resultMsg', '')}")

    total = int(root.findtext(".//totalCount", "0") or 0)
    out: list[RawTrade] = []
    for item in root.iter("item"):
        amount_s = _text(item, F["amount"]).replace(",", "").strip()
        if not amount_s:
            continue
        y = int(_text(item, F["year"]) or 0)
        m = int(_text(item, F["month"]) or 0)
        d = int(_text(item, F["day"]) or 1)
        out.append(RawTrade(
            apt_nm=_text(item, F["apt"]),
            umd=_text(item, F["umd"]),
            deal_date=date(y, m, d),
            area_m2=float(_text(item, F["area"]) or 0),
            floor=int(_text(item, F["floor"]) or 0),
            price_won=int(amount_s) * 10_000,          # 응답 단위: 만원
            build_year=int(_text(item, F["build"]) or 0),
            canceled=_text(item, F["cancel"]).upper() == "O",
        ))
    return out, total


def fetch_month(fetcher: Fetcher, key: str, lawd_cd: str, yyyymm: str) -> list[RawTrade]:
    rows: list[RawTrade] = []
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
                asof: date, months: int) -> list[RawTrade]:
    """asof가 속한 달부터 과거 months개월 수집 (미래 정보 누출 없음)."""
    rows: list[RawTrade] = []
    y, m = asof.year, asof.month
    for _ in range(months):
        rows.extend(fetch_month(fetcher, key, lawd_cd, f"{y}{m:02d}"))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return [r for r in rows if r.deal_date <= asof]


# ── 도메인 모델 변환 ─────────────────────────────────────────────────────────

def to_transactions(raws: list[RawTrade],
                    apt_to_complex: dict[str, str]) -> list[Transaction]:
    """설정에 등록된 비교단지(아파트명 → complex_id)의 거래만 적재."""
    out = []
    for r in raws:
        cid = apt_to_complex.get(r.apt_nm)
        if cid is None:
            continue
        out.append(Transaction(
            complex_id=cid, trade_date=r.deal_date, area_m2=r.area_m2,
            floor=r.floor, price=r.price_won, canceled=r.canceled))
    return out


def build_comparables(raws: list[RawTrade],
                      wanted: list[dict]) -> dict[str, Comparable]:
    """설정의 비교단지 목록에 실데이터의 건축년도를 결합해 Comparable 생성.

    wanted 원소: {"apt_nm": str, "dist_m": float,
                  "presale": bool(선택), "brand_tier": int(선택)}
    """
    build_by_name: dict[str, int] = {}
    for r in raws:
        if r.build_year:
            build_by_name.setdefault(r.apt_nm, r.build_year)

    comps: dict[str, Comparable] = {}
    for i, w in enumerate(wanted):
        name = w["apt_nm"]
        cid = f"C{i:02d}"
        comps[cid] = Comparable(
            id=cid, name=name,
            built_year=build_by_name.get(name, 0) or int(w.get("build_year", 0)),
            units=int(w.get("units", 0)),
            brand_tier=int(w.get("brand_tier", 2)),
            dist_m=float(w["dist_m"]),
            region=str(w.get("region", "")),
            is_presale_right=bool(w.get("presale", False)))
    return comps
