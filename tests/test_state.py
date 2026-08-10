"""전이 판정과 중복 알림 방지 테스트."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from cgv_watcher.models import KST, Showtime
from cgv_watcher.state import StateStore

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=KST)


def make_showtime(free: int, seq: str = "1") -> Showtime:
    return Showtime(
        site_no="0059",
        site_name="CGV 영등포타임스퀘어",
        screen_no="017",
        screen_name="IMAX관",
        screen_grade="아이맥스",
        movie_no="30001323",
        movie_name="오디세이",
        movie_kind="IMAX 2D",
        rating="15세이상관람가",
        play_date="20260811",
        start_hhmm="1730",
        end_hhmm="2032",
        seq=seq,
        free_seats=free,
        total_seats=387,
    )


def evaluate(store, showtime, min_seats=1, cooldown=30, first_seen=False, now=NOW):
    return store.evaluate(
        showtime=showtime,
        min_seats=min_seats,
        rule_names=("테스트",),
        cooldown_minutes=cooldown,
        notify_on_first_seen=first_seen,
        now=now,
    )


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "seats.json")


def test_first_sight_does_not_notify(store):
    """첫 실행에 조건 맞는 회차가 전부 터지는 걸 막는다."""
    assert evaluate(store, make_showtime(50)) is None


def test_cold_start_never_notifies_even_when_opted_in(store):
    """이전 state가 없으면 모든 회차가 '신규'다. 도배를 막는다."""
    assert not store.loaded_from_disk
    assert evaluate(store, make_showtime(50), first_seen=True) is None


def test_new_showtime_notifies_when_state_exists(tmp_path):
    """편성이 새로 추가된 경우. 이전 state가 있을 때만 알린다."""
    path = tmp_path / "seats.json"
    first = StateStore(path)
    evaluate(first, make_showtime(10, seq="1"), first_seen=True)
    first.save()

    second = StateStore(path)
    second.load()
    assert second.loaded_from_disk

    # seq="2" 는 이전 실행에 없던 새 회차 = 편성 추가
    transition = evaluate(second, make_showtime(120, seq="2"), first_seen=True)
    assert transition is not None
    assert transition.previous_free is None


def test_new_showtime_ignored_when_opted_out(tmp_path):
    path = tmp_path / "seats.json"
    first = StateStore(path)
    evaluate(first, make_showtime(10, seq="1"))
    first.save()

    second = StateStore(path)
    second.load()
    assert evaluate(second, make_showtime(120, seq="2"), first_seen=False) is None


def test_zero_to_available_notifies(store):
    evaluate(store, make_showtime(0))
    transition = evaluate(store, make_showtime(3), now=NOW + timedelta(minutes=5))
    assert transition is not None
    assert transition.previous_free == 0
    assert transition.showtime.free_seats == 3


def test_still_zero_does_not_notify(store):
    evaluate(store, make_showtime(0))
    assert evaluate(store, make_showtime(0), now=NOW + timedelta(minutes=5)) is None


def test_available_to_available_does_not_notify(store):
    """이미 열려 있던 회차는 좌석이 늘어도 다시 알리지 않는다."""
    evaluate(store, make_showtime(5))
    assert evaluate(store, make_showtime(9), now=NOW + timedelta(minutes=5)) is None


def test_min_seats_gate(store):
    """min_seats=2 면 1석만 났을 때는 알리지 않는다."""
    evaluate(store, make_showtime(0), min_seats=2)
    assert evaluate(store, make_showtime(1), min_seats=2, now=NOW + timedelta(minutes=5)) is None
    assert evaluate(store, make_showtime(2), min_seats=2, now=NOW + timedelta(minutes=10)) is not None


def test_cooldown_suppresses_flapping(store):
    """0↔1을 오갈 때 알림 도배를 막는다."""
    evaluate(store, make_showtime(0))
    assert evaluate(store, make_showtime(1), now=NOW + timedelta(minutes=1)) is not None

    # 다시 0으로 떨어졌다가 곧바로 1이 되면, 쿨다운(30분) 안이므로 억제된다.
    evaluate(store, make_showtime(0), now=NOW + timedelta(minutes=2))
    assert evaluate(store, make_showtime(1), now=NOW + timedelta(minutes=3)) is None


def test_cooldown_expires(store):
    """쿨다운이 지난 뒤 다시 0→N 전이가 일어나면 알린다."""
    evaluate(store, make_showtime(0))
    assert evaluate(store, make_showtime(1), now=NOW + timedelta(minutes=1)) is not None

    evaluate(store, make_showtime(0), now=NOW + timedelta(minutes=40))
    assert evaluate(store, make_showtime(1), now=NOW + timedelta(minutes=45)) is not None


def test_state_survives_save_and_load(tmp_path):
    path = tmp_path / "seats.json"
    first = StateStore(path)
    evaluate(first, make_showtime(0))
    first.save()

    second = StateStore(path)
    second.load()
    assert second.loaded_from_disk
    transition = evaluate(second, make_showtime(4), now=NOW + timedelta(minutes=5))
    assert transition is not None, "복원된 state로 0→N 전이를 감지해야 한다"


def test_corrupt_state_does_not_crash(tmp_path):
    path = tmp_path / "seats.json"
    path.write_text("{ 깨진 JSON", encoding="utf-8")
    store = StateStore(path)
    store.load()
    assert not store.loaded_from_disk
    assert store.showtimes == {}


def test_prune_removes_past_showtimes(store):
    evaluate(store, make_showtime(1))
    assert len(store.showtimes) == 1
    store.prune(datetime(2026, 8, 20, tzinfo=KST).date())
    assert store.showtimes == {}


def test_prune_keeps_today(store):
    evaluate(store, make_showtime(1))
    store.prune(datetime(2026, 8, 11, tzinfo=KST).date())
    assert len(store.showtimes) == 1


def test_failure_alert_threshold_and_cooldown(store):
    store.failure_consecutive = 2
    assert not store.should_alert_failure(threshold=3, cooldown_minutes=60, now=NOW)

    store.failure_consecutive = 3
    assert store.should_alert_failure(threshold=3, cooldown_minutes=60, now=NOW)

    store.failure_last_alert_at = NOW.isoformat(timespec="seconds")
    assert not store.should_alert_failure(
        threshold=3, cooldown_minutes=60, now=NOW + timedelta(minutes=30)
    )
    assert store.should_alert_failure(
        threshold=3, cooldown_minutes=60, now=NOW + timedelta(minutes=90)
    )
