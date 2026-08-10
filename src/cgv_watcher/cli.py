"""실행 오케스트레이션.

한 번의 실행(= Actions job 1회) 안에서:
  1. state 복원
  2. 지점명 -> siteNo 해석
  3. rounds_per_run 만큼 폴링 (라운드 사이 랜덤 간격)
  4. 전이 감지 -> 알림
  5. state 저장

GitHub Actions 스케줄은 지연될 수 있어서, 한 번 깨어났을 때 짧게 여러 번
폴링해 실효 감지 주기를 줄인다.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from .cgv_client import CgvClient
from .config import Config, WatchRule, load_config
from .errors import ConfigError, FetchError, ParseError, WatcherError
from .matcher import matches, required_seats, resolve_site_no
from .models import KST, Showtime
from .notify import (
    Notifier,
    build_failure_text,
    build_heartbeat_text,
    build_listed_text,
    build_notifiers,
    build_seat_text,
    redact_secrets,
)
from .parser import parse_screening_dates, parse_showtimes, parse_sites
from .ratelimit import Throttle
from .state import KIND_LISTED, StateStore, Transition

log = logging.getLogger("cgv_watcher")

FAILURE_ALERT_COOLDOWN_MINUTES = 60


class _RedactingFilter(logging.Filter):
    """모든 로그 레코드에서 비밀값을 지운다.

    우리 코드만 조심해서는 부족하다. urllib3 는 DEBUG 레벨에서 요청 경로를
    통째로 찍는데, 디스코드 웹훅과 텔레그램 봇 토큰은 경로 안에 들어 있다.
    로그가 만들어지는 마지막 지점에서 일괄 처리한다.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = redact_secrets(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def _setup_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(_RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    # --verbose 를 켜도 HTTP 라이브러리는 조용히 시킨다.
    # urllib3 의 DEBUG 로그는 요청 URL 전체(= 토큰 포함)를 남긴다.
    # 위 필터가 한 번 더 막지만, 애초에 만들지 않는 편이 낫다.
    for name in ("urllib3", "requests", "charset_normalizer", "chardet"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _dump_raw(client: CgvClient, dump_dir: Path, label: str) -> Path | None:
    """파싱이 깨졌을 때 원문을 남긴다. 조용히 실패하지 않기 위한 것."""
    raw = client.last_raw_response
    if not raw:
        return None
    dump_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(KST).strftime("%Y%m%d-%H%M%S")
    path = dump_dir / f"raw-{label}-{stamp}.json"
    path.write_text(raw[:1_000_000], encoding="utf-8")
    log.error("원문 응답을 저장했다: %s", path)
    return path


def _notify_all(notifiers: list[Notifier], title: str, body: str) -> bool:
    """모든 채널에 보낸다. 한 채널이 실패해도 나머지는 시도한다."""
    ok = False
    for notifier in notifiers:
        try:
            notifier.send(title, body)
            log.info("[%s] 알림 전송 완료: %s", notifier.name, title)
            ok = True
        except Exception as exc:  # 알림 실패가 감시를 죽이면 안 된다
            # 예외 메시지에 토큰이나 웹훅 URL이 섞일 수 있다.
            # public 레포의 Actions 로그는 누구나 볼 수 있으므로 반드시 가린다.
            log.error(
                "[%s] 알림 전송 실패: %s", notifier.name, redact_secrets(str(exc))
            )
    return ok


def _deliver(
    notifiers: list[Notifier],
    store: StateStore,
    transitions,
    pending: set,
) -> None:
    """감지 즉시 알린다.

    라운드가 끝날 때마다 바로 보내는 게 중요하다. 실행이 50분씩 이어지므로
    끝에 모아서 보내면 첫 라운드에 잡은 좌석이 50분 뒤에야 전달된다.

    모든 채널에서 실패하면 그 회차의 관측을 되돌려 다음 라운드에 재시도한다.
    pending 은 아직 전달하지 못한 회차 키의 집합이며, 나중 라운드에서 재시도가
    성공하면 빠진다. 이렇게 해야 일시적인 장애로 재시도에 성공한 실행이
    실패로 잘못 표시되지 않는다.
    """
    for transition in transitions:
        if transition.kind == KIND_LISTED:
            title, body = build_listed_text(transition)
        else:
            title, body = build_seat_text(transition)

        key = transition.showtime.key
        if _notify_all(notifiers, title, body):
            pending.discard(key)
            continue

        pending.add(key)
        store.rollback(transition)
        log.error(
            "알림 전달 실패 — 모든 채널에서 실패: %s %s %s",
            transition.showtime.movie_name,
            transition.showtime.display_date(),
            transition.showtime.display_time(),
        )


def _collect_targets(
    config: Config, sites: dict[str, str]
) -> dict[str, list[WatchRule]]:
    """siteNo -> 그 지점을 보는 규칙들."""
    targets: dict[str, list[WatchRule]] = defaultdict(list)
    for rule in config.watches:
        site_no = resolve_site_no(rule, sites)
        targets[site_no].append(rule)
    return dict(targets)


def _dates_to_check(rules: list[WatchRule], available: list[str]) -> list[str]:
    """규칙의 날짜 범위와 실제 예매 오픈 날짜의 교집합.

    오픈도 안 된 날짜에 요청을 던지지 않으려는 것 — 요청 수를 줄인다.
    """
    wanted: set[str] = set()
    for ymd in available:
        try:
            day = datetime.strptime(ymd, "%Y%m%d").date()
        except ValueError:
            continue
        if any(rule.date_range.contains(day) for rule in rules):
            wanted.add(ymd)
    return sorted(wanted)


def _run_round(
    client: CgvClient,
    store: StateStore,
    config: Config,
    targets: dict[str, list[WatchRule]],
    date_cache: dict[str, list[str]],
    dump_dir: Path,
) -> tuple[list[Transition], list[tuple[Showtime, int]]]:
    """폴링 1라운드.

    (전이된 회차, 이번 라운드에서 조건에 맞은 회차 전부)를 반환한다.
    두 번째 값은 생존 신고에 현재 현황을 싣는 데 쓴다.
    """
    transitions: list[Transition] = []
    snapshot: list[tuple[Showtime, int]] = []
    now = datetime.now(KST)

    for site_no, rules in targets.items():
        if site_no not in date_cache:
            try:
                date_cache[site_no] = parse_screening_dates(
                    client.fetch_screening_dates(site_no)
                )
            except ParseError:
                _dump_raw(client, dump_dir, f"dates-{site_no}")
                raise

        dates = _dates_to_check(rules, date_cache[site_no])
        if not dates:
            log.info("지점 %s: 감시 범위에 해당하는 상영일 없음", site_no)
            continue

        for ymd in dates:
            try:
                showtimes = parse_showtimes(client.fetch_showtimes(site_no, ymd))
            except ParseError:
                _dump_raw(client, dump_dir, f"showtimes-{site_no}-{ymd}")
                raise

            log.info("지점 %s %s: 회차 %d건 수신", site_no, ymd, len(showtimes))

            # 같은 회차를 여러 규칙이 볼 수 있다. min_seats는 가장 느슨한 값을 쓴다.
            matched: dict[str, tuple[Showtime, int, list[str]]] = {}
            for showtime in showtimes:
                if showtime.start_dt <= now:
                    continue  # 이미 시작한 회차는 볼 필요 없다
                for rule in rules:
                    if not matches(showtime, rule):
                        continue
                    # 규칙마다 요구 좌석이 다르다(일반관은 비율 조건이 붙는 등).
                    # 같은 회차를 여러 규칙이 보면 가장 느슨한 쪽을 따른다.
                    need = required_seats(showtime, rule)
                    existing = matched.get(showtime.key)
                    if existing is None:
                        matched[showtime.key] = (showtime, need, [rule.name])
                    else:
                        _, prev_need, names = existing
                        names.append(rule.name)
                        matched[showtime.key] = (
                            showtime,
                            min(prev_need, need),
                            names,
                        )

            for showtime, min_seats, names in matched.values():
                snapshot.append((showtime, min_seats))
                transition = store.evaluate(
                    showtime=showtime,
                    min_seats=min_seats,
                    rule_names=tuple(names),
                    cooldown_minutes=config.cooldown_minutes,
                    notify_on_first_seen=config.notify_on_first_seen,
                    notify_on_listed=config.notify_on_listed,
                    now=now,
                )
                if transition:
                    transitions.append(transition)

    return transitions, snapshot


def _all_watches_expired(config: Config, today) -> bool:
    """모든 규칙의 날짜 범위가 지났는지."""
    return all(rule.date_range.end < today for rule in config.watches)


def run(config: Config, notifiers: list[Notifier], dump_dir: Path) -> int:
    store = StateStore(config.state_path)
    store.load()

    # 감시 기간이 끝났으면 CGV에 요청하지 않는다. 잡을 게 없는데 5분마다
    # 계속 두드릴 이유가 없다. 조용히 도는 대신 만료됐다고 알린다.
    today = datetime.now(KST).date()
    if _all_watches_expired(config, today):
        log.warning(
            "config.yaml 의 모든 date_range 가 지났다(오늘 %s). CGV 조회를 건너뛴다.",
            today,
        )
        if store.should_alert_expiry():
            last_day = max(rule.date_range.end for rule in config.watches)
            _notify_all(
                notifiers,
                "감시 기간 종료",
                f"config.yaml 의 감시 기간이 {last_day} 로 모두 지났습니다.\n"
                f"이제 아무것도 잡히지 않습니다.\n\n"
                f"계속 쓰시려면 config.yaml 의 date_range 를 새 날짜로 바꾸세요.\n"
                f"당분간 안 쓰시면 Actions 탭에서 워크플로를 비활성화하세요\n"
                f"(… 메뉴 → Disable workflow).",
            )
            store.expiry_alert_at = datetime.now(KST).isoformat(timespec="seconds")
        store.save()
        return 0

    first_run = not store.loaded_from_disk
    if first_run:
        log.warning(
            "이전 state가 없다. 이번 실행은 현재 좌석 상황을 기준선으로 기록만 하고 "
            "알림은 보내지 않는다(notify_on_first_seen=%s).",
            config.notify_on_first_seen,
        )

    throttle = Throttle(config.polling.request_delay_sec)
    transitions: list[Transition] = []
    last_snapshot: list[tuple[Showtime, int]] = []
    pending_delivery: set = set()
    failure: Exception | None = None

    with CgvClient(throttle) as client:
        try:
            sites = parse_sites(client.fetch_sites())
            log.info("지점 목록 %d개 확보", len(sites))

            targets = _collect_targets(config, sites)
            log.info(
                "감시 대상 지점 %d곳 / 규칙 %d개", len(targets), len(config.watches)
            )

            date_cache: dict[str, list[str]] = {}

            duration = config.polling.run_duration_minutes
            deadline = (
                datetime.now(KST) + timedelta(minutes=duration) if duration else None
            )
            if deadline:
                log.info(
                    "%d분 동안 약 %.0f초 간격으로 감시한다 (종료 예정 %s)",
                    duration,
                    sum(config.polling.round_interval_sec) / 2,
                    deadline.strftime("%H:%M"),
                )

            round_index = 0
            while True:
                if round_index:
                    gap = random.uniform(*config.polling.round_interval_sec)
                    # 남은 시간보다 대기가 길면 더 돌지 않고 끝낸다.
                    if deadline and datetime.now(KST) + timedelta(seconds=gap) >= deadline:
                        log.info("남은 시간이 부족해 감시를 마친다")
                        break
                    log.info("라운드 간 대기 %.0fs", gap)
                    time.sleep(gap)

                round_index += 1
                log.info("=== 라운드 %d ===", round_index)
                round_transitions, snapshot = _run_round(
                    client, store, config, targets, date_cache, dump_dir
                )
                transitions += round_transitions
                # 마지막 라운드의 현황이 가장 최신이다.
                last_snapshot = snapshot

                # 감지 즉시 발송한다. 실행이 50분 이어지므로 끝에 모아 보내면
                # 첫 라운드에 잡은 좌석이 50분 뒤에 전달된다.
                _deliver(notifiers, store, round_transitions, pending_delivery)

                # 오래 도는 실행에서 job 이 중간에 죽어도 여기까지의 관측을
                # 잃지 않도록 라운드마다 저장한다. 파일이 작아 비용은 무시할 만하다.
                store.save()

                if deadline is None and round_index >= config.polling.rounds_per_run:
                    break
                if deadline and datetime.now(KST) >= deadline:
                    break

            store.failure_consecutive = 0
            store.failure_last_alert_at = None

        except (FetchError, ParseError) as exc:
            failure = exc
            store.failure_consecutive += 1
            log.exception("감시 실패 (연속 %d회)", store.failure_consecutive)

    store.prune(datetime.now(KST).date())

    if failure is not None:
        if store.should_alert_failure(
            threshold=config.failure_threshold,
            cooldown_minutes=FAILURE_ALERT_COOLDOWN_MINUTES,
        ):
            title, body = build_failure_text(store.failure_consecutive, str(failure))
            if _notify_all(notifiers, title, body):
                store.failure_last_alert_at = datetime.now(KST).isoformat(
                    timespec="seconds"
                )
        store.save()
        return 1

    # 생존 신고. 알림이 없는 게 정상인지 죽은 것인지 구분하기 위한 것이므로
    # 정상 실행에서만 보낸다(실패는 별도의 실패 알림이 담당한다).
    now = datetime.now(KST)
    if store.should_send_heartbeat(config.heartbeat_hours, now=now):
        title, body = build_heartbeat_text(
            rows=last_snapshot,
            last_watch_day=max(rule.date_range.end for rule in config.watches),
            checked_at=now.strftime("%Y-%m-%d %H:%M KST"),
        )
        if _notify_all(notifiers, title, body):
            store.heartbeat_at = now.isoformat(timespec="seconds")
        else:
            # 보내지 못했으면 heartbeat_at 을 갱신하지 않는다.
            # 다음 실행에서 다시 시도한다.
            log.warning("생존 신고를 보내지 못했다. 다음 실행에서 재시도한다.")

    store.save()

    if pending_delivery:
        log.error(
            "완료. 전이 %d건 중 %d건을 끝내 보내지 못함.",
            len(transitions),
            len(pending_delivery),
        )
        return 1

    log.info("완료. 전이 %d건 감지, 미전달 없음.", len(transitions))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cgv-watcher", description="CGV 잔여 좌석 감시 (알림 전용)"
    )
    parser.add_argument("--config", default="config.yaml", help="config.yaml 경로")
    parser.add_argument(
        "--state", default=None, help="state 파일 경로 (기본: state/seats.json)"
    )
    parser.add_argument(
        "--dump-dir", default="dumps", help="파싱 실패 시 원문을 저장할 디렉터리"
    )
    parser.add_argument(
        "--test-notify",
        action="store_true",
        help="설정된 알림 채널로 테스트 메시지만 보내고 종료",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    notifiers = build_notifiers()
    if not notifiers:
        log.error(
            "알림 채널이 하나도 설정되지 않았다. "
            "DISCORD_WEBHOOK_URL 또는 (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) 를 "
            "repo secrets 에 등록할 것."
        )
        return 2

    if args.test_notify:
        # 실제 좌석 알림과 같은 형식으로 보낸다. 연결 확인과 동시에
        # 진짜 자리가 났을 때 어떤 메시지를 받게 되는지 미리 보여준다.
        sample = Transition(
            showtime=Showtime(
                site_no="0059",
                site_name="CGV 영등포타임스퀘어",
                screen_no="017",
                screen_name="IMAX관",
                screen_grade="아이맥스",
                movie_no="30001323",
                movie_name="오디세이",
                movie_kind="IMAX LASER 2D",
                rating="15세이상관람가",
                play_date="20260815",
                start_hhmm="1730",
                end_hhmm="2032",
                seq="4",
                free_seats=2,
                total_seats=387,
            ),
            previous_free=0,
            min_seats=1,
            rule_names=("★ 영등포 IMAX",),
        )
        title, body = build_seat_text(sample)
        body = (
            "⚠️ 이것은 테스트 메시지입니다. 실제 좌석이 난 것이 아닙니다.\n"
            "아래는 진짜 자리가 났을 때 받게 될 알림의 형식입니다.\n"
            "──────────────\n" + body
        )
        ok = _notify_all(notifiers, title + " (테스트)", body)
        return 0 if ok else 1

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("config 오류: %s", exc)
        return 2

    if args.state:
        # replace 를 쓴다. 필드를 손으로 나열하면 Config 에 필드를 추가할 때마다
        # 여기를 같이 고쳐야 하고, 잊으면 --state 를 준 실행만 터진다(실제로 겪음).
        config = replace(config, state_path=Path(args.state))

    try:
        return run(config, notifiers, Path(args.dump_dir))
    except ConfigError as exc:
        log.error("config 오류: %s", exc)
        return 2
    except WatcherError as exc:
        log.exception("치명적 오류: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
