"""登录轮询器判定逻辑测试（批次 2，2026-10-14）。

login_watch.py 跑在 Mac 上、每 20 秒被 launchd 拉起一次。它是整条登录链的
调度中枢：决定何时拉起 Pi、维护 `.login_in_flight` 哨兵、给请求落终态。没有测试
的话，它出问题只会在生产里以「登录卡住」或「main_round 全被挡住」的形式暴露。

这里只测**判定**——拉起 Pi 的那一步被替换掉，绝不真的跑一轮。
"""

import sys
import time
from pathlib import Path

import pytest

from app.login_request import PHASE_EXPIRED, PHASE_FAILED, RedisLoginRequest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "agent"))

import login_watch  # noqa: E402
import run_round  # noqa: E402

TEST_REDIS_URL = "redis://127.0.0.1:6379/15"


@pytest.fixture()
def watch(monkeypatch, tmp_path):
    """把轮询器指向 15 号库 + 临时哨兵/锁文件，并拦掉真正的 Pi 拉起。"""
    store = RedisLoginRequest(TEST_REDIS_URL)
    store.clear()
    monkeypatch.setattr(login_watch, "RedisLoginRequest", lambda: store)
    monkeypatch.setattr(login_watch, "SENTINEL", tmp_path / ".login_in_flight")
    monkeypatch.setattr(run_round, "LOCK_FILE", tmp_path / ".round.lock")

    launched: list[list[str]] = []
    monkeypatch.setattr(login_watch.subprocess, "call", lambda cmd: launched.append(cmd) or 0)

    yield store, launched, tmp_path
    store.clear()


def _hold_round_lock(tmp_path: Path) -> None:
    """伪造「另一轮次正在跑」：锁里写当前进程的 PID（它是活的）。"""
    (tmp_path / ".round.lock").write_text(str(__import__("os").getpid()), encoding="utf-8")


def test_no_request_clears_sentinel(watch):
    store, launched, tmp_path = watch
    (tmp_path / ".login_in_flight").write_text("stale", encoding="utf-8")

    assert login_watch.main() == 0
    assert not (tmp_path / ".login_in_flight").exists()
    assert launched == []


def test_pending_launches_round_and_sets_sentinel(watch):
    store, launched, tmp_path = watch
    store.start(account_id=7)

    assert login_watch.main() == 0
    assert len(launched) == 1
    assert launched[0][-1] == "login_round"
    assert (tmp_path / ".login_in_flight").exists()
    assert store.get()["attempts"] == 1


def test_in_progress_keeps_sentinel_but_does_not_launch(watch):
    """dispatched / verifying = Pi 正在跑，轮询器只维持哨兵，不重复拉起。"""
    store, launched, tmp_path = watch
    store.start(account_id=7)
    store.set_phase("dispatched")

    assert login_watch.main() == 0
    assert launched == []
    assert (tmp_path / ".login_in_flight").exists()
    assert store.get()["attempts"] == 0


def test_does_not_burn_attempt_when_another_round_is_running(watch):
    """撞上轮次锁**不算**一次尝试。

    否则连续三次撞锁就会把用户一次正常的登录判死——这正是「先 bump 再判断」的写法
    会踩的坑。
    """
    store, launched, tmp_path = watch
    store.start(account_id=7)
    _hold_round_lock(tmp_path)

    assert login_watch.main() == 0
    assert launched == []
    assert store.get()["attempts"] == 0
    assert store.get()["phase"] == "pending"


def test_attempt_cap_stops_relaunching(watch):
    store, launched, tmp_path = watch
    store.start(account_id=7)
    for _ in range(3):
        store.bump_attempts()

    assert login_watch.main() == 0
    assert launched == []
    assert store.get()["phase"] == PHASE_FAILED
    assert not (tmp_path / ".login_in_flight").exists()


def test_timeout_marks_expired(watch):
    store, launched, tmp_path = watch
    store.start(account_id=7)
    store._redis.hset("pipeline:state:login_req", "expires_at", "1")

    assert login_watch.main() == 0
    assert store.get()["phase"] == PHASE_EXPIRED
    assert launched == []
    assert not (tmp_path / ".login_in_flight").exists()


def test_terminal_phase_clears_sentinel_without_launching(watch):
    store, launched, tmp_path = watch
    store.start(account_id=7)
    store.set_phase("success")

    assert login_watch.main() == 0
    assert launched == []
    assert not (tmp_path / ".login_in_flight").exists()


def test_stale_sentinel_does_not_block_main_round(monkeypatch, tmp_path):
    """轮询器挂掉后，陈旧哨兵最多堵住 LOGIN_SENTINEL_MAX_AGE，不能是永久。"""
    sentinel = tmp_path / ".login_in_flight"
    sentinel.write_text("7 pending", encoding="utf-8")
    monkeypatch.setattr(run_round, "LOGIN_IN_FLIGHT_FILE", sentinel)

    assert run_round.login_in_flight() is True

    old = time.time() - run_round.LOGIN_SENTINEL_MAX_AGE - 60
    import os

    os.utime(sentinel, (old, old))
    assert run_round.login_in_flight() is False
