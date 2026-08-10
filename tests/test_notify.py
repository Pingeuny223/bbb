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

WEBHOOK = "https://discord.com/api/webhooks/1536219021333233725/AbCdEf-secret_TOKEN123"
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
        "with url: /api/webhooks/1536219021333233725/AbCdEf-secret_TOKEN123"
    )
    result = redact_secrets(text, {})  # 환경변수가 없어도 잡아야 한다
    assert "secret_TOKEN123" not in result
    assert "1536219021333233725" not in result


def test_redacts_split_telegram_path():
    text = f"Max retries exceeded with url: /bot{BOT_TOKEN}/sendMessage"
    result = redact_secrets(text, {})
    assert "realbottokenvalue" not in result


def test_discord_network_error_does_not_leak_url():
    """웹훅 URL이 예외 메시지로 새지 않아야 한다."""
    notifier = DiscordNotifier(WEBHOOK)
    boom = requests.ConnectionError(
        f"HTTPSConnectionPool(host='discord.com', port=443): "
        f"Max retries exceeded with url: /api/webhooks/1536219021333233725/"
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
