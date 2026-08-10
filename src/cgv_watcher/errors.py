"""예외 정의.

조용한 실패를 막는 게 목적이다. 여기 정의된 예외는 모두 상위로 전파되어
로그 + 알림 + 종료코드 1 로 이어진다.
"""

from __future__ import annotations


class WatcherError(Exception):
    """이 프로젝트에서 발생하는 모든 예외의 최상위."""


class ConfigError(WatcherError):
    """config.yaml 이 잘못됐다. 재시도해도 소용없으므로 즉시 종료한다."""


class FetchError(WatcherError):
    """HTTP 계층 실패. 재시도 대상."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimitedError(FetchError):
    """429 / 403. 백오프 후 재시도."""


class ParseError(WatcherError):
    """응답 구조가 기대와 다르다.

    CGV가 API를 바꿨다는 뜻이므로 절대 조용히 넘기지 않는다.
    raw 응답 일부를 함께 실어 보내 디버깅에 쓴다.
    """

    def __init__(self, message: str, raw: object = None) -> None:
        super().__init__(message)
        self.raw = raw
