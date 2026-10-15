"""登录轮询器判定逻辑测试（批次 2，2026-10-14）。

login_watch.py 跑在 Mac 上、每 20 秒被 launchd 拉起一次。它是整条登录链的
调度中枢：决定何时驱动手机、维护 `.login_in_flight` 哨兵、给请求落终态。没有测试
的话，它出问题只会在生产里以「登录卡住」或「main_round 全被挡住」的形式暴露。

这里只测**判定**——真正的手机操作（flows/phone）被整体替换掉，绝不碰手机。
重构后它不再拉起 Pi，而是直接跑确定性序列（见 login_watch 的模块 docstring）。
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

# 抢占的打桩状态。**绝不能让它真的去杀进程**——这些测试跑在 pytest 自己的进程里，
# 而「撞锁」的测试把 pytest 的 PID 写进了锁文件；真跑抢占会把测试进程本身 SIGTERM 掉。
PREEMPT = {"ok": True, "count": 0}


@pytest.fixture()
def watch(monkeypatch, tmp_path):
    """把轮询器指向 15 号库 + 临时哨兵/锁文件，并拦掉真正的 Pi 拉起。"""
    store = RedisLoginRequest(TEST_REDIS_URL)
    store.clear()
    monkeypatch.setattr(login_watch, "RedisLoginRequest", lambda: store)
    monkeypatch.setattr(login_watch, "SENTINEL", tmp_path / ".login_in_flight")
    monkeypatch.setattr(run_round, "LOCK_FILE", tmp_path / ".round.lock")

    # 记录真正被驱动的那一步，而不是真的碰手机
    calls: list[tuple] = []
    monkeypatch.setattr(login_watch, "_account_phone", lambda _id: "13800000000")
    monkeypatch.setattr(
        login_watch.flows, "login_code",
        lambda number, **kw: calls.append(("login_code", number)) or {"ok": True},
    )
    monkeypatch.setattr(
        login_watch.flows, "submit_code",
        lambda code: calls.append(("submit_code", code)) or {"ok": True},
    )
    monkeypatch.setattr(login_watch.phone, "home", lambda *a, **k: None)
    monkeypatch.setattr(login_watch, "mark_account_logged_in", lambda _id: None)

    PREEMPT["ok"] = True
    PREEMPT["count"] = 0

    def _fake_preempt() -> bool:
        PREEMPT["count"] += 1
        return PREEMPT["ok"]

    monkeypatch.setattr(login_watch, "_preempt_round", _fake_preempt)

    yield store, calls, tmp_path
    store.clear()


def _hold_round_lock(tmp_path: Path) -> None:
    """伪造「另一轮次正在跑」：锁里写当前进程的 PID（它是活的）。"""
    (tmp_path / ".round.lock").write_text(str(__import__("os").getpid()), encoding="utf-8")


def test_no_request_clears_sentinel(watch):
    store, calls, tmp_path = watch
    (tmp_path / ".login_in_flight").write_text("stale", encoding="utf-8")

    assert login_watch.main() == 0
    assert not (tmp_path / ".login_in_flight").exists()
    assert calls == []


def test_pending_drives_login_and_sets_sentinel(watch):
    """pending → 直接跑确定性登录序列（不经过 LLM），并把相位推到 awaiting_code。"""
    store, calls, tmp_path = watch
    store.start(account_id=7)

    assert login_watch.main() == 0
    assert calls == [("login_code", "13800000000")]
    assert (tmp_path / ".login_in_flight").exists()
    assert store.get()["attempts"] == 1
    assert store.get()["phase"] == "awaiting_code"


def test_awaiting_code_submits_the_claimed_code(watch):
    """awaiting_code 且已填码 → 取码并提交，成功后落地为生效账号。"""
    store, calls, tmp_path = watch
    store.start(account_id=7)
    store.submit_code("123456")

    assert login_watch.main() == 0
    assert calls == [("submit_code", "123456")]
    assert store.get()["phase"] == "success"
    assert not (tmp_path / ".login_in_flight").exists()


def test_awaiting_code_without_a_code_just_waits(watch):
    """`awaiting_code` 但还没码 = **在等人填**，什么都不做。

    ★ 这条以前断言的是「落 failed」——那正是 2026-10-15 真机第一次跑一键切换时
      踩到的坑：轮询器一看到 awaiting_code 就动手，claim_code 拿到 None，
      于是把一次正常等待判成「验证码不可用」。**每次登录都会这样死**。
      `awaiting_code` 同时表示「在等人」和「码已就绪」两种状态，判据必须是
      「有没有码」（`needs_action`），不是光看相位。
    """
    store, calls, tmp_path = watch
    store.start(account_id=7)
    store.set_phase("awaiting_code")

    assert login_watch.main() == 0
    assert calls == []
    assert store.get()["phase"] == "awaiting_code"   # 还在等，没被判死
    assert store.get()["attempts"] == 0               # 也不该白烧一次尝试


def test_flow_mismatch_fails_with_reason(watch, monkeypatch):
    """锚点失配要说清「看到了什么」，不能只说失败——否则没法判断是锚点过期还是状态不对。"""
    store, calls, tmp_path = watch
    store.start(account_id=7)

    def boom(*_a, **_kw):
        raise login_watch.flows.FlowMismatch("下一步", "找不到「下一步」按钮", ["输入验证码"])

    monkeypatch.setattr(login_watch.flows, "login_code", boom)
    assert login_watch.main() == 0
    detail = store.get()["error"]
    assert "下一步" in detail and "输入验证码" in detail


def test_in_progress_keeps_sentinel_but_does_not_launch(watch):
    """dispatched / verifying = Pi 正在跑，轮询器只维持哨兵，不重复拉起。"""
    store, calls, tmp_path = watch
    store.start(account_id=7)
    store.set_phase("dispatched")

    assert login_watch.main() == 0
    assert calls == []
    assert (tmp_path / ".login_in_flight").exists()
    assert store.get()["attempts"] == 0


def test_preempts_the_round_in_flight_and_drives(watch):
    """用户当场发起的登录比定时轮次更急 —— 撞上轮次在跑就**抢占**，而不是排队等。

    等一轮可能要几十分钟（接口降级时今天有过 47 分钟的一轮），用户点一下不该等那么久。
    """
    store, calls, tmp_path = watch
    store.start(account_id=7)
    _hold_round_lock(tmp_path)

    assert login_watch.main() == 0
    assert PREEMPT["count"] == 1                       # 确实去抢了
    assert calls == [("login_code", "13800000000")]     # 抢到后照常驱动
    assert store.get()["phase"] == "awaiting_code"


def test_gives_up_without_burning_attempt_when_preempt_fails(watch):
    """抢不到就本轮放弃，**且不计尝试次数**。

    否则三次抢不到就把用户一次正常的登录判死了——这和以前「撞锁不计」是同一个道理。
    """
    store, calls, tmp_path = watch
    PREEMPT["ok"] = False
    store.start(account_id=7)
    _hold_round_lock(tmp_path)

    assert login_watch.main() == 0
    assert calls == []
    assert store.get()["attempts"] == 0
    assert store.get()["phase"] == "pending"


def test_attempt_cap_stops_relaunching(watch):
    store, calls, tmp_path = watch
    store.start(account_id=7)
    for _ in range(3):
        store.bump_attempts()

    assert login_watch.main() == 0
    assert calls == []
    assert store.get()["phase"] == PHASE_FAILED
    assert not (tmp_path / ".login_in_flight").exists()


def test_timeout_marks_expired(watch):
    store, calls, tmp_path = watch
    store.start(account_id=7)
    store._redis.hset("pipeline:state:login_req", "expires_at", "1")

    assert login_watch.main() == 0
    assert store.get()["phase"] == PHASE_EXPIRED
    assert calls == []
    assert not (tmp_path / ".login_in_flight").exists()


def test_terminal_phase_clears_sentinel_without_launching(watch):
    store, calls, tmp_path = watch
    store.start(account_id=7)
    store.set_phase("success")

    assert login_watch.main() == 0
    assert calls == []
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
