"""CGV 응답 -> 도메인 모델.

★ CGV가 API를 바꾸면 여기와 cgv_client.py 만 고치면 된다.
   응답 필드명은 전부 이 파일 상단 상수에 모여 있다.
"""

from __future__ import annotations

import logging
from typing import Any

from .errors import ParseError
from .models import Showtime

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 응답 필드명. CGV 스키마가 바뀌면 이 블록을 고친다.
# 실제 응답 샘플은 tests/fixtures/searchMovScnInfo.json 참고.
# ---------------------------------------------------------------------------
F_STATUS = "statusCode"
F_MESSAGE = "statusMessage"
F_DATA = "data"

F_SITE_NO = "siteNo"
F_SITE_NAME = "siteNm"
F_SCREEN_NO = "scnsNo"
F_SCREEN_NAME = "scnsNm"
F_SCREEN_GRADE = "tcscnsGradNm"  # '아이맥스' / '4DX' / 'SCREENX' / '일반'
F_MOVIE_NO = "movNo"
F_MOVIE_NAME = "movNm"
F_MOVIE_KIND = "movkndDsplNm"  # '2D' / '4DX 2D' / 'IMAX 2D' ...
F_RATING = "cratgClsNm"
F_PLAY_DATE = "scnYmd"  # YYYYMMDD
F_START_TIME = "scnsrtTm"  # HHMM, 심야는 '2430' 처럼 24 초과
F_END_TIME = "scnendTm"
F_SEQ = "scnSseq"
F_FREE_SEATS = "frSeatCnt"  # ★ 잔여좌석. 감시 대상.
F_TOTAL_SEATS = "cpSeatCnt"

# 이 키들이 하나라도 없으면 스키마가 바뀐 것으로 보고 ParseError를 낸다.
REQUIRED_FIELDS = (
    F_SITE_NO,
    F_SCREEN_NO,
    F_SCREEN_NAME,
    F_MOVIE_NAME,
    F_PLAY_DATE,
    F_START_TIME,
    F_SEQ,
    F_FREE_SEATS,
    F_TOTAL_SEATS,
)

# 날짜 목록 응답
F_DATE_LIST_YMD = "scnYmd"

# 지역/지점 목록 응답
F_REGION_NAME = "regnGrpNm"
F_SITE_LIST = "siteList"


def _require_envelope(payload: Any, endpoint: str) -> list[dict[str, Any]]:
    """공통 응답 봉투를 검증하고 data 리스트를 꺼낸다."""
    if not isinstance(payload, dict):
        raise ParseError(f"{endpoint}: 응답이 JSON 객체가 아님 ({type(payload).__name__})", payload)

    if F_STATUS not in payload:
        raise ParseError(
            f"{endpoint}: 응답에 '{F_STATUS}' 가 없음. 스키마가 바뀌었을 수 있음", payload
        )

    status = payload.get(F_STATUS)
    if status != 0:
        raise ParseError(
            f"{endpoint}: CGV가 오류 상태를 반환 "
            f"({F_STATUS}={status!r}, {F_MESSAGE}={payload.get(F_MESSAGE)!r})",
            payload,
        )

    data = payload.get(F_DATA)
    if data is None:
        raise ParseError(f"{endpoint}: '{F_DATA}' 가 없음", payload)
    if not isinstance(data, list):
        raise ParseError(f"{endpoint}: '{F_DATA}' 가 리스트가 아님 ({type(data).__name__})", payload)
    return data


def _as_int(value: Any) -> int | None:
    """CGV는 좌석 수를 문자열('387')로 준다. 숫자로 못 바꾸면 None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def parse_showtimes(payload: Any) -> list[Showtime]:
    """searchMovScnInfo 응답을 Showtime 리스트로 변환한다.

    상영 일정이 아예 없는 날은 data가 빈 리스트로 오며, 이건 정상이다.
    반대로 항목은 있는데 전부 파싱에 실패하면 스키마 변경으로 보고 예외를 낸다.
    """
    rows = _require_envelope(payload, "searchMovScnInfo")

    showtimes: list[Showtime] = []
    skipped: list[str] = []

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            skipped.append(f"[{index}] 항목이 객체가 아님")
            continue

        missing = [key for key in REQUIRED_FIELDS if key not in row]
        if missing:
            raise ParseError(
                f"searchMovScnInfo: 회차 [{index}]에 필수 필드 누락 {missing}. "
                f"CGV 스키마 변경 의심 — parser.py 의 필드 상수를 확인할 것",
                row,
            )

        free = _as_int(row[F_FREE_SEATS])
        total = _as_int(row[F_TOTAL_SEATS])
        if free is None or total is None:
            skipped.append(
                f"[{index}] 좌석 수를 숫자로 못 읽음 "
                f"({F_FREE_SEATS}={row[F_FREE_SEATS]!r}, {F_TOTAL_SEATS}={row[F_TOTAL_SEATS]!r})"
            )
            continue

        start = str(row[F_START_TIME]).strip()
        if len(start) != 4 or not start.isdigit():
            skipped.append(f"[{index}] 시작시간이 HHMM 형식이 아님 ({start!r})")
            continue

        showtimes.append(
            Showtime(
                site_no=str(row[F_SITE_NO]).strip(),
                site_name=str(row.get(F_SITE_NAME, "")).strip(),
                screen_no=str(row[F_SCREEN_NO]).strip(),
                screen_name=str(row[F_SCREEN_NAME]).strip(),
                screen_grade=str(row.get(F_SCREEN_GRADE) or "").strip(),
                movie_no=str(row.get(F_MOVIE_NO, "")).strip(),
                movie_name=str(row[F_MOVIE_NAME]).strip(),
                movie_kind=str(row.get(F_MOVIE_KIND) or "").strip(),
                rating=str(row.get(F_RATING) or "").strip(),
                play_date=str(row[F_PLAY_DATE]).strip(),
                start_hhmm=start,
                end_hhmm=str(row.get(F_END_TIME) or "").strip(),
                seq=str(row[F_SEQ]).strip(),
                free_seats=free,
                total_seats=total,
            )
        )

    if skipped:
        log.warning(
            "회차 %d건을 건너뜀 (전체 %d건): %s",
            len(skipped),
            len(rows),
            "; ".join(skipped[:5]),
        )

    if rows and not showtimes:
        raise ParseError(
            f"searchMovScnInfo: 회차 {len(rows)}건이 왔는데 단 하나도 파싱하지 못함. "
            f"CGV 스키마 변경 의심. 사유: {'; '.join(skipped[:5])}",
            rows[0] if rows else None,
        )

    return showtimes


def parse_screening_dates(payload: Any) -> list[str]:
    """searchSiteScnscYmdListBySite 응답 -> ['20260810', ...]."""
    rows = _require_envelope(payload, "searchSiteScnscYmdListBySite")

    dates: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ymd = row.get(F_DATE_LIST_YMD)
        if ymd is None:
            continue
        text = str(ymd).strip()
        if len(text) == 8 and text.isdigit():
            dates.append(text)

    if rows and not dates:
        raise ParseError(
            "searchSiteScnscYmdListBySite: 날짜를 하나도 못 읽음. 스키마 변경 의심",
            rows[0],
        )
    return dates


def parse_sites(payload: Any) -> dict[str, str]:
    """searchRegnList 응답 -> {지점명: siteNo}.

    지점명은 '영등포타임스퀘어' 처럼 'CGV ' 접두사 없이 온다.
    """
    regions = _require_envelope(payload, "searchRegnList")

    sites: dict[str, str] = {}
    for region in regions:
        if not isinstance(region, dict):
            continue
        for site in region.get(F_SITE_LIST) or []:
            if not isinstance(site, dict):
                continue
            name = site.get(F_SITE_NAME)
            no = site.get(F_SITE_NO)
            if name and no:
                sites[str(name).strip()] = str(no).strip()

    if not sites:
        raise ParseError("searchRegnList: 지점을 하나도 못 읽음. 스키마 변경 의심", regions[:1])
    return sites
