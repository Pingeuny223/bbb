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


def test_expiry_alert_respects_cooldown(store):
    """감시 기간 만료 알림은 하루 1회만."""
    assert store.should_alert_expiry(now=NOW)
    store.expiry_alert_at = NOW.isoformat(timespec="seconds")
    assert not store.should_alert_expiry(now=NOW + timedelta(hours=5))
    assert store.should_alert_expiry(now=NOW + timedelta(hours=25))


def test_expiry_marker_survives_roundtrip(tmp_path):
    path = tmp_path / "seats.json"
    first = StateStore(path)
    first.expiry_alert_at = NOW.isoformat(timespec="seconds")
    first.save()

    second = StateStore(path)
    second.load()
    assert second.expiry_alert_at == NOW.isoformat(timespec="seconds")


def test_heartbeat_interval(store):
    """생존 신고는 지정한 시간 간격으로만."""
    assert store.should_send_heartbeat(12, now=NOW)  # 최초는 항상 보냄
    store.heartbeat_at = NOW.isoformat(timespec="seconds")
    assert not store.should_send_heartbeat(12, now=NOW + timedelta(hours=6))
    assert store.should_send_heartbeat(12, now=NOW + timedelta(hours=13))


def test_heartbeat_can_be_disabled(store):
    assert not store.should_send_heartbeat(0, now=NOW)


# -- 편성 등록 알림 (예매 오픈 전) --------------------------------------------


def evaluate_listed(store, showtime, min_seats=4, now=NOW, listed=True):
    return store.evaluate(
        showtime=showtime, min_seats=min_seats, rule_names=("돌비",),
        cooldown_minutes=30, notify_on_first_seen=True,
        notify_on_listed=listed, now=now,
    )


def _warm(tmp_path):
    """이전 실행이 있는 state (콜드 스타트가 아닌 상태)."""
    path = tmp_path / "seats.json"
    StateStore(path).save()
    store = StateStore(path)
    store.load()
    assert store.loaded_from_disk
    return store


def test_listed_notice_fires_for_new_zero_seat_showtime(tmp_path):
    """편성만 등록된(0석) 회차는 '편성 등록'으로 알린다."""
    from cgv_watcher.state import KIND_LISTED

    store = _warm(tmp_path)
    tr = evaluate_listed(store, make_showtime(0))
    assert tr is not None
    assert tr.kind == KIND_LISTED


def test_listed_notice_is_sent_only_once(tmp_path):
    store = _warm(tmp_path)
    assert evaluate_listed(store, make_showtime(0)) is not None
    assert evaluate_listed(store, make_showtime(0), now=NOW + timedelta(minutes=3)) is None


def test_listed_notice_does_not_swallow_the_real_opening(tmp_path):
    """핵심 회귀 테스트.

    등록 알림이 last_notified_at 을 갱신해 버리면, 몇 분 뒤 실제로 예매가
    열렸을 때 쿨다운(30분)에 걸려 정작 필요한 알림이 통째로 묻힌다.
    """
    from cgv_watcher.state import KIND_LISTED, KIND_SEAT

    store = _warm(tmp_path)
    first = evaluate_listed(store, make_showtime(0))
    assert first.kind == KIND_LISTED

    # 6분 뒤 예매 오픈 — 쿨다운 30분 안이지만 반드시 알려야 한다
    opened = evaluate_listed(store, make_showtime(195), now=NOW + timedelta(minutes=6))
    assert opened is not None, "등록 알림이 예매 오픈 알림을 삼키면 안 된다"
    assert opened.kind == KIND_SEAT
    assert opened.previous_free == 0


def test_listed_notice_disabled_by_default(tmp_path):
    store = _warm(tmp_path)
    assert evaluate_listed(store, make_showtime(0), listed=False) is None


def test_listed_notice_not_sent_on_cold_start(tmp_path):
    """콜드 스타트에서는 모든 0석 회차가 '신규'라 도배된다."""
    store = StateStore(tmp_path / "seats.json")
    assert not store.loaded_from_disk
    assert evaluate_listed(store, make_showtime(0)) is None


def test_partially_sold_new_showtime_is_not_a_listing(tmp_path):
    """새로 보이지만 좌석이 조금 있는 회차는 '편성 등록'이 아니다.

    0석일 때만 '아직 예매 불가'로 본다.
    """
    store = _warm(tmp_path)
    assert evaluate_listed(store, make_showtime(2)) is None


# -- 전송 실패 되돌리기 -------------------------------------------------------


def test_rollback_lets_failed_alert_retry(tmp_path):
    """알림 전송이 실패하면 다음 라운드에 다시 감지돼야 한다.

    evaluate 는 '알리기로 결정한 시점'에 state 를 갱신한다. 전송이 실패했는데
    되돌리지 않으면 다음 라운드에는 전이가 성립하지 않아 그 알림이 영구히
    사라진다 — 정작 자리가 났을 때 놓치는 최악의 경우다.
    """
    store = _warm(tmp_path)
    evaluate_listed(store, make_showtime(0))

    opened = evaluate_listed(store, make_showtime(195), now=NOW + timedelta(minutes=3))
    assert opened is not None

    # 전송 실패 → 되돌림
    store.rollback(opened)

    retry = evaluate_listed(store, make_showtime(195), now=NOW + timedelta(minutes=6))
    assert retry is not None, "되돌린 뒤에는 같은 전이를 다시 감지해야 한다"
    assert retry.previous_free == 0


def test_rollback_of_brand_new_showtime_removes_it(tmp_path):
    """이전에 본 적 없는 회차는 되돌리면 아예 없던 것이 된다."""
    store = _warm(tmp_path)
    tr = evaluate_listed(store, make_showtime(0))
    assert tr is not None
    assert make_showtime(0).key in store.showtimes

    store.rollback(tr)
    assert make_showtime(0).key not in store.showtimes

    again = evaluate_listed(store, make_showtime(0), now=NOW + timedelta(minutes=3))
    assert again is not None, "되돌렸으므로 다시 신규로 감지돼야 한다"


def test_rollback_restores_exact_previous_observation(tmp_path):
    store = _warm(tmp_path)
    evaluate_listed(store, make_showtime(2), min_seats=10)  # 조용히 기록만
    before = store.showtimes[make_showtime(2).key]

    tr = evaluate_listed(store, make_showtime(50), min_seats=10, now=NOW + timedelta(minutes=3))
    assert tr is not None
    store.rollback(tr)

    assert store.showtimes[make_showtime(2).key] == before


# -- 좌석 감소 경고 (alert_below_ratio) ----------------------------------------


def evaluate_below(store, showtime, ratio=0.65, now=NOW, min_seats=4):
    return store.evaluate(
        showtime=showtime, min_seats=min_seats, rule_names=("돌비",),
        cooldown_minutes=30, notify_on_first_seen=True, notify_on_listed=True,
        alert_below_ratio=ratio, now=now,
    )


def dolby(free, total=195):
    return Showtime(
        site_no="0059", site_name="CGV 영등포타임스퀘어", screen_no="004",
        screen_name="4관[DOLBY ATMOS] (Laser)", screen_grade="DOLBY ATMOS",
        movie_no="30001323", movie_name="오디세이", movie_kind="2D", rating="15세",
        play_date="20260815", start_hhmm="1920", end_hhmm="2222", seq="4",
        free_seats=free, total_seats=total,
    )


def test_filling_alert_fires_on_downward_crossing(tmp_path):
    """195석의 65% = 126.75석. 127 → 126 으로 내려올 때 울린다."""
    from cgv_watcher.state import KIND_FILLING

    store = _warm(tmp_path)
    evaluate_below(store, dolby(130))  # 기준선 위 — 조용
    tr = evaluate_below(store, dolby(126), now=NOW + timedelta(minutes=3))
    assert tr is not None
    assert tr.kind == KIND_FILLING
    assert tr.previous_free == 130


def test_filling_alert_fires_only_once_per_showtime(tmp_path):
    """기준선 근처에서 오르내려도 반복되지 않아야 한다."""
    store = _warm(tmp_path)
    evaluate_below(store, dolby(130))
    assert evaluate_below(store, dolby(126), now=NOW + timedelta(minutes=3)) is not None

    for i, free in enumerate([130, 126, 120, 128, 100], start=2):
        assert evaluate_below(
            store, dolby(free), now=NOW + timedelta(minutes=3 * i + 3)
        ) is None, f"{free}석에서 다시 울리면 안 된다"


def test_filling_flag_survives_save_and_load(tmp_path):
    path = tmp_path / "seats.json"
    store = _warm(tmp_path)
    evaluate_below(store, dolby(130))
    evaluate_below(store, dolby(126), now=NOW + timedelta(minutes=3))
    store.save()

    reloaded = StateStore(path)
    reloaded.load()
    assert evaluate_below(
        reloaded, dolby(120), now=NOW + timedelta(minutes=9)
    ) is None, "재시작 후에도 이미 알린 회차는 다시 울리면 안 된다"


def test_already_below_threshold_never_triggers_filling(tmp_path):
    """이미 기준선 한참 아래인 회차는 더 줄어도 감소 경고가 없다.

    지금의 IMAX(0~6석 / 387석)와 5관(32~48석 / 320석)이 이 경우다.
    '아래로 내려오는 순간'을 잡는 것이지 '아래에 있음'을 잡는 게 아니다.
    """
    from cgv_watcher.state import KIND_SEAT

    store = _warm(tmp_path)

    # 처음 보는 회차라 '신규 편성'으로 좌석 알림이 나가는 건 정상이다.
    first = evaluate_below(store, dolby(48, total=320))
    assert first is not None and first.kind == KIND_SEAT

    # 이후 계속 줄어들어도 감소 경고는 나오지 않는다.
    for i, free in enumerate([44, 40, 32, 20], start=1):
        assert evaluate_below(
            store, dolby(free, total=320), now=NOW + timedelta(minutes=3 * i)
        ) is None


def test_filling_alert_disabled_when_ratio_zero(tmp_path):
    store = _warm(tmp_path)
    evaluate_below(store, dolby(130), ratio=0.0)
    assert evaluate_below(store, dolby(126), ratio=0.0, now=NOW + timedelta(minutes=3)) is None


def test_seat_alert_still_wins_over_filling(tmp_path):
    """0석 → 좌석 발생은 감소 경고보다 우선한다."""
    from cgv_watcher.state import KIND_SEAT

    store = _warm(tmp_path)
    evaluate_below(store, dolby(0))
    tr = evaluate_below(store, dolby(195), now=NOW + timedelta(minutes=3))
    assert tr is not None and tr.kind == KIND_SEAT
