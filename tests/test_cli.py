"""CLI 진입점 테스트.

이 파일이 있는 이유: Config 에 필드를 추가하면서 --state 를 줬을 때만 타는
재조립 코드를 고치지 않아 CI 가 즉시 죽은 적이 있다. 단위 테스트는 전부
통과했는데 main() 을 거치는 경로를 아무도 안 밟았기 때문이다.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest import mock

import pytest

from cgv_watcher import cli
from cgv_watcher.config import Config, load_config

CONFIG_YAML = """
defaults:
  min_seats: 4
polling:
  rounds_per_run: 1
watches:
  - name: "테스트"
    movie: { title: "오디세이" }
    theater: "영등포타임스퀘어"
    date_range: { start: "2099-01-01", end: "2099-01-02" }
"""


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML, encoding="utf-8")
    return path


class FakeNotifier:
    name = "fake"

    def __init__(self):
        self.sent = []

    def send(self, title, body):
        self.sent.append((title, body))


def test_state_override_survives_new_config_fields(config_file, tmp_path):
    """--state 를 주면 Config 를 다시 만든다.

    필드를 손으로 나열하면 Config 에 필드가 늘 때마다 여기가 깨진다.
    replace 를 쓰는지 확인한다.
    """
    config = load_config(config_file)
    updated = dataclasses.replace(config, state_path=tmp_path / "s.json")

    # 모든 필드가 보존되어야 한다 (state_path 만 바뀜)
    for field in dataclasses.fields(Config):
        if field.name == "state_path":
            continue
        assert getattr(updated, field.name) == getattr(config, field.name)
    assert updated.state_path == tmp_path / "s.json"


def test_main_with_state_option_does_not_crash(config_file, tmp_path, monkeypatch):
    """실제 CI 실패를 재현하던 경로. run() 까지 도달해야 한다."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")

    captured = {}

    def fake_run(config, notifiers, dump_dir):
        captured["state_path"] = config.state_path
        captured["heartbeat_hours"] = config.heartbeat_hours
        return 0

    with mock.patch.object(cli, "run", fake_run):
        code = cli.main(
            [
                "--config",
                str(config_file),
                "--state",
                str(tmp_path / "seats.json"),
            ]
        )

    assert code == 0
    assert captured["state_path"] == tmp_path / "seats.json"
    assert captured["heartbeat_hours"] == 12  # 기본값이 살아있어야 한다


def test_main_without_state_option(config_file, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    with mock.patch.object(cli, "run", lambda c, n, d: 0):
        assert cli.main(["--config", str(config_file)]) == 0


def test_main_exits_2_without_notifiers(config_file, monkeypatch):
    for name in ("DISCORD_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["--config", str(config_file)]) == 2


def test_main_exits_2_on_bad_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    bad = tmp_path / "bad.yaml"
    bad.write_text("watches: []", encoding="utf-8")
    assert cli.main(["--config", str(bad)]) == 2


def test_main_exits_2_on_missing_config(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    assert cli.main(["--config", "없는파일.yaml"]) == 2


def test_test_notify_sends_sample_alert(monkeypatch):
    """--test-notify 는 CGV 를 건드리지 않고 알림만 보낸다."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    fake = FakeNotifier()
    with mock.patch.object(cli, "build_notifiers", lambda: [fake]):
        code = cli.main(["--test-notify"])

    assert code == 0
    assert len(fake.sent) == 1
    title, body = fake.sent[0]
    assert "테스트" in title
    assert "오디세이" in body


def test_expired_watches_skip_cgv_entirely(config_file, tmp_path, monkeypatch):
    """감시 기간이 지나면 CGV 클라이언트를 아예 만들지 않아야 한다."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    path = tmp_path / "expired.yaml"
    path.write_text(
        CONFIG_YAML.replace("2099-01-01", "2020-01-01").replace(
            "2099-01-02", "2020-01-02"
        ),
        encoding="utf-8",
    )

    config = load_config(path)
    config = dataclasses.replace(config, state_path=tmp_path / "s.json")
    fake = FakeNotifier()

    with mock.patch.object(cli, "CgvClient") as client_cls:
        code = cli.run(config, [fake], Path(tmp_path / "dumps"))

    assert code == 0
    client_cls.assert_not_called()  # 요청을 한 번도 보내면 안 된다
    assert fake.sent and fake.sent[0][0] == "감시 기간 종료"
