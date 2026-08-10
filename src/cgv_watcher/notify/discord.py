"""Discord 웹훅 알림."""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

COLOR_SEAT = 0x2ECC71  # 초록
COLOR_FAILURE = 0xE74C3C  # 빨강

# Discord embed description 상한은 4096자. 여유를 두고 자른다.
MAX_DESCRIPTION = 3900


class DiscordNotifier:
    name = "discord"

    def __init__(self, webhook_url: str, timeout: float = 10.0) -> None:
        self._webhook_url = webhook_url
        self._timeout = timeout

    def send(self, title: str, body: str) -> None:
        is_failure = "실패" in title
        payload = {
            "embeds": [
                {
                    "title": ("⚠️ " if is_failure else "🎟️ ") + title,
                    "description": body[:MAX_DESCRIPTION],
                    "color": COLOR_FAILURE if is_failure else COLOR_SEAT,
                }
            ]
        }

        # Discord 웹훅은 초과 시 429 + retry_after 를 준다. 그 값을 존중한다.
        for attempt in range(3):
            response = requests.post(
                self._webhook_url, json=payload, timeout=self._timeout
            )
            if response.status_code == 429:
                try:
                    wait = float(response.json().get("retry_after", 1.0))
                except (ValueError, AttributeError):
                    wait = 1.0
                log.warning("Discord 레이트리밋. %.1fs 대기 후 재시도", wait)
                time.sleep(min(wait, 30.0))
                continue
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Discord 웹훅 실패: HTTP {response.status_code} {response.text[:200]}"
                )
            return

        raise RuntimeError("Discord 웹훅 실패: 레이트리밋으로 3회 재시도 후 포기")
