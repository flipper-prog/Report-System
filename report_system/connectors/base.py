"""HTTP 수집 공통부: 캐시·재시도·수집 이력(Provenance)."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

DEFAULT_TIMEOUT = 20
RETRIES = 3
BACKOFF = 2.0

KEY_ENV = "DATA_GO_KR_API_KEY"


class MissingApiKeyError(RuntimeError):
    pass


def api_key() -> str:
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise MissingApiKeyError(
            f"환경변수 {KEY_ENV} 가 설정되지 않았습니다.\n"
            "  1) https://www.data.go.kr 회원가입 후 아래 두 API에 활용신청:\n"
            "     - 국토교통부_아파트 매매 실거래가 자료\n"
            "     - 한국부동산원_청약홈 분양정보/경쟁률 조회 서비스\n"
            f"  2) 발급받은 일반 인증키(Decoding)를 export {KEY_ENV}=... 로 설정\n"
            "  (승인까지 통상 즉시~1시간, odcloud 계열은 자동승인)")
    return key


@dataclass
class Provenance:
    """근거원장(데이터 원장)에 기록할 수집 이력."""
    source: str          # 기관·데이터명
    url: str             # 파라미터 포함 요청 URL (serviceKey는 마스킹)
    fetched_at: str      # UTC ISO
    sha256: str          # 응답 본문 해시
    from_cache: bool


def _mask_key(url: str) -> str:
    return url.replace(os.environ.get(KEY_ENV, "§none§"), "***KEY***")


class Fetcher:
    """디스크 캐시 우선 HTTP GET."""

    def __init__(self, cache_dir: str = "out/cache", offline: bool = False):
        self.cache = pathlib.Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self.provenance: list[Provenance] = []

    def get(self, source: str, url: str, params: dict) -> bytes:
        qs = urllib.parse.urlencode(params, safe=":")
        full = f"{url}?{qs}"
        digest = hashlib.sha256(full.encode()).hexdigest()[:24]
        path = self.cache / f"{digest}.bin"

        if path.exists():
            body = path.read_bytes()
            self._record(source, full, body, from_cache=True)
            return body
        if self.offline:
            raise RuntimeError(f"offline 모드: 캐시에 없는 요청 {_mask_key(full)}")

        body = self._fetch_with_retry(full)
        path.write_bytes(body)
        self._record(source, full, body, from_cache=False)
        return body

    def _fetch_with_retry(self, full_url: str) -> bytes:
        last: Optional[Exception] = None
        for attempt in range(RETRIES):
            try:
                req = urllib.request.Request(
                    full_url, headers={"User-Agent": "report-system/0.1"})
                with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as r:
                    return r.read()
            except (urllib.error.URLError, TimeoutError) as e:  # noqa: PERF203
                last = e
                time.sleep(BACKOFF * (attempt + 1))
        raise RuntimeError(f"수집 실패({RETRIES}회): {_mask_key(full_url)} — {last}")

    def _record(self, source: str, full_url: str, body: bytes, from_cache: bool):
        self.provenance.append(Provenance(
            source=source,
            url=_mask_key(full_url),
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            sha256=hashlib.sha256(body).hexdigest(),
            from_cache=from_cache))

    def provenance_json(self) -> str:
        return json.dumps([p.__dict__ for p in self.provenance],
                          ensure_ascii=False, indent=2)
