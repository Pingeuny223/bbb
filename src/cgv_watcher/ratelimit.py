"""요청 간격 제어와 백오프.

원칙:
  - 동시 요청 없음. 모든 요청은 한 줄로 순차 실행된다.
  - 요청 사이에 랜덤 지연을 넣는다.
  - 429/403은 지수 백오프. 우회는 하지 않는다.
"""

from __future__ import annotations

import logging
import random
import time

log = logging.getLogger(__name__)


class Throttle:
    """요청 사이 랜덤 지연을 강제한다.

    첫 요청은 지연 없이 통과시키고, 이후 매 요청 전에 delay_range 안에서
    무작위로 뽑은 시간만큼 쉰다.
    """

    def __init__(self, delay_range: tuple[float, float]) -> None:
        low, high = delay_range
        if low < 0 or high < low:
            raise ValueError(f"잘못된 지연 범위: {delay_range}")
        self._low = low
        self._high = high
        self._primed = False

    def wait(self) -> None:
        if not self._primed:
            self._primed = True
            return
        delay = random.uniform(self._low, self._high)
        log.debug("요청 간 지연 %.2fs", delay)
        time.sleep(delay)


def backoff_delay(attempt: int, base: float = 2.0, cap: float = 60.0) -> float:
    """attempt(0부터)에 대한 지수 백오프 + 지터.

    0 -> ~2s, 1 -> ~4s, 2 -> ~8s ... cap에서 멈춘다.
    지터는 여러 워크플로가 같은 분에 깨어났을 때 동시에 재시도하는 걸 막는다.
    """
    raw = min(base * (2**attempt), cap)
    return raw * random.uniform(0.8, 1.2)


class FailureTracker:
    """실행 간에 누적되는 연속 실패 카운터.

    state에 저장돼서 러너가 초기화돼도 이어진다. 임계치를 넘으면 알림을 보내되,
    알림 자체가 도배되지 않도록 자체 쿨다운을 둔다.
    """

    def __init__(self, consecutive: int = 0, last_alert_at: str | None = None) -> None:
        self.consecutive = consecutive
        self.last_alert_at = last_alert_at

    def record_success(self) -> None:
        self.consecutive = 0

    def record_failure(self) -> None:
        self.consecutive += 1

    def extra_delay(self) -> float:
        """연속 실패가 쌓이면 다음 실행의 시작 지연을 늘린다."""
        if self.consecutive == 0:
            return 0.0
        return min(5.0 * self.consecutive, 60.0)
