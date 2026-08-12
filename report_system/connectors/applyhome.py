"""E02 — 한국부동산원 청약홈 커넥터 (odcloud JSON API).

- 분양정보 상세: ApplyhomeInfoDetailSvc/v1/getAPTLttotPblancDetail
- 타입별 경쟁률: ApplyhomeInfoCmpetRtSvc/v1/getAPTLttotPblancCmpet

두 응답을 (HOUSE_MANAGE_NO, PBLANC_NO)로 결합해 SubscriptionRecord를 만든다.
가격 갭·동시 공급은 이 API가 제공하지 않으므로 None으로 적재되며,
청약 전망 모듈은 None 조건을 매칭 기준에서 제외한다(LIMITATION으로 기록).
"""
from __future__ import annotations

import json
from datetime import date, datetime

from ..models import SubscriptionRecord
from .base import Fetcher

DETAIL_URL = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1/getAPTLttotPblancDetail"
CMPET_URL = "https://api.odcloud.kr/api/ApplyhomeInfoCmpetRtSvc/v1/getAPTLttotPblancCmpet"
SOURCE_DETAIL = "청약홈 APT 분양정보 상세 (E02)"
SOURCE_CMPET = "청약홈 APT 타입별 경쟁률 (E02)"
PER_PAGE = 500

# 필드 후보(스키마 방어적 해석)
K_MANAGE = ["HOUSE_MANAGE_NO", "houseManageNo"]
K_PBLANC = ["PBLANC_NO", "pblancNo"]
K_NAME = ["HOUSE_NM", "houseNm"]
K_AREA_NM = ["SUBSCRPT_AREA_CODE_NM", "subscrptAreaCodeNm"]
K_RCEPT = ["RCEPT_BGNDE", "RCRIT_PBLANC_DE", "rceptBgnde"]
K_TYPE = ["HOUSE_TY", "houseTy", "MODEL_NO"]
K_SUPLY = ["SUPLY_HSHLDCO", "suplyHshldco"]
K_REQ = ["REQ_CNT", "reqCnt", "SUBSCRPT_REQ_CNT"]
K_RATE = ["CMPET_RATE", "cmpetRate"]


class ApplyhomeApiError(RuntimeError):
    pass


def _pick(row: dict, keys: list[str]):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def _parse_json(body: bytes, source: str) -> list[dict]:
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ApplyhomeApiError(f"{source}: JSON 해석 실패 — {e}") from e
    if isinstance(doc, dict) and "data" in doc:
        return list(doc["data"])
    raise ApplyhomeApiError(f"{source}: 예상 밖 응답 구조 {list(doc)[:5]}")


def _fetch_all(fetcher: Fetcher, key: str, url: str, source: str,
               extra: dict | None = None) -> list[dict]:
    rows: list[dict] = []
    page = 1
    while True:
        params = {"page": page, "perPage": PER_PAGE, "serviceKey": key}
        if extra:
            params.update(extra)
        got = _parse_json(fetcher.get(source, url, params), source)
        rows.extend(got)
        if len(got) < PER_PAGE:
            return rows
        page += 1


def _parse_rate(raw) -> tuple[float | None, bool]:
    """경쟁률 문자열 → (rate, 미달 여부). 예: '12.5' / '5.32:1' / '(△3)' / '-'"""
    if raw is None:
        return None, False
    s = str(raw).strip()
    shortfall = ("△" in s) or ("미달" in s)
    s = (s.replace(":1", "").replace("△", "").replace("(", "")
         .replace(")", "").replace(",", "").replace("미달", "").strip())
    try:
        rate = float(s)
    except ValueError:
        return None, shortfall
    # '(△n)' 형식은 미달 세대 수 표기 → 경쟁률 1 미만으로 간주
    return (rate, True) if shortfall else (rate, rate < 1.0)


def _parse_date(raw) -> date | None:
    if not raw:
        return None
    s = str(raw).replace("-", "").replace(".", "")[:8]
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return None


def fetch_subscription_history(
    fetcher: Fetcher, key: str,
    region_names: list[str],
    since: date, until: date,
) -> list[SubscriptionRecord]:
    """지역명(예: '서울', '경기') 필터로 청약 이력을 수집·결합한다."""
    details = _fetch_all(fetcher, key, DETAIL_URL, SOURCE_DETAIL)
    cmpets = _fetch_all(fetcher, key, CMPET_URL, SOURCE_CMPET)

    detail_by_key: dict[tuple, dict] = {}
    for d in details:
        mk = (_pick(d, K_MANAGE), _pick(d, K_PBLANC))
        if mk[0] is not None:
            detail_by_key[mk] = d

    out: list[SubscriptionRecord] = []
    for c in cmpets:
        mk = (_pick(c, K_MANAGE), _pick(c, K_PBLANC))
        d = detail_by_key.get(mk)
        if d is None:
            continue
        region = str(_pick(d, K_AREA_NM) or "")
        if region_names and not any(r in region for r in region_names):
            continue
        open_date = _parse_date(_pick(d, K_RCEPT))
        if open_date is None or not (since <= open_date <= until):
            continue
        units = int(_pick(c, K_SUPLY) or 0)
        if units <= 0:
            continue
        rate, shortfall = _parse_rate(_pick(c, K_RATE))
        req = _pick(c, K_REQ)
        applicants = int(req) if req is not None else (
            int(round(units * rate)) if rate is not None else 0)
        if applicants == 0 and rate is None:
            continue
        out.append(SubscriptionRecord(
            complex_id=f"{mk[0]}-{_pick(c, K_TYPE) or 'ALL'}",
            open_date=open_date,
            units=units,
            applicants=applicants,
            region=region,
            price_gap_pct=None,        # API 미제공 → 매칭 기준에서 제외 (LIMITATION)
            concurrent_supply=None,    # API 미제공 → 매칭 기준에서 제외 (LIMITATION)
            sold_out_in_order=not shortfall,
        ))
    return out
