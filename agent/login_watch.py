#!/usr/bin/env python3
"""登录轮询器（Mac 侧，launchd 每 20 秒拉起一次，见 com.hr-workbuddy.pi-login）。

职责有三：

1. **发现登录请求并驱动手机**：phase 是 pending / awaiting_code 时，跑
   `flows.py` 里的确定性序列（**不经过 LLM**）。
   以前这里是 `subprocess.call(run_round.py login_round)` —— 一个 6 次点击的固定序列
   要穿过 Redis → 轮询器 → run_round → Pi(LLM) → MCP → adb，DeepSeek 一返 503 就全停，
   而且首字能等 284 秒。改成直接跑 flow 后：秒级、每步 dump 可核对、**接口挂了照样能登录**。
   `login_round` 保留为人工兜底入口（见 rounds.md §4），不再自动拉起。
2. **维护 `.login_in_flight` 哨兵**：run_round.py 只用标准库、读不了 Redis，靠这个
   文件知道「手机可能停在登录页」。没有它，main_round 会把登录页误判成掉登录，
   触发 risk_set() 造成全系统假熔断。
3. **过期与重试封顶**：登录请求超时、或尝试次数用尽，都由这里落终态——轮询器
   总在跑，Pi 不一定在。

20 秒（而不是 60 秒）是因为用户填完验证码后要等这个间隔才被提交。
"""

from __future__ import annotations

import os
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
import flows  # noqa: E402  确定性驱动：登录/退出的固定序列
import phone  # noqa: E402  adb 原语与锚点匹配
from app.login_request import (  # noqa: E402
    LAUNCHABLE_PHASES,
    MAX_ATTEMPTS,
    MODE_AUTO,
    PHASE_AWAITING_CODE,
    PHASE_EXPIRED,
    PHASE_FAILED,
    PHASE_PENDING,
    PHASE_SUCCESS,
    TERMINAL_PHASES,
    RedisLoginRequest,
    mark_account_logged_in,
)

SENTINEL = run_round.LOGIN_IN_FLIGHT_FILE

# 是否允许代用户点「同意并继续」（独立的《服务协议及个人信息保护》同意页）。
# ★ 那是代做**法律同意**，所以默认关；确认过之后再开。
# （登录表单里那个「我已阅读并同意」勾选框是流程必经步骤，不受此开关影响。）
AGREE_TERMS = os.environ.get("HR_AGREE_TERMS", "").strip() in {"1", "true", "yes"}


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

    # 人工模式：轮询器**一律不碰手机**，由操作者驱动（脚本或人工）。
    # 但上面的哨兵照常维护——手动模式下手机同样会停在登录页，
    # main_round 跑起来照样会误判成「掉登录」。
    # 前端在两种模式下完全一样：照常显示进度、照常弹验证码框。
    if req["mode"] != MODE_AUTO:
        return 0

    if req["phase"] not in LAUNCHABLE_PHASES:
        # verifying：流程正在跑，等它自己推进
        return 0

    if req["attempts"] >= MAX_ATTEMPTS:
        print(f"[login_watch] 已尝试 {req['attempts']} 次仍未推进，置为 failed")
        store.set_phase(
            PHASE_FAILED,
            f"已自动尝试 {MAX_ATTEMPTS} 次仍未完成，为避免反复空跑已停止重试；"
            "请重试或人工在手机上登录",
        )
        _clear_sentinel()
        return 0

    if _round_running():
        # 有轮次在跑。**不能在这里 bump_attempts**——那会把「碰到锁」算作一次失败尝试，
        # 三次撞锁就把用户正常的登录判死了。
        return 0

    store.bump_attempts()
    if req["phase"] == PHASE_PENDING:
        return _drive_login(store, req)
    return _drive_submit(store, req)


def _fail(store, message: str) -> int:
    print(f"[login_watch] 停手：{message}")
    store.set_phase(PHASE_FAILED, message)
    _clear_sentinel()
    phone.home()
    return 0


def _account_phone(account_id: int) -> str | None:
    """取账号登记的手机号。抽成独立函数便于测试打桩（否则驱动测试要连真库）。"""
    from app.db import SessionLocal
    from app.models import Account

    with SessionLocal() as session:
        account = session.get(Account, account_id)
        return account.phone if account else None


def _drive_login(store, req: dict) -> int:
    """phase=pending：输手机号 → 勾协议 → 点「下一步」把验证码发出去。

    这一步跑的是确定性脚本，**不经过 LLM**——所以 DeepSeek 接口挂了也照样能登录，
    而且秒级完成、每步 dump 可核对。
    """
    number = _account_phone(req["account_id"])
    if not number:
        return _fail(store, f"账号 {req['account_id']} 没有登记手机号，无法登录")

    print(f"[login_watch] 驱动登录：账号 {req['account_id']}，手机尾号 {number[-4:]}")
    try:
        flows.login_code(number, agree=AGREE_TERMS)
    except flows.FlowMismatch as exc:
        return _fail(store, f"{exc}")
    except phone.AdbError as exc:
        return _fail(store, f"adb 出错：{exc}")

    store.set_phase(PHASE_AWAITING_CODE, "")
    print("[login_watch] 验证码已发出 → 前端应弹出输入框")
    return 0


def _drive_submit(store, req: dict) -> int:
    """phase=awaiting_code：取人工填的码，输进去提交。"""
    code = store.claim_code()
    if not code:
        return _fail(store, "验证码不可用（未填、已用过或已过期）")

    print(f"[login_watch] 提交验证码（{len(code)} 位）")
    try:
        flows.submit_code(code)
    except flows.FlowMismatch as exc:
        return _fail(store, f"{exc}")
    except phone.AdbError as exc:
        return _fail(store, f"adb 出错：{exc}")

    # 与 Pi 那条路共用同一份落地实现
    mark_account_logged_in(req["account_id"])
    store.set_phase(PHASE_SUCCESS, "")
    print(f"[login_watch] ✅ 登录成功，已把账号 {req['account_id']} 置为生效账号")
    _clear_sentinel()
    return 0


if __name__ == "__main__":
    sys.exit(main())
