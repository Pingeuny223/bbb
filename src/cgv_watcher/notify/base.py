"""알림 채널 공통 인터페이스와 메시지 본문 생성."""

from __future__ import annotations

from typing import Protocol

from ..cgv_client import BOOKING_PAGE_URL
from ..state import Transition


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
        f"💺 잔여 {showtime.free_seats}석 / 전체 {showtime.total_seats}석  ({change})",
        "",
        f"🔗 {BOOKING_PAGE_URL}",
        "",
        f"↑ 위 링크에서 '{showtime.site_name or showtime.site_no}' 와 "
        f"{showtime.display_date()} 을 선택하세요.",
    ]
    if transition.rule_names:
        lines.append(f"(조건: {', '.join(transition.rule_names)})")

    return "좌석 발생", "\n".join(lines)


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
