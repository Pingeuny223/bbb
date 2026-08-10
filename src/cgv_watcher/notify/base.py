"""알림 채널 공통 인터페이스와 메시지 본문 생성."""

from __future__ import annotations

import os
import re
from typing import Protocol

from ..cgv_client import BOOKING_PAGE_URL
from ..state import Transition

# 로그에 절대 나가면 안 되는 값들이 담긴 환경변수.
SECRET_ENV_VARS = (
    "DISCORD_WEBHOOK_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)

# 값이 통째로 나타나지 않고 조각나서 찍히는 경우까지 잡는 패턴.
# 예: requests 예외는 host 와 path 를 따로 찍어서 GitHub 의 secret 마스킹을
#     빠져나간다. public 레포의 Actions 로그는 누구나 볼 수 있으므로
#     토큰이 들어가는 경로 형태 자체를 지운다.
_SECRET_PATTERNS = (
    re.compile(r"/api/webhooks/\d+/[\w.\-]+"),  # 디스코드 웹훅 경로
    re.compile(r"/bot\d+:[\w.\-]+"),  # 텔레그램 봇 토큰 경로
)


def redact_secrets(text: str, env: dict | None = None) -> str:
    """로그로 나가기 전에 비밀값을 가린다.

    notifier 들이 이미 예외 메시지를 정제하지만, 예상 못 한 경로로 값이
    새는 경우에 대비한 마지막 방어선이다.
    """
    source = env if env is not None else os.environ
    result = text

    for name in SECRET_ENV_VARS:
        value = (source.get(name) or "").strip()
        # 너무 짧은 값을 치환하면 멀쩡한 로그가 뭉개진다.
        if len(value) >= 8:
            result = result.replace(value, f"<{name} 가려짐>")

    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("/<가려짐>", result)

    return result


# 알림 종류. 채널마다 아이콘과 색을 다르게 주는 데 쓴다.
LEVEL_SEAT = "seat"
LEVEL_INFO = "info"
LEVEL_ERROR = "error"

_LEVEL_BY_TITLE = {
    "좌석 발생": LEVEL_SEAT,
    "감시 실패": LEVEL_ERROR,
    "감시 정상 동작 중": LEVEL_INFO,
    "감시 기간 종료": LEVEL_INFO,
    "연결 테스트": LEVEL_INFO,
}


def classify(title: str) -> str:
    """제목으로 알림 종류를 판별한다.

    build_*_text 가 만드는 제목이 기준이며, 뒤에 '(테스트)' 같은 꼬리표가
    붙어도 앞부분으로 맞춘다.
    """
    for prefix, level in _LEVEL_BY_TITLE.items():
        if title.startswith(prefix):
            return level
    return LEVEL_INFO


class Notifier(Protocol):
    name: str

    def send(self, title: str, body: str) -> None:
        """알림 1건 전송. 실패하면 예외를 올린다."""
        ...


def build_seat_text(transition: Transition) -> tuple[str, str]:
    """좌석 발생 알림의 (제목, 본문).

    요구사항: 영화명 / 지점 / 상영관 / 날짜 / 시작시간 / 잔여석 수 / 예매 페이지 URL.
    """
    showtime = transition.showtime

    if transition.previous_free is None:
        change = "신규 감지"
    else:
        change = f"{transition.previous_free}석 → {showtime.free_seats}석"

    lines = [
        f"🎬 {showtime.movie_name}",
        f"🏢 {showtime.site_name or showtime.site_no}",
        f"🎦 {showtime.display_screen()}",
        f"📅 {showtime.display_date()}  ⏰ {showtime.display_time()}",
        f"💺 잔여 {showtime.display_seats()}  ({change})",
        "",
        f"🔗 {BOOKING_PAGE_URL}",
        "",
        f"↑ 위 링크에서 '{showtime.site_name or showtime.site_no}' 와 "
        f"{showtime.display_date()} 을 선택하세요.",
    ]
    if transition.rule_names:
        lines.append(f"(조건: {', '.join(transition.rule_names)})")

    return "좌석 발생", "\n".join(lines)


def build_heartbeat_text(
    rows: list,
    last_watch_day,
    checked_at: str,
    max_rows: int = 24,
) -> tuple[str, str]:
    """생존 신고의 (제목, 본문).

    rows 는 (Showtime, 알림에 필요한 좌석 수) 튜플 목록이다.
    필요 좌석은 회차마다 다르다 — 일반관에는 비율 조건이 붙어서 IMAX보다
    훨씬 많은 자리가 남아야 알림이 뜬다. 그래서 회차별로 함께 보여준다.

    '알림이 없다'가 정상인지 죽은 것인지 구분하기 위한 메시지다.
    기왕 보내는 김에 현재 잔여석 현황도 같이 담아 한 번에 파악되게 한다.
    """
    lines = [
        f"🕐 마지막 확인: {checked_at}",
        f"👀 감시 중인 회차: {len(rows)}건",
        f"📆 감시 종료일: {last_watch_day}",
    ]

    if rows:
        lines += ["", "현재 잔여석  (괄호 안은 알림이 뜨는 기준)"]
        ordered = sorted(rows, key=lambda r: (r[0].play_date, r[0].start_minutes))
        for showtime, needed in ordered[:max_rows]:
            mark = "🔔" if showtime.free_seats >= needed else "  "
            lines.append(
                f"{mark} {showtime.display_date()} {showtime.display_time()} "
                f"{showtime.display_site()} {showtime.display_screen()} — "
                f"{showtime.display_seats()} · 기준 {needed}석"
            )
        if len(ordered) > max_rows:
            lines.append(f"   … 외 {len(ordered) - max_rows}건")
    else:
        lines += [
            "",
            "⚠️ 조건에 맞는 회차가 하나도 없습니다.",
            "config.yaml 의 영화명·지점·상영관·날짜를 확인하세요.",
        ]

    lines += ["", "이 메시지는 감시가 살아 있다는 확인용입니다."]
    return "감시 정상 동작 중", "\n".join(lines)


def build_failure_text(consecutive: int, detail: str) -> tuple[str, str]:
    """장애 알림의 (제목, 본문). 조용히 죽지 않기 위한 것."""
    body = "\n".join(
        [
            f"연속 {consecutive}회 실패했습니다. 좌석 감시가 멈춰 있을 수 있습니다.",
            "",
            "마지막 오류:",
            f"{detail[:1200]}",
            "",
            "CGV 응답 구조가 바뀌었다면 src/cgv_watcher/parser.py 의 필드 상수와 "
            "src/cgv_watcher/cgv_client.py 의 엔드포인트를 확인하세요. "
            "(README '파싱이 깨졌을 때' 참고)",
        ]
    )
    return "감시 실패", body
