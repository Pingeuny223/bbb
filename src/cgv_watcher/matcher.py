"""config 규칙과 실제 회차를 대조한다."""

from __future__ import annotations

import logging

from .config import WatchRule
from .errors import ConfigError
from .models import (
    SCREEN_ALIASES,  # noqa: F401  — 별칭표는 models 에 있다. 여기서 재노출한다.
    Showtime,
    normalize_screen as _normalize,
    screen_alias_tokens as _alias_tokens,
)

log = logging.getLogger(__name__)


def matches_screen(showtime: Showtime, screen_types: tuple[str, ...]) -> bool:
    if not screen_types:
        return True
    haystack = _normalize(f"{showtime.screen_grade} {showtime.screen_name} {showtime.movie_kind}")
    for value in screen_types:
        if any(token in haystack for token in _alias_tokens(value)):
            return True
    return False


def matches_movie(showtime: Showtime, rule: WatchRule) -> bool:
    if rule.movie_no:
        return showtime.movie_no == rule.movie_no
    if rule.movie_title:
        return _normalize(rule.movie_title) in _normalize(showtime.movie_name)
    return True


def matches(showtime: Showtime, rule: WatchRule) -> bool:
    """이 회차가 규칙에 해당하는지. 좌석 수 조건은 여기서 보지 않는다."""
    if not rule.date_range.contains(showtime.play_date_obj):
        return False
    if rule.time_range and not rule.time_range.contains(showtime.start_minutes):
        return False
    if not matches_movie(showtime, rule):
        return False
    if not matches_screen(showtime, rule.screen_types):
        return False
    return True


def resolve_site_no(rule: WatchRule, sites: dict[str, str]) -> str:
    """규칙의 theater 값을 siteNo로 바꾼다.

    이미 코드 형태('0059')면 그대로 쓰고, 아니면 지점명으로 찾는다.
    정확히 일치 -> 부분 일치 순으로 시도하며, 부분 일치가 여럿이면 실패시킨다
    (조용히 엉뚱한 지점을 감시하는 것보다 낫다).
    """
    value = rule.theater.strip()

    if value in sites.values():
        return value

    if value in sites:
        return sites[value]

    normalized = _normalize(value)
    exact = [no for name, no in sites.items() if _normalize(name) == normalized]
    if len(exact) == 1:
        return exact[0]

    partial = {name: no for name, no in sites.items() if normalized in _normalize(name)}
    if len(partial) == 1:
        name, no = next(iter(partial.items()))
        log.info("지점 '%s' 을(를) '%s'(%s) 로 해석함", value, name, no)
        return no
    if len(partial) > 1:
        raise ConfigError(
            f"watches['{rule.name}'].theater='{value}' 가 여러 지점에 해당함: "
            f"{sorted(partial)}. 정확한 지점명이나 siteNo를 적을 것"
        )

    raise ConfigError(
        f"watches['{rule.name}'].theater='{value}' 에 해당하는 지점을 찾을 수 없음. "
        f"README의 지점 목록 확인 방법을 참고할 것"
    )
