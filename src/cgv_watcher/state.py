"""회차별 이전 상태 저장/복원 및 전이 판정.

GitHub Actions 러너는 매 실행마다 초기화되므로 이 파일이 만드는 JSON을
actions/cache 로 실행 간에 실어 나른다(README '상태 유지' 참고).

state가 유실되면 최악의 경우 이미 열린 좌석을 한 번 더 알리는 정도이고,
알림을 놓치지는 않는다. 그래서 캐시 방식을 택했다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import KST, SeatState, Showtime

log = logging.getLogger(__name__)

STATE_VERSION = 1


@dataclass
class Transition:
    """알림 대상이 된 상태 변화 하나."""

    showtime: Showtime
    previous_free: int | None
    min_seats: int
    rule_names: tuple[str, ...]


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.showtimes: dict[str, SeatState] = {}
        self.failure_consecutive: int = 0
        self.failure_last_alert_at: str | None = None
        self.expiry_alert_at: str | None = None
        self.loaded_from_disk: bool = False

    # -- 입출력 ----------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            log.info("이전 state 없음 (%s). 이번 실행은 기준선만 기록한다.", self.path)
            return

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # state가 깨졌다고 실행을 멈추지는 않는다. 기준선을 다시 잡으면 된다.
            log.warning("state 읽기 실패 (%s). 새로 시작한다.", exc)
            return

        if not isinstance(payload, dict):
            log.warning("state 형식이 잘못됨. 새로 시작한다.")
            return

        version = payload.get("version")
        if version != STATE_VERSION:
            log.warning("state 버전 불일치 (%r != %r). 새로 시작한다.", version, STATE_VERSION)
            return

        for key, value in (payload.get("showtimes") or {}).items():
            if not isinstance(value, dict):
                continue
            try:
                self.showtimes[key] = SeatState(
                    free_seats=int(value["free_seats"]),
                    total_seats=int(value.get("total_seats", 0)),
                    last_seen_at=str(value.get("last_seen_at", "")),
                    last_notified_at=value.get("last_notified_at"),
                )
            except (KeyError, TypeError, ValueError):
                log.warning("state 항목 '%s' 이(가) 손상됨. 건너뜀", key)

        failures = payload.get("failures") or {}
        if isinstance(failures, dict):
            try:
                self.failure_consecutive = int(failures.get("consecutive", 0))
            except (TypeError, ValueError):
                self.failure_consecutive = 0
            last = failures.get("last_alert_at")
            self.failure_last_alert_at = str(last) if last else None

        expiry = payload.get("expiry_alert_at")
        self.expiry_alert_at = str(expiry) if expiry else None

        self.loaded_from_disk = True
        log.info("state 복원: 회차 %d건, 연속 실패 %d회", len(self.showtimes), self.failure_consecutive)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
            "showtimes": {
                key: {
                    "free_seats": value.free_seats,
                    "total_seats": value.total_seats,
                    "last_seen_at": value.last_seen_at,
                    "last_notified_at": value.last_notified_at,
                }
                for key, value in sorted(self.showtimes.items())
            },
            "failures": {
                "consecutive": self.failure_consecutive,
                "last_alert_at": self.failure_last_alert_at,
            },
            "expiry_alert_at": self.expiry_alert_at,
        }
        # 쓰다 죽어도 기존 파일이 반쯤 덮이지 않도록 임시 파일 후 교체.
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(self.path)
        log.info("state 저장: %s (회차 %d건)", self.path, len(self.showtimes))

    # -- 판정 ------------------------------------------------------------

    def evaluate(
        self,
        showtime: Showtime,
        min_seats: int,
        rule_names: tuple[str, ...],
        cooldown_minutes: int,
        notify_on_first_seen: bool,
        now: datetime | None = None,
    ) -> Transition | None:
        """이 회차가 알림 대상인지 판정하고, state를 갱신한다.

        알림 조건: 직전 잔여석이 min_seats 미만이었다가 min_seats 이상이 됐을 때.
        min_seats=1 이면 정확히 '0 -> 1 이상' 전이가 된다.
        """
        now = now or datetime.now(KST)
        now_text = now.isoformat(timespec="seconds")
        previous = self.showtimes.get(showtime.key)

        should_notify = False
        previous_free: int | None = None

        if previous is None:
            # 처음 본 회차 = 편성이 새로 추가됐다는 뜻이다.
            #
            # 단, 이전 state가 없는 콜드 스타트(첫 실행, 캐시 유실)에서는
            # '모든' 회차가 처음 보는 것이므로 알리지 않는다. 안 그러면
            # 조건에 맞는 회차 전부가 한꺼번에 터진다.
            if (
                notify_on_first_seen
                and self.loaded_from_disk
                and showtime.free_seats >= min_seats
            ):
                should_notify = True
        else:
            previous_free = previous.free_seats
            if previous.free_seats < min_seats <= showtime.free_seats:
                should_notify = True

        last_notified = previous.last_notified_at if previous else None

        if should_notify and last_notified and cooldown_minutes > 0:
            try:
                previous_dt = datetime.fromisoformat(last_notified)
                if now - previous_dt < timedelta(minutes=cooldown_minutes):
                    log.info(
                        "쿨다운으로 알림 생략: %s (%s 에 알림함)",
                        showtime.key,
                        last_notified,
                    )
                    should_notify = False
            except ValueError:
                pass  # 값이 깨졌으면 그냥 알린다.

        self.showtimes[showtime.key] = SeatState(
            free_seats=showtime.free_seats,
            total_seats=showtime.total_seats,
            last_seen_at=now_text,
            last_notified_at=now_text if should_notify else last_notified,
        )

        if not should_notify:
            return None

        return Transition(
            showtime=showtime,
            previous_free=previous_free,
            min_seats=min_seats,
            rule_names=rule_names,
        )

    def prune(self, today: date) -> int:
        """지난 회차를 정리한다. state 파일이 무한정 커지는 걸 막는다."""
        cutoff = today - timedelta(days=1)
        stale = []
        for key in self.showtimes:
            # key = siteNo|scnsNo|YYYYMMDD|seq
            parts = key.split("|")
            if len(parts) != 4 or len(parts[2]) != 8 or not parts[2].isdigit():
                stale.append(key)
                continue
            try:
                play_date = datetime.strptime(parts[2], "%Y%m%d").date()
            except ValueError:
                stale.append(key)
                continue
            if play_date < cutoff:
                stale.append(key)

        for key in stale:
            del self.showtimes[key]
        if stale:
            log.info("지난 회차 %d건 정리", len(stale))
        return len(stale)

    def should_alert_expiry(
        self, cooldown_minutes: int = 1440, now: datetime | None = None
    ) -> bool:
        """감시 기간 만료 알림을 보낼 시점인지. 기본 하루 1회."""
        now = now or datetime.now(KST)
        if self.expiry_alert_at and cooldown_minutes > 0:
            try:
                previous = datetime.fromisoformat(self.expiry_alert_at)
                if now - previous < timedelta(minutes=cooldown_minutes):
                    return False
            except ValueError:
                pass
        return True

    def should_alert_failure(
        self, threshold: int, cooldown_minutes: int, now: datetime | None = None
    ) -> bool:
        """연속 실패 알림을 보낼 시점인지."""
        if self.failure_consecutive < threshold:
            return False
        now = now or datetime.now(KST)
        if self.failure_last_alert_at and cooldown_minutes > 0:
            try:
                previous = datetime.fromisoformat(self.failure_last_alert_at)
                if now - previous < timedelta(minutes=cooldown_minutes):
                    return False
            except ValueError:
                pass
        return True
