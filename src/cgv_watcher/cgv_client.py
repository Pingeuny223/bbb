"""CGV HTTP 계층.

★ 엔드포인트/헤더가 바뀌면 여기를 고친다.

실측 결과(2026-08-10 확인):
  - 로그인/쿠키 불필요. 응답은 UTF-8 JSON.
  - 앞단에 Cloudflare가 있고, 브라우저 User-Agent + cgv.co.kr Referer 가
    둘 다 있어야 200이 온다. 하나라도 빠지면 403.

  - **왜 requests 가 아니라 stdlib urllib 인가**
    같은 헤더, 같은 OpenSSL인데 requests(urllib3)로 보내면 전 엔드포인트가
    일관되게 403이고 urllib으로 보내면 200이다. urllib3가 자체 cipher 목록을
    쓰는 탓에 TLS 핸드셰이크 지문이 달라서 Cloudflare가 걸러내는 것으로 보인다.
    지문을 위조하는 대신, 그냥 문제가 없는 표준 라이브러리를 쓴다.
    (알림 채널 쪽은 이런 제약이 없어서 requests 를 그대로 쓴다.)

    재현:
        requests.get(url, headers=H) -> 403
        urllib.request.urlopen(Request(url, headers=H)) -> 200
"""

from __future__ import annotations

import gzip
import http.cookiejar
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any

from .errors import FetchError, ParseError, RateLimitedError
from .ratelimit import Throttle, backoff_delay

log = logging.getLogger(__name__)

BASE_URL = "https://cgv.co.kr/api/v1/booking"

# CGV 법인 코드. CGV 국내 = A420.
CO_CD = "A420"

# 관람등급 제한 범위 코드. 비로그인 웹 클라이언트가 보내는 값 그대로 사용한다.
# 의미를 추측해서 바꾸지 말 것.
RTCTL_SCOP_CD = "08"

# 예매 페이지. 회차 단위 딥링크는 제공되지 않는다(README '알려진 한계' 참고).
BOOKING_PAGE_URL = "https://cgv.co.kr/cnm/movieBook/cinema"

# 고정 User-Agent 하나만 쓴다. 로테이션이나 우회는 하지 않는다.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://cgv.co.kr/",
}

# 백오프 대상 상태코드. 403은 CGV 앞단 WAF가 일시적으로 막을 때도 나온다.
RETRYABLE_STATUS = (403, 408, 429, 500, 502, 503, 504)


def _decompress(body: bytes, encoding: str) -> bytes:
    encoding = (encoding or "").lower()
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        return zlib.decompress(body)
    return body


def _charset_from_content_type(content_type: str) -> str | None:
    for part in (content_type or "").split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            return part.split("=", 1)[1].strip().strip('"')
    return None


class CgvClient:
    """CGV 예매 API 클라이언트. 순차 요청만 수행한다."""

    def __init__(
        self,
        throttle: Throttle,
        timeout: float = 15.0,
        max_attempts: int = 4,
        sleep=None,
    ) -> None:
        self._throttle = throttle
        self._timeout = timeout
        self._max_attempts = max_attempts
        # Cloudflare가 내려주는 __cf_bm 쿠키를 유지한다. 일반 브라우저와 같은
        # 동작이고, 매 요청마다 새 세션으로 보이는 것보다 얌전하다.
        self._cookies = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookies)
        )
        if sleep is None:
            import time

            sleep = time.sleep
        self._sleep = sleep
        self._last_raw: str | None = None

    @property
    def last_raw_response(self) -> str | None:
        """파싱 실패 시 덤프용으로 마지막 원문을 보관한다."""
        return self._last_raw

    def close(self) -> None:
        self._opener.close()

    def __enter__(self) -> "CgvClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- 내부 -------------------------------------------------------------

    def _get(self, path: str, params: dict) -> Any:
        """GET 후 JSON 파싱. 재시도와 백오프를 포함한다."""
        url = f"{BASE_URL}/{path}?{urllib.parse.urlencode(params)}"
        last_error: Exception | None = None

        for attempt in range(self._max_attempts):
            if attempt:
                delay = backoff_delay(attempt - 1)
                log.warning(
                    "%s 재시도 %d/%d — %.1fs 대기 (직전 오류: %s)",
                    path,
                    attempt + 1,
                    self._max_attempts,
                    delay,
                    last_error,
                )
                self._sleep(delay)

            self._throttle.wait()

            request = urllib.request.Request(url, headers=dict(DEFAULT_HEADERS))
            try:
                with self._opener.open(request, timeout=self._timeout) as response:
                    body = _decompress(
                        response.read(), response.headers.get("Content-Encoding", "")
                    )
                    charset = _charset_from_content_type(
                        response.headers.get("Content-Type", "")
                    )
                    return self._decode(body, charset, path)

            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read()[:200].decode("utf-8", errors="replace")
                except Exception:
                    pass
                if exc.code in RETRYABLE_STATUS:
                    last_error = RateLimitedError(
                        f"{path}: HTTP {exc.code}", status=exc.code
                    )
                    continue
                raise FetchError(f"{path}: HTTP {exc.code} — {detail}", status=exc.code)

            except (urllib.error.URLError, OSError) as exc:
                last_error = FetchError(f"{path}: 요청 실패 — {exc}")
                continue

        assert last_error is not None
        raise last_error

    def _decode(self, body: bytes, charset: str | None, path: str) -> Any:
        """응답 본문을 JSON으로 디코딩한다.

        현재 CGV는 charset=UTF-8 을 명시해서 주지만, 선언이 빠지거나
        구 엔드포인트로 돌아가 EUC-KR이 올 경우에 대비해 순서대로 시도한다.
        cp949를 euc-kr보다 먼저 두는 건 cp949가 상위호환이기 때문이다.
        """
        candidates = []
        if charset:
            candidates.append(charset)
        candidates += ["utf-8", "cp949", "euc-kr"]

        text: str | None = None
        for encoding in candidates:
            try:
                text = body.decode(encoding)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if text is None:
            text = body.decode("utf-8", errors="replace")
            log.warning("%s: 알려진 인코딩으로 디코딩 실패, 손실 디코딩으로 진행", path)

        self._last_raw = text

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"{path}: JSON 파싱 실패 — {exc}. "
                f"CGV가 HTML 오류 페이지를 반환했을 수 있음",
                text[:500],
            ) from exc

    # -- 공개 API ---------------------------------------------------------

    def fetch_sites(self) -> Any:
        """전국 지역/지점 목록. 지점명 -> siteNo 매핑에 쓴다."""
        return self._get("searchRegnList", {"coCd": CO_CD, "defaultTabType": "region"})

    def fetch_screening_dates(self, site_no: str) -> Any:
        """해당 지점에서 예매가 열린 날짜 목록."""
        return self._get(
            "searchSiteScnscYmdListBySite", {"coCd": CO_CD, "siteNo": site_no}
        )

    def fetch_showtimes(self, site_no: str, play_date: str) -> Any:
        """지점 + 날짜의 전체 회차. 잔여좌석(frSeatCnt)이 여기 들어 있다.

        회차별로 따로 조회할 필요가 없다 — 이 한 번으로 그 날 전부 받는다.
        """
        return self._get(
            "searchMovScnInfo",
            {
                "coCd": CO_CD,
                "siteNo": site_no,
                "scnYmd": play_date,
                "rtctlScopCd": RTCTL_SCOP_CD,
            },
        )
