"""파서 테스트.

fixtures/searchMovScnInfo.json 은 2026-08-10에 실제 CGV API에서 받은 응답을
잘라낸 것이다. 손으로 만든 가짜가 아니다 — 스키마가 바뀌면 이 테스트가 깨진다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cgv_watcher.errors import ParseError
from cgv_watcher.parser import parse_screening_dates, parse_showtimes, parse_sites

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parses_real_response():
    showtimes = parse_showtimes(load_fixture("searchMovScnInfo.json"))
    assert len(showtimes) == 11
    assert all(s.site_no == "0059" for s in showtimes)
    assert all(s.total_seats > 0 for s in showtimes)


def test_imax_showtime_fields():
    showtimes = parse_showtimes(load_fixture("searchMovScnInfo.json"))
    imax = [s for s in showtimes if "IMAX" in s.screen_name]
    assert imax, "픽스처에 IMAX 회차가 있어야 한다"

    first = min(imax, key=lambda s: s.start_minutes)
    assert first.screen_name == "IMAX관"
    assert first.screen_grade == "아이맥스"
    assert first.total_seats == 387
    assert first.display_time() == "07:00"


def test_late_night_showtime_rolls_over():
    """'2450' 은 익일 00:50 이다. 그냥 자르면 깨지는 지점."""
    showtimes = parse_showtimes(load_fixture("searchMovScnInfo.json"))
    late = [s for s in showtimes if s.start_minutes >= 24 * 60]
    assert late, "픽스처에 심야 회차가 있어야 한다"

    session = min(late, key=lambda s: s.start_minutes)
    assert session.start_hhmm == "2450"
    assert session.display_time() == "24:50"  # 표기는 CGV 그대로
    assert session.start_dt.hour == 0  # 실제 시각은 익일 00:50
    assert session.start_dt.minute == 50
    assert session.start_dt.day == session.play_date_obj.day + 1


def test_showtime_key_is_stable_and_unique():
    showtimes = parse_showtimes(load_fixture("searchMovScnInfo.json"))
    keys = [s.key for s in showtimes]
    assert len(keys) == len(set(keys)), "회차 키가 충돌하면 안 된다"


def test_missing_required_field_raises():
    payload = load_fixture("searchMovScnInfo.json")
    del payload["data"][0]["frSeatCnt"]
    with pytest.raises(ParseError, match="frSeatCnt"):
        parse_showtimes(payload)


def test_error_status_raises():
    with pytest.raises(ParseError, match="오류 상태"):
        parse_showtimes({"statusCode": 500, "statusMessage": "오류", "data": []})


def test_missing_envelope_raises():
    with pytest.raises(ParseError, match="statusCode"):
        parse_showtimes({"data": []})


def test_html_instead_of_json_raises():
    with pytest.raises(ParseError):
        parse_showtimes("<html>403 Forbidden</html>")


def test_empty_schedule_is_not_an_error():
    """상영 일정이 없는 날은 정상적으로 빈 리스트가 온다."""
    assert parse_showtimes({"statusCode": 0, "statusMessage": "ok", "data": []}) == []


def test_all_rows_unparseable_raises():
    """항목은 왔는데 전부 못 읽으면 스키마 변경으로 본다."""
    payload = load_fixture("searchMovScnInfo.json")
    for row in payload["data"]:
        row["frSeatCnt"] = "좌석없음"
    with pytest.raises(ParseError, match="하나도 파싱하지 못함"):
        parse_showtimes(payload)


def test_partial_bad_rows_are_skipped():
    payload = load_fixture("searchMovScnInfo.json")
    payload["data"][0]["frSeatCnt"] = None
    showtimes = parse_showtimes(payload)
    assert len(showtimes) == 10


def test_parse_screening_dates():
    payload = {
        "statusCode": 0,
        "statusMessage": "조회 되었습니다.",
        "data": [
            {"scnYmd": "20260810", "hldyYn": "N"},
            {"scnYmd": "20260815", "hldyYn": "Y"},
        ],
    }
    assert parse_screening_dates(payload) == ["20260810", "20260815"]


def test_parse_sites():
    payload = {
        "statusCode": 0,
        "statusMessage": "조회 되었습니다.",
        "data": [
            {
                "regnGrpNm": "서울",
                "siteList": [
                    {"siteNo": "0059", "siteNm": "영등포타임스퀘어"},
                    {"siteNo": "0112", "siteNm": "여의도"},
                ],
            }
        ],
    }
    assert parse_sites(payload) == {"영등포타임스퀘어": "0059", "여의도": "0112"}
