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
            try:
                response = requests.post(
                    self._webhook_url, json=payload, timeout=self._timeout
                )
            except requests.RequestException as exc:
                # requests 예외 메시지에는 요청 URL이 그대로 들어간다.
                # 웹훅 URL은 경로에 토큰이 들어있는 인증 수단이고, public 레포의
                # Actions 로그는 누구나 볼 수 있다. 원본 메시지는 버리고
                # 예외 종류만 남긴다. (from None 으로 체인도 끊는다)
                raise RuntimeError(
                    f"Discord 웹훅 전송 실패: {type(exc).__name__}"
                ) from None
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
