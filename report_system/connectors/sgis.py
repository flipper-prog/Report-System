"""SGIS(통계지리정보서비스) 커넥터 — L1 상주인구 · L2 가구 · L4 사업체.

인증 방식이 다른 API와 다르다: consumerKey/consumerSecret 으로 accessToken 을
발급받아 호출한다. 따라서 별도 환경변수를 사용한다.
  SGIS_CONSUMER_KEY / SGIS_CONSUMER_SECRET

수집 단위는 행정구역 코드(adm_cd)이며, 본 커넥터는 시군구(5자리) 기준으로
연도별 시계열을 수집한다. 생활권(격자) 단위 집계는 별도 API 권한이 필요하므로
현 단계에서는 시군구 값을 사용하되 **공간 해상도 한계를 반드시 표기**한다
(제안서 5.4.1의 '시·구 통계를 생활권처럼 쓰지 않는다' 원칙).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from .base import Fetcher

AUTH_URL = "https://sgisapi.kostat.go.kr/OpenAPI3/auth/authentication.json"
POP_URL = "https://sgisapi.kostat.go.kr/OpenAPI3/stats/population.json"
HOUSE_URL = "https://sgisapi.kostat.go.kr/OpenAPI3/stats/household.json"
COMPANY_URL = "https://sgisapi.kostat.go.kr/OpenAPI3/stats/company.json"

SOURCE = "SGIS 통계지리정보 (L1·L2·L4)"
KEY_ENV = "SGIS_CONSUMER_KEY"
SECRET_ENV = "SGIS_CONSUMER_SECRET"

SPATIAL_LIMITATION = (
    "SGIS 집계 단위가 시군구이므로 현장 생활권보다 공간 해상도가 낮습니다. "
    "생활권 격자 집계 권한 확보 전까지 추세 판단용으로만 사용합니다 [LIMITATION]")


class SgisAuthError(RuntimeError):
    pass


class SgisApiError(RuntimeError):
    pass


@dataclass
class YearValue:
    year: int
    value: float


@dataclass
class RegionStats:
    adm_cd: str
    population: list[YearValue] = field(default_factory=list)
    households: list[YearValue] = field(default_factory=list)
    avg_household_size: list[YearValue] = field(default_factory=list)
    companies: list[YearValue] = field(default_factory=list)
    employees: list[YearValue] = field(default_factory=list)
    limitations: list[str] = field(default_factory=lambda: [SPATIAL_LIMITATION])

    def _cagr(self, series: list[YearValue]) -> Optional[float]:
        if len(series) < 2:
            return None
        first, last = series[0], series[-1]
        years = last.year - first.year
        if years <= 0 or first.value <= 0:
            return None
        return ((last.value / first.value) ** (1 / years) - 1) * 100

    @property
    def population_cagr(self) -> Optional[float]:
        return self._cagr(self.population)

    @property
    def household_cagr(self) -> Optional[float]:
        return self._cagr(self.households)

    @property
    def employee_cagr(self) -> Optional[float]:
        return self._cagr(self.employees)

    def summary(self) -> str:
        parts = []
        if self.population_cagr is not None:
            parts.append(f"인구 {self.population_cagr:+.1f}%/년")
        if self.household_cagr is not None:
            parts.append(f"가구 {self.household_cagr:+.1f}%/년")
        if self.employee_cagr is not None:
            parts.append(f"종사자 {self.employee_cagr:+.1f}%/년")
        return " · ".join(parts) if parts else "추세 산출 불가"


def credentials() -> tuple[str, str]:
    key = os.environ.get(KEY_ENV, "").strip()
    secret = os.environ.get(SECRET_ENV, "").strip()
    if not key or not secret:
        raise SgisAuthError(
            f"SGIS 인증 정보 없음 ({KEY_ENV}, {SECRET_ENV}).\n"
            "  https://sgis.kostat.go.kr/developer 에서 서비스 신청 후\n"
            "  서비스 ID(consumer_key)와 보안 Key(consumer_secret)를 export 하십시오.")
    return key, secret


def get_token(fetcher: Fetcher, key: str, secret: str) -> str:
    body = fetcher.get(SOURCE, AUTH_URL,
                       {"consumer_key": key, "consumer_secret": secret})
    doc = json.loads(body.decode("utf-8"))
    if str(doc.get("errCd", "0")) not in ("0", "00"):
        raise SgisAuthError(f"SGIS 인증 실패 [{doc.get('errCd')}] {doc.get('errMsg')}")
    token = (doc.get("result") or {}).get("accessToken")
    if not token:
        raise SgisAuthError("SGIS 응답에 accessToken 없음")
    return str(token)


def _fetch_stat(fetcher: Fetcher, url: str, token: str, adm_cd: str,
                year: int, extra: dict | None = None) -> list[dict]:
    params = {"accessToken": token, "year": str(year), "adm_cd": adm_cd,
              "low_search": "0"}
    if extra:
        params.update(extra)
    doc = json.loads(fetcher.get(SOURCE, url, params).decode("utf-8"))
    code = str(doc.get("errCd", "0"))
    if code not in ("0", "00"):
        # -100: 해당 연도 자료 없음 → 빈 결과로 처리
        if code in ("-100", "100"):
            return []
        raise SgisApiError(f"SGIS 오류 [{code}] {doc.get('errMsg')} ({url})")
    return list(doc.get("result") or [])


def _num(row: dict, *keys: str) -> Optional[float]:
    for k in keys:
        v = row.get(k)
        if v in (None, ""):
            continue
        try:
            return float(str(v).replace(",", ""))
        except ValueError:
            continue
    return None


def fetch_region_stats(fetcher: Fetcher, adm_cd: str, years: list[int],
                       token: Optional[str] = None) -> RegionStats:
    """시군구 코드(5자리)의 연도별 인구·가구·사업체 통계를 수집한다."""
    if token is None:
        token = get_token(fetcher, *credentials())

    stats = RegionStats(adm_cd=adm_cd)
    for y in sorted(years):
        for row in _fetch_stat(fetcher, POP_URL, token, adm_cd, y):
            v = _num(row, "population", "tot_ppltn")
            if v is not None:
                stats.population.append(YearValue(y, v))
                break
        for row in _fetch_stat(fetcher, HOUSE_URL, token, adm_cd, y):
            hh = _num(row, "household_cnt", "hshld_cnt", "tot_family")
            if hh is not None:
                stats.households.append(YearValue(y, hh))
            avg = _num(row, "avg_fmember_cnt", "avg_family")
            if avg is not None:
                stats.avg_household_size.append(YearValue(y, avg))
            break
        for row in _fetch_stat(fetcher, COMPANY_URL, token, adm_cd, y):
            corp = _num(row, "corp_cnt", "company_cnt")
            if corp is not None:
                stats.companies.append(YearValue(y, corp))
            emp = _num(row, "tot_worker", "employee_cnt", "worker_cnt")
            if emp is not None:
                stats.employees.append(YearValue(y, emp))
            break

    if not (stats.population or stats.households or stats.companies):
        stats.limitations.append("수집 결과 없음 — 연도·행정구역 코드 확인 필요")
    return stats
