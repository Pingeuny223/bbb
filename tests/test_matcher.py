"""규칙 대조 테스트."""

from __future__ import annotations

from datetime import date

import pytest

from cgv_watcher.config import DateRange, TimeRange, WatchRule
from cgv_watcher.errors import ConfigError
from cgv_watcher.matcher import (
    matches,
    matches_screen,
    required_seats,
    resolve_site_no,
)
from cgv_watcher.models import Showtime

SITES = {
    "영등포타임스퀘어": "0059",
    "여의도": "0112",
    "용산아이파크몰": "0013",
    "씨네드쉐프 용산": "P013",
    "인천": "0002",
    "인천가정": "0296",
    "인천연수": "0258",
}


def make_showtime(**overrides) -> Showtime:
    base = dict(
        site_no="0059",
        site_name="CGV 영등포타임스퀘어",
        screen_no="017",
        screen_name="IMAX관",
        screen_grade="아이맥스",
        movie_no="30001323",
        movie_name="오디세이",
        movie_kind="IMAX 2D",
        rating="15세이상관람가",
        play_date="20260811",
        start_hhmm="1730",
        end_hhmm="2032",
        seq="4",
        free_seats=4,
        total_seats=387,
    )
    base.update(overrides)
    return Showtime(**base)


def make_rule(**overrides) -> WatchRule:
    base = dict(
        name="테스트",
        theater="영등포타임스퀘어",
        movie_title="오디세이",
        movie_no=None,
        screen_types=(),
        date_range=DateRange(date(2026, 8, 11), date(2026, 8, 17)),
        time_range=None,
        min_seats=1,
        min_free_ratio=0.0,
    )
    base.update(overrides)
    return WatchRule(**base)


# -- 상영관 -----------------------------------------------------------------


@pytest.mark.parametrize("value", ["IMAX", "imax", "아이맥스", "i m a x"])
def test_imax_aliases(value):
    """CGV는 scnsNm에 'IMAX관', tcscnsGradNm에 '아이맥스'로 준다. 둘 다 잡아야 한다."""
    assert matches_screen(make_showtime(), (value,))


@pytest.mark.parametrize("value", ["SCREENX", "스크린X", "스크린엑스", "screen x"])
def test_screenx_aliases(value):
    showtime = make_showtime(
        screen_name="SCREENX관 (리클라이너) with PRIVATE BOX",
        screen_grade="SCREENX",
        movie_kind="SCREENX 2D",
    )
    assert matches_screen(showtime, (value,))


def test_4dx_does_not_match_imax():
    showtime = make_showtime(screen_name="4DX관", screen_grade="4DX", movie_kind="4DX 2D")
    assert not matches_screen(showtime, ("IMAX",))
    assert matches_screen(showtime, ("4DX",))


def test_empty_screen_types_matches_everything():
    assert matches_screen(make_showtime(screen_name="1관", screen_grade="일반"), ())


# -- 종합 -------------------------------------------------------------------


def test_matches_all_conditions():
    assert matches(make_showtime(), make_rule(screen_types=("IMAX",)))


def test_date_out_of_range():
    assert not matches(make_showtime(play_date="20260820"), make_rule())


def test_time_out_of_range():
    rule = make_rule(time_range=TimeRange(18 * 60, 23 * 60))
    assert not matches(make_showtime(start_hhmm="1730"), rule)
    assert matches(make_showtime(start_hhmm="1900"), rule)


def test_late_night_time_range():
    """심야 회차는 24시 이후 표기로 비교해야 잡힌다."""
    rule = make_rule(time_range=TimeRange(23 * 60, 26 * 60))
    assert matches(make_showtime(start_hhmm="2450"), rule)


def test_movie_title_partial_match():
    assert matches(make_showtime(movie_name="오디세이"), make_rule(movie_title="오디세"))
    assert not matches(make_showtime(movie_name="오디세이"), make_rule(movie_title="듄"))


def test_movie_code_exact_match():
    rule = make_rule(movie_title=None, movie_no="30001323")
    assert matches(make_showtime(movie_no="30001323"), rule)
    assert not matches(make_showtime(movie_no="30001192"), rule)


# -- 지점 해석 ---------------------------------------------------------------


def test_resolve_by_exact_name():
    assert resolve_site_no(make_rule(theater="영등포타임스퀘어"), SITES) == "0059"


def test_resolve_by_site_code():
    assert resolve_site_no(make_rule(theater="0112"), SITES) == "0112"


def test_resolve_by_partial_name():
    assert resolve_site_no(make_rule(theater="영등포"), SITES) == "0059"


def test_ambiguous_name_fails_loudly():
    """'용산'은 '용산아이파크몰'과 '씨네드쉐프 용산' 둘 다에 걸린다.

    조용히 아무거나 고르면 엉뚱한 지점을 감시하게 되므로 실패시킨다.
    """
    with pytest.raises(ConfigError, match="여러 지점"):
        resolve_site_no(make_rule(theater="용산"), SITES)


def test_unknown_theater_fails():
    with pytest.raises(ConfigError, match="찾을 수 없음"):
        resolve_site_no(make_rule(theater="없는지점"), SITES)


# -- 필요 좌석 수 (min_seats + min_free_ratio) --------------------------------


def test_required_seats_without_ratio():
    """비율 조건이 없으면 min_seats 그대로. 특별관은 이쪽."""
    rule = make_rule(min_seats=4, min_free_ratio=0.0)
    assert required_seats(make_showtime(total_seats=387), rule) == 4


def test_required_seats_with_ratio_uses_stricter_side():
    """일반관: 절반 이상 남아야 한다. 320석 -> 160석."""
    rule = make_rule(min_seats=4, min_free_ratio=0.5)
    assert required_seats(make_showtime(total_seats=320), rule) == 160


def test_required_seats_ratio_rounds_up():
    """반올림이 아니라 올림. 기준이 느슨해지면 안 된다."""
    rule = make_rule(min_seats=1, min_free_ratio=0.5)
    assert required_seats(make_showtime(total_seats=387), rule) == 194  # 193.5 -> 194


def test_required_seats_min_seats_wins_when_larger():
    """비율이 낮아도 min_seats 아래로는 안 내려간다."""
    rule = make_rule(min_seats=10, min_free_ratio=0.01)
    assert required_seats(make_showtime(total_seats=100), rule) == 10


def test_imax_cancellation_does_not_meet_ratio_but_meets_plain_rule():
    """실제 상황 재현.

    IMAX 387석에 4석이 남았다. 특별관 규칙(비율 없음)에서는 알림 대상이지만,
    같은 회차에 비율 0.5를 걸면 걸리지 않는다.
    """
    showtime = make_showtime(total_seats=387, free_seats=4)
    assert showtime.free_seats >= required_seats(
        showtime, make_rule(min_seats=4, min_free_ratio=0.0)
    )
    assert showtime.free_seats < required_seats(
        showtime, make_rule(min_seats=4, min_free_ratio=0.5)
    )


def test_newly_opened_showtime_passes_ratio():
    """새로 편성된 회차는 거의 비어 있으므로 비율 조건을 통과한다."""
    showtime = make_showtime(total_seats=320, free_seats=320)
    assert showtime.free_seats >= required_seats(
        showtime, make_rule(min_seats=4, min_free_ratio=0.5)
    )
