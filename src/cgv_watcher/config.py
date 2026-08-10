"""config.yaml 로드 및 검증.

검증 실패는 재시도해도 소용없으므로 ConfigError로 즉시 종료한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .models import parse_clock_minutes

DEFAULT_MIN_SEATS = 1
DEFAULT_COOLDOWN_MINUTES = 30
DEFAULT_ROUNDS = 3
DEFAULT_ROUND_INTERVAL = (60.0, 90.0)
DEFAULT_REQUEST_DELAY = (2.0, 5.0)
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_HEARTBEAT_HOURS = 12  # 하루 2회. 0이면 끔.


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    def contains(self, value: date) -> bool:
        return self.start <= value <= self.end


@dataclass(frozen=True)
class TimeRange:
    """상영일 자정 기준 분 단위. '27:00' 같은 심야 표기를 허용한다."""

    start_minutes: int
    end_minutes: int

    def contains(self, minutes: int) -> bool:
        return self.start_minutes <= minutes <= self.end_minutes


@dataclass(frozen=True)
class WatchRule:
    name: str
    theater: str  # 지점명 또는 siteNo
    movie_title: str | None
    movie_no: str | None
    screen_types: tuple[str, ...]
    date_range: DateRange
    time_range: TimeRange | None
    min_seats: int


@dataclass(frozen=True)
class PollingConfig:
    rounds_per_run: int
    round_interval_sec: tuple[float, float]
    request_delay_sec: tuple[float, float]


@dataclass(frozen=True)
class Config:
    watches: tuple[WatchRule, ...]
    polling: PollingConfig
    cooldown_minutes: int
    notify_on_first_seen: bool
    failure_threshold: int
    heartbeat_hours: int
    state_path: Path = field(default=Path("state/seats.json"))


# ---------------------------------------------------------------------------


def _require_mapping(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: 매핑(key: value)이어야 함, 실제로는 {type(value).__name__}")
    return value


def _parse_pair(value: Any, where: str, fallback: tuple[float, float]) -> tuple[float, float]:
    if value is None:
        return fallback
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ConfigError(f"{where}: [최소, 최대] 형태의 값 2개여야 함")
    try:
        low, high = float(value[0]), float(value[1])
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: 숫자여야 함 — {exc}") from exc
    if low < 0 or high < low:
        raise ConfigError(f"{where}: 0 이상이어야 하고 최소 <= 최대 여야 함 (받은 값: {value})")
    return (low, high)


def _parse_date(value: Any, where: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError as exc:
            raise ConfigError(f"{where}: YYYY-MM-DD 형식이어야 함 (받은 값: {value!r})") from exc
    raise ConfigError(f"{where}: 날짜여야 함 (받은 값: {value!r})")


def _parse_time_range(value: Any, where: str) -> TimeRange | None:
    mapping = _require_mapping(value, where)
    if not mapping:
        return None
    start = mapping.get("start")
    end = mapping.get("end")
    if start is None or end is None:
        raise ConfigError(f"{where}: start 와 end 가 모두 필요함")
    try:
        start_min = parse_clock_minutes(str(start))
        end_min = parse_clock_minutes(str(end))
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from exc
    if end_min < start_min:
        raise ConfigError(
            f"{where}: end 가 start 보다 빠름. 심야까지 보려면 end 를 '26:00' 처럼 24 이상으로 적을 것"
        )
    return TimeRange(start_min, end_min)


def _parse_watch(raw: Any, index: int, defaults: dict[str, Any]) -> WatchRule:
    where = f"watches[{index}]"
    mapping = _require_mapping(raw, where)
    if not mapping:
        raise ConfigError(f"{where}: 내용이 비어 있음")

    name = str(mapping.get("name") or f"watch-{index + 1}")

    theater = mapping.get("theater")
    if not theater:
        raise ConfigError(f"{where}('{name}'): theater 는 필수 (지점명 또는 siteNo)")

    movie = _require_mapping(mapping.get("movie"), f"{where}.movie")
    movie_title = movie.get("title")
    movie_no = movie.get("code") or movie.get("movNo")
    if not movie_title and not movie_no:
        raise ConfigError(f"{where}('{name}'): movie.title 또는 movie.code 중 하나는 필요함")

    date_range_raw = _require_mapping(mapping.get("date_range"), f"{where}.date_range")
    if not date_range_raw:
        raise ConfigError(f"{where}('{name}'): date_range 는 필수")
    start = _parse_date(date_range_raw.get("start"), f"{where}.date_range.start")
    end = _parse_date(date_range_raw.get("end"), f"{where}.date_range.end")
    if end < start:
        raise ConfigError(f"{where}('{name}'): date_range.end 가 start 보다 빠름")

    screen_types_raw = mapping.get("screen_types") or []
    if isinstance(screen_types_raw, str):
        screen_types_raw = [screen_types_raw]
    if not isinstance(screen_types_raw, list):
        raise ConfigError(f"{where}.screen_types: 리스트여야 함")

    min_seats = mapping.get("min_seats", defaults.get("min_seats", DEFAULT_MIN_SEATS))
    try:
        min_seats = int(min_seats)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}.min_seats: 정수여야 함 (받은 값: {min_seats!r})") from exc
    if min_seats < 1:
        raise ConfigError(f"{where}.min_seats: 1 이상이어야 함")

    return WatchRule(
        name=name,
        theater=str(theater).strip(),
        movie_title=str(movie_title).strip() if movie_title else None,
        movie_no=str(movie_no).strip() if movie_no else None,
        screen_types=tuple(str(s).strip() for s in screen_types_raw if str(s).strip()),
        date_range=DateRange(start, end),
        time_range=_parse_time_range(mapping.get("time_range"), f"{where}.time_range"),
        min_seats=min_seats,
    )


def load_config(path: str | Path) -> Config:
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigError(f"config 파일이 없음: {file_path}")

    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"config YAML 문법 오류: {exc}") from exc

    root = _require_mapping(raw, "config 최상위")
    if not root:
        raise ConfigError("config 파일이 비어 있음")

    defaults = _require_mapping(root.get("defaults"), "defaults")
    polling_raw = _require_mapping(root.get("polling"), "polling")

    watches_raw = root.get("watches")
    if not isinstance(watches_raw, list) or not watches_raw:
        raise ConfigError("watches: 최소 1개 이상의 감시 조건이 필요함")

    watches = tuple(
        _parse_watch(item, index, defaults) for index, item in enumerate(watches_raw)
    )

    rounds = polling_raw.get("rounds_per_run", DEFAULT_ROUNDS)
    try:
        rounds = int(rounds)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"polling.rounds_per_run: 정수여야 함 — {exc}") from exc
    if rounds < 1:
        raise ConfigError("polling.rounds_per_run: 1 이상이어야 함")

    cooldown = defaults.get("cooldown_minutes", DEFAULT_COOLDOWN_MINUTES)
    try:
        cooldown = int(cooldown)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"defaults.cooldown_minutes: 정수여야 함 — {exc}") from exc
    if cooldown < 0:
        raise ConfigError("defaults.cooldown_minutes: 0 이상이어야 함")

    threshold = defaults.get("failure_threshold", DEFAULT_FAILURE_THRESHOLD)
    try:
        threshold = int(threshold)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"defaults.failure_threshold: 정수여야 함 — {exc}") from exc
    if threshold < 1:
        raise ConfigError("defaults.failure_threshold: 1 이상이어야 함")

    heartbeat = defaults.get("heartbeat_hours", DEFAULT_HEARTBEAT_HOURS)
    try:
        heartbeat = int(heartbeat)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"defaults.heartbeat_hours: 정수여야 함 — {exc}") from exc
    if heartbeat < 0:
        raise ConfigError("defaults.heartbeat_hours: 0 이상이어야 함 (0 = 끔)")

    return Config(
        watches=watches,
        polling=PollingConfig(
            rounds_per_run=rounds,
            round_interval_sec=_parse_pair(
                polling_raw.get("round_interval_sec"),
                "polling.round_interval_sec",
                DEFAULT_ROUND_INTERVAL,
            ),
            request_delay_sec=_parse_pair(
                polling_raw.get("request_delay_sec"),
                "polling.request_delay_sec",
                DEFAULT_REQUEST_DELAY,
            ),
        ),
        cooldown_minutes=cooldown,
        notify_on_first_seen=bool(defaults.get("notify_on_first_seen", False)),
        failure_threshold=threshold,
        heartbeat_hours=heartbeat,
    )
