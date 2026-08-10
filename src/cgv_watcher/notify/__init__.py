"""알림 채널.

토큰/웹훅은 오직 환경변수(= GitHub repo secrets)에서만 읽는다.
코드나 config.yaml 에는 절대 두지 않는다.

secrets에 설정된 채널만 활성화된다. 둘 다 없으면 실행을 시작하지 않는다.
"""

from __future__ import annotations

import logging
import os

from .base import (
    Notifier,
    build_failure_text,
    build_heartbeat_text,
    build_seat_text,
    redact_secrets,
)
from .discord import DiscordNotifier
from .telegram import TelegramNotifier

log = logging.getLogger(__name__)

ENV_DISCORD_WEBHOOK = "DISCORD_WEBHOOK_URL"
ENV_TELEGRAM_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_TELEGRAM_CHAT_ID = "TELEGRAM_CHAT_ID"

__all__ = [
    "Notifier",
    "DiscordNotifier",
    "TelegramNotifier",
    "build_seat_text",
    "build_failure_text",
    "build_heartbeat_text",
    "redact_secrets",
    "build_notifiers",
    "ENV_DISCORD_WEBHOOK",
    "ENV_TELEGRAM_TOKEN",
    "ENV_TELEGRAM_CHAT_ID",
]


def build_notifiers(env: dict[str, str] | None = None) -> list[Notifier]:
    """환경변수를 보고 사용 가능한 채널만 만든다."""
    source = env if env is not None else dict(os.environ)
    notifiers: list[Notifier] = []

    webhook = (source.get(ENV_DISCORD_WEBHOOK) or "").strip()
    if webhook:
        notifiers.append(DiscordNotifier(webhook))
        log.info("알림 채널 활성화: Discord")

    token = (source.get(ENV_TELEGRAM_TOKEN) or "").strip()
    chat_id = (source.get(ENV_TELEGRAM_CHAT_ID) or "").strip()
    if token and chat_id:
        notifiers.append(TelegramNotifier(token, chat_id))
        log.info("알림 채널 활성화: Telegram")
    elif token or chat_id:
        log.warning(
            "Telegram 설정이 반쪽이다 (%s=%s, %s=%s). 둘 다 있어야 활성화된다.",
            ENV_TELEGRAM_TOKEN,
            "설정됨" if token else "없음",
            ENV_TELEGRAM_CHAT_ID,
            "설정됨" if chat_id else "없음",
        )

    return notifiers
