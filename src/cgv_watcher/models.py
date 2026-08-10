"""도메인 모델.

CGV 응답 필드명(cgv_client/parser)과 나머지 코드를 분리하는 경계면이다.
CGV가 필드명을 바꾸면 parser.py 만 고치면 되고 이 파일은 그대로 둔다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

# 한국은 1988년 이후 서머타임이 없다. 고정 오프셋으로 충분하고,
# 이러면 tzdata 패키지나 OS의 타임존 DB에 의존하지 않는다.
KST = timezone(timedelta(hours=9), "KST")

_WEEKDAY_KO = ("월", "화", "수", "목", "금", "토", "일")

# 특별관 표기 흔들림 흡수용 별칭.
# CGV는 같은 상영관을 tcscnsGradNm에 '아이맥스', scnsNm에 'IMAX관' 처럼 다르게 준다.
# matcher(조건 대조)와 display_screen(중복 표기 제거)이 같이 쓴다.
SCREEN_ALIASES: dict = {
    "IMAX": ("IMAX", "아이맥스"),
    "SCREENX": ("SCREENX", "스크린X", "스크린엑스"),
    "4DX": ("4DX",),
    "ULTRA4DX": ("ULTRA4DX", "울트라4DX"),
    "DOLBYATMOS": ("DOLBYATMOS", "돌비애트모스", "돌비"),
    "SPHEREX": ("SPHEREX", "스피어X"),
    "일반": ("일반",),
}


def normalize_screen(text: str) -> str:
    """공백/하이픈 제거 + 대문자화. '스크린 X' 와 'SCREENX' 를 같게 만든다."""
    return text.replace(" ", "").replace("-", "").replace("_", "").upper()


def screen_alias_tokens(value: str) -> tuple:
    """상영관 표기를 비교용 토큰들로 확장한다."""
    normalized = normalize_screen(value)
    for canonical, aliases in SCREEN_ALIASES.items():
        if normalized == normalize_screen(canonical) or any(
            normalized == normalize_screen(a) for a in aliases
        ):
            return tuple(normalize_screen(a) for a in aliases)
    # 별칭표에 없으면 적힌 값을 그대로 부분일치에 쓴다.
    return (normalized,)


def parse_hhmm_minutes(value: str) -> int:
    """'1730' -> 1050, '2430' -> 1470.

    CGV는 심야 회차를 24시를 넘겨 표기한다('2430' = 익일 00:30).
    자정을 넘기는 비교를 단순하게 만들려고 '상영일 자정으로부터의 분'으로 다룬다.
    """
    digits = value.strip()
    if len(digits) != 4 or not digits.isdigit():
        raise ValueError(f"HHMM 형식이 아님: {value!r}")
    return int(digits[:2]) * 60 + int(digits[2:])


def parse_clock_minutes(value: str) -> int:
    """config의 '18:00' -> 1080. '27:00'(=익일 03:00) 같은 표기도 허용한다."""
    text = value.strip()
    hh, _, mm = text.partition(":")
    if not hh.isdigit() or not mm.isdigit():
        raise ValueError(f"HH:MM 형식이 아님: {value!r}")
    return int(hh) * 60 + int(mm)


@dataclass(frozen=True)
class Showtime:
    """상영 회차 하나."""

    site_no: str
    site_name: str
    screen_no: str
    screen_name: str
    screen_grade: str
    movie_no: str
    movie_name: str
    movie_kind: str
    rating: str
    play_date: str  # YYYYMMDD
    start_hhmm: str  # '1730' 또는 '2430'
    end_hhmm: str
    seq: str
    free_seats: int
    total_seats: int

    @property
    def key(self) -> str:
        """회차 고유키. state 저장 및 중복 알림 방지의 기준."""
        return f"{self.site_no}|{self.screen_no}|{self.play_date}|{self.seq}"

    @property
    def start_minutes(self) -> int:
        """상영일 자정으로부터의 분. 24시 이후 회차는 1440을 넘는다."""
        return parse_hhmm_minutes(self.start_hhmm)

    @property
    def start_dt(self) -> datetime:
        """실제 시작 시각(KST). '2430'이면 다음 날 00:30이 된다."""
        day = datetime.strptime(self.play_date, "%Y%m%d").date()
        minutes = self.start_minutes
        return datetime.combine(day, datetime.min.time(), tzinfo=KST) + timedelta(
            minutes=minutes
        )

    @property
    def play_date_obj(self) -> date:
        return datetime.strptime(self.play_date, "%Y%m%d").date()

    def display_date(self) -> str:
        d = self.play_date_obj
        return f"{d.year}-{d.month:02d}-{d.day:02d} ({_WEEKDAY_KO[d.weekday()]})"

    def display_time(self) -> str:
        """CGV 표기 그대로 보여준다(24:30 같은 심야 표기 유지)."""
        m = self.start_minutes
        return f"{m // 60:02d}:{m % 60:02d}"

    def display_site(self) -> str:
        """지점명. 목록에서 쓰기 좋게 'CGV ' 접두사를 뗀다."""
        name = self.site_name or self.site_no
        return name[4:] if name.startswith("CGV ") else name

    def display_seats(self) -> str:
        """'9석(총 387석)' 형태."""
        return f"{self.free_seats}석(총 {self.total_seats}석)"

    def display_screen(self) -> str:
        """상영관 표기.

        상영관명에 이미 특별관 정보가 들어 있으면('IMAX관' + '아이맥스') 등급을
        덧붙이지 않는다. 별칭까지 비교해서 'IMAX관 (아이맥스)' 같은 중복을 막는다.
        """
        if not self.screen_grade or self.screen_grade == "일반":
            return self.screen_name

        haystack = normalize_screen(self.screen_name)
        if any(token in haystack for token in screen_alias_tokens(self.screen_grade)):
            return self.screen_name
        return f"{self.screen_name} ({self.screen_grade})"


@dataclass(frozen=True)
class SeatState:
    """이전 실행에서 관측한 회차 상태."""

    free_seats: int
    total_seats: int
    last_seen_at: str
    last_notified_at: str | None = None
    # 좌석 감소 경고를 이미 보냈는지. 회차당 한 번만 보내기 위한 표시로,
    # 임계선 근처에서 좌석이 오르내려도 반복 알림이 나가지 않게 한다.
    filling_alerted: bool = False
