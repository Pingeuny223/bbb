"""알림 채널 선택과 비밀값 유출 방지 테스트.

레포가 public이면 Actions 로그도 public이다. 웹훅 URL과 봇 토큰은 그 자체가
인증 수단이므로 어떤 경로로도 로그에 남으면 안 된다.
"""

from __future__ import annotations

from unittest import mock

import pytest
import requests

from cgv_watcher.notify import build_notifiers, redact_secrets
from cgv_watcher.notify.discord import DiscordNotifier
from cgv_watcher.notify.telegram import TelegramNotifier

WEBHOOK = "https://discord.com/api/webhooks/111111111111111111/AbCdEf-secret_TOKEN123"
BOT_TOKEN = "7891234567:AAF-realbottokenvalue12345678"


# -- 채널 선택 ---------------------------------------------------------------


def test_only_discord_configured():
    notifiers = build_notifiers({"DISCORD_WEBHOOK_URL": WEBHOOK})
    assert [n.name for n in notifiers] == ["discord"]


def test_only_telegram_configured():
    notifiers = build_notifiers(
        {"TELEGRAM_BOT_TOKEN": BOT_TOKEN, "TELEGRAM_CHAT_ID": "12345"}
    )
    assert [n.name for n in notifiers] == ["telegram"]


def test_both_configured():
    notifiers = build_notifiers(
        {
            "DISCORD_WEBHOOK_URL": WEBHOOK,
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "TELEGRAM_CHAT_ID": "12345",
        }
    )
    assert sorted(n.name for n in notifiers) == ["discord", "telegram"]


def test_partial_telegram_is_ignored():
    """토큰만 있고 chat_id가 없으면 켜지 않는다."""
    assert build_notifiers({"TELEGRAM_BOT_TOKEN": BOT_TOKEN}) == []


def test_nothing_configured():
    assert build_notifiers({}) == []


def test_blank_secret_is_ignored():
    assert build_notifiers({"DISCORD_WEBHOOK_URL": "   "}) == []


# -- 비밀값 유출 방지 ---------------------------------------------------------


def test_redacts_exact_secret_value():
    text = f"연결 실패: {WEBHOOK}"
    result = redact_secrets(text, {"DISCORD_WEBHOOK_URL": WEBHOOK})
    assert WEBHOOK not in result
    assert "secret_TOKEN123" not in result


def test_redacts_split_webhook_path():
    """requests 예외는 host와 path를 쪼개 찍어 GitHub 마스킹을 빠져나간다.

    실제로 관측된 형태:
      HTTPSConnectionPool(host='discord.com', port=443):
      Max retries exceeded with url: /api/webhooks/123/TOKEN
    """
    text = (
        "HTTPSConnectionPool(host='discord.com', port=443): Max retries exceeded "
        "with url: /api/webhooks/111111111111111111/AbCdEf-secret_TOKEN123"
    )
    result = redact_secrets(text, {})  # 환경변수가 없어도 잡아야 한다
    assert "secret_TOKEN123" not in result
    assert "111111111111111111" not in result


def test_redacts_split_telegram_path():
    text = f"Max retries exceeded with url: /bot{BOT_TOKEN}/sendMessage"
    result = redact_secrets(text, {})
    assert "realbottokenvalue" not in result


def test_discord_network_error_does_not_leak_url():
    """웹훅 URL이 예외 메시지로 새지 않아야 한다."""
    notifier = DiscordNotifier(WEBHOOK)
    boom = requests.ConnectionError(
        f"HTTPSConnectionPool(host='discord.com', port=443): "
        f"Max retries exceeded with url: /api/webhooks/111111111111111111/"
        f"AbCdEf-secret_TOKEN123"
    )
    with mock.patch("requests.post", side_effect=boom):
        with pytest.raises(RuntimeError) as excinfo:
            notifier.send("제목", "본문")

    message = str(excinfo.value)
    assert "secret_TOKEN123" not in message
    assert "api/webhooks" not in message
    assert "ConnectionError" in message  # 진단은 가능해야 한다


def test_telegram_network_error_does_not_leak_token():
    notifier = TelegramNotifier(BOT_TOKEN, "12345")
    boom = requests.ConnectionError(
        f"Max retries exceeded with url: /bot{BOT_TOKEN}/sendMessage"
    )
    with mock.patch("requests.post", side_effect=boom):
        with pytest.raises(RuntimeError) as excinfo:
            notifier.send("제목", "본문")

    assert BOT_TOKEN not in str(excinfo.value)
    assert "realbottokenvalue" not in str(excinfo.value)


def test_discord_http_error_does_not_leak_url():
    response = mock.Mock(status_code=404, text="Unknown Webhook")
    with mock.patch("requests.post", return_value=response):
        notifier = DiscordNotifier(WEBHOOK)
        with pytest.raises(RuntimeError) as excinfo:
            notifier.send("제목", "본문")

    assert "secret_TOKEN123" not in str(excinfo.value)


def test_redaction_leaves_normal_text_alone():
    text = "지점 0059 20260814: 회차 40건 수신"
    assert redact_secrets(text, {"DISCORD_WEBHOOK_URL": WEBHOOK}) == text


def test_short_secret_value_does_not_mangle_logs():
    """chat_id 같은 짧은 값으로 로그가 뭉개지면 안 된다."""
    text = "지점 12345 회차 수신"
    assert redact_secrets(text, {"TELEGRAM_CHAT_ID": "12345"}) == text


# -- 로깅 파이프라인 ---------------------------------------------------------


def test_verbose_does_not_enable_urllib3_debug_logging():
    """--verbose 로 urllib3 DEBUG 를 켜면 요청 URL 전체가 로그에 남는다.

    디스코드 웹훅과 텔레그램 토큰은 URL 경로 안에 있으므로 실제 유출이 된다.
    (관측된 실제 로그:
     urllib3.connectionpool: https://discord.com:443 "POST /api/webhooks/<id>/<token>")
    """
    import logging

    from cgv_watcher.cli import _setup_logging

    _setup_logging(verbose=True)
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("urllib3").level == logging.WARNING
    assert logging.getLogger("requests").level == logging.WARNING


def test_log_filter_redacts_third_party_records():
    """우리 코드가 아닌 라이브러리가 찍어도 걸러져야 한다."""
    import logging

    from cgv_watcher.cli import _setup_logging

    _setup_logging(verbose=True)
    handler = logging.getLogger().handlers[0]

    record = logging.LogRecord(
        name="urllib3.connectionpool",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg='https://discord.com:443 "POST %s HTTP/1.1" 204',
        args=("/api/webhooks/111111111111111111/AbCdEf-secret_TOKEN123",),
        exc_info=None,
    )
    for log_filter in handler.filters:
        log_filter.filter(record)

    assert "secret_TOKEN123" not in record.getMessage()


# -- 생존 신고 ---------------------------------------------------------------


def _sample_showtime(free, hhmm="1730"):
    from cgv_watcher.models import Showtime

    return Showtime(
        site_no="0059", site_name="CGV 영등포타임스퀘어", screen_no="017",
        screen_name="IMAX관", screen_grade="아이맥스", movie_no="30001323",
        movie_name="오디세이", movie_kind="IMAX 2D", rating="15세",
        play_date="20260814", start_hhmm=hhmm, end_hhmm="2032", seq=hhmm,
        free_seats=free, total_seats=387,
    )


def test_heartbeat_marks_rows_above_threshold():
    from datetime import date

    from cgv_watcher.notify.base import build_heartbeat_text

    title, body = build_heartbeat_text(
        showtimes=[_sample_showtime(7, "1400"), _sample_showtime(2, "1730")],
        min_seats=4,
        last_watch_day=date(2026, 8, 15),
        checked_at="2026-08-10 14:30 KST",
    )
    assert title == "감시 정상 동작 중"
    assert body.count("🔔") == 1  # 4석 이상인 회차만 표시

    # 잔여석은 '잔여(총 N석)' 형태로, 지점명도 함께 보여야 한다.
    assert "7석(총 387석)" in body
    assert "2석(총 387석)" in body
    assert "영등포타임스퀘어 IMAX관" in body
    assert "CGV 영등포" not in body  # 목록에서는 'CGV ' 접두사를 뗀다


def test_heartbeat_warns_when_nothing_matches():
    from datetime import date

    from cgv_watcher.notify.base import build_heartbeat_text

    _, body = build_heartbeat_text(
        showtimes=[], min_seats=4,
        last_watch_day=date(2026, 8, 15), checked_at="x",
    )
    assert "회차가 하나도 없습니다" in body


def test_notification_icons_differ_by_kind():
    from cgv_watcher.notify.base import (
        LEVEL_ERROR, LEVEL_INFO, LEVEL_SEAT, classify,
    )

    assert classify("좌석 발생") == LEVEL_SEAT
    assert classify("좌석 발생 (테스트)") == LEVEL_SEAT
    assert classify("감시 실패") == LEVEL_ERROR
    assert classify("감시 정상 동작 중") == LEVEL_INFO
    assert classify("감시 기간 종료") == LEVEL_INFO
