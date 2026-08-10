"""텔레그램 봇 알림."""

from __future__ import annotations

import html
import logging
import time

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

# 텔레그램 메시지 상한은 4096자.
MAX_MESSAGE = 3900


class TelegramNotifier:
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, timeout: float = 10.0) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._timeout = timeout

    def send(self, title: str, body: str) -> None:
        is_failure = "실패" in title
        prefix = "⚠️" if is_failure else "🎟️"
        text = f"<b>{prefix} {html.escape(title)}</b>\n\n{html.escape(body)}"

        url = f"{API_BASE}/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text[:MAX_MESSAGE],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        for attempt in range(3):
            response = requests.post(url, json=payload, timeout=self._timeout)
            if response.status_code == 429:
                try:
                    wait = float(
                        response.json().get("parameters", {}).get("retry_after", 1.0)
                    )
                except (ValueError, AttributeError):
                    wait = 1.0
                log.warning("Telegram 레이트리밋. %.1fs 대기 후 재시도", wait)
                time.sleep(min(wait, 30.0))
                continue
            if response.status_code >= 400:
                # 토큰이 본문에 섞여 로그로 새지 않도록 URL은 찍지 않는다.
                raise RuntimeError(
                    f"Telegram 전송 실패: HTTP {response.status_code} {response.text[:200]}"
                )
            return

        raise RuntimeError("Telegram 전송 실패: 레이트리밋으로 3회 재시도 후 포기")
