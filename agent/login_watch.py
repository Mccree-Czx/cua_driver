#!/usr/bin/env python3
"""登录轮询器（Mac 侧，launchd 每 20 秒拉起一次，见 com.hr-workbuddy.pi-login）。

职责有三，都只跟 Redis 打交道：

1. **发现登录请求**：phase 是 pending / awaiting_code 时拉起一轮 `login_round`。
2. **维护 `.login_in_flight` 哨兵**：run_round.py 只用标准库、读不了 Redis，靠这个
   文件知道「手机可能停在登录页」。没有它，main_round 会把登录页误判成掉登录，
   触发 risk_set() 造成全系统假熔断。
3. **过期与重试封顶**：登录请求超时、或拉起次数用尽，都由这里落终态——轮询器
   总在跑，Pi 不一定在。

20 秒（而不是 60 秒）是因为用户填完验证码后要等这个间隔才被提交。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent

# 必须先切到仓库根再导入 app.*：pydantic-settings 的 env_file=".env" 是**相对 CWD**
# 解析的。launchd 的工作目录是 agent/，在那儿读到的是默认值——Redis 会退化成
# 127.0.0.1:6379，而它在 CT104 上。这种错法只在定时任务里出现，手动跑永远看不到。
os.chdir(REPO_ROOT)


_REEXEC_FLAG = "HR_LOGIN_WATCH_REEXEC"


def _reexec_under_venv() -> None:
    """当前解释器没有 redis 时，换仓库 venv 重新执行自己。

    launchd 拉起的是系统 python3，它没有 pipeline 的依赖。与其在 plist 里写死
    解释器路径（换台机器就失效），不如让脚本自己找。
    """
    try:
        import redis  # noqa: F401
    except ImportError:
        pass
    else:
        return
    # 换过一次还是没有，就直接失败——否则两个同样缺 redis 的解释器会互相
    # execv 个没完，而这是个每 20 秒被拉起一次的后台任务，死循环没人看得见。
    if os.environ.get(_REEXEC_FLAG):
        raise SystemExit("已换过解释器，仍然没有 redis——无法读写登录请求状态")
    for cand in (
        REPO_ROOT / ".venv" / "bin" / "python",
        REPO_ROOT / ".venv" / "bin" / "python3",
        Path("/opt/homebrew/bin/python3"),
    ):
        if cand.exists() and Path(cand).resolve() != Path(sys.executable).resolve():
            os.environ[_REEXEC_FLAG] = "1"
            os.execv(str(cand), [str(cand), __file__, *sys.argv[1:]])
    raise SystemExit("找不到带 redis 的 python——无法读写登录请求状态")


_reexec_under_venv()

sys.path.insert(0, str(REPO_ROOT / "services" / "pipeline"))
sys.path.insert(0, str(AGENT_DIR))

import run_round  # noqa: E402  同目录的轮次运行器（借用它的锁判定，避免复制一份逻辑）
from app.login_request import (  # noqa: E402
    LAUNCHABLE_PHASES,
    MAX_ATTEMPTS,
    PHASE_EXPIRED,
    PHASE_FAILED,
    TERMINAL_PHASES,
    RedisLoginRequest,
)

SENTINEL = run_round.LOGIN_IN_FLIGHT_FILE


def _round_running() -> bool:
    """有轮次正在跑吗？判定与 run_round.acquire_round_lock 同源，不另写一套。"""
    try:
        age = time.time() - run_round.LOCK_FILE.stat().st_mtime
        pid = int(run_round.LOCK_FILE.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return False
    return run_round._pid_alive(pid) and age < run_round.LOCK_STALE_SECONDS


def _write_sentinel(req: dict) -> None:
    try:
        SENTINEL.write_text(f"{req['account_id']} {req['phase']}", encoding="utf-8")
    except OSError:
        pass


def _clear_sentinel() -> None:
    try:
        SENTINEL.unlink(missing_ok=True)
    except OSError:
        pass


def main() -> int:
    store = RedisLoginRequest()
    req = store.get()

    if req is None:
        _clear_sentinel()
        return 0

    if req["phase"] in TERMINAL_PHASES:
        _clear_sentinel()
        return 0

    # 超时：轮询器是唯一能可靠判定它的地方
    if req["expires_at"] and req["expires_at"] < time.time():
        print("[login_watch] 登录请求超时，置为 expired")
        store.set_phase(PHASE_EXPIRED, "登录请求超时（30 分钟内未完成）")
        _clear_sentinel()
        return 0

    _write_sentinel(req)

    if req["phase"] not in LAUNCHABLE_PHASES:
        # dispatched / verifying：Pi 正在跑，等它自己推进
        return 0

    if req["attempts"] >= MAX_ATTEMPTS:
        print(f"[login_watch] 已拉起 {req['attempts']} 次仍未推进，置为 failed")
        store.set_phase(
            PHASE_FAILED,
            f"已自动尝试 {MAX_ATTEMPTS} 次仍未完成，为避免空烧 token 已停止重试；"
            "请重试或人工在手机上登录",
        )
        _clear_sentinel()
        return 0

    if _round_running():
        # 有轮次在跑。**不能在这里 bump_attempts**——那会把「碰到锁」算作一次失败尝试，
        # 三次撞锁就把用户正常的登录判死了。
        return 0

    store.bump_attempts()
    print(f"[login_watch] 拉起 login_round（第 {req['attempts'] + 1} 次，phase={req['phase']}）")
    return subprocess.call(
        [sys.executable, str(AGENT_DIR / "run_round.py"), "login_round"]
    )


if __name__ == "__main__":
    sys.exit(main())
