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
import signal
import subprocess
import sys
import time
import traceback
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
from app.job_sync import sync_scanned_jobs  # noqa: E402
from app.login_request import (  # noqa: E402
    KIND_SWITCH,
    MAX_ATTEMPTS,
    PHASE_AWAITING_CODE,
    PHASE_DISPATCHED,
    PHASE_EXPIRED,
    PHASE_FAILED,
    PHASE_LOGGING_OUT,
    PHASE_PENDING,
    PHASE_SCANNING_JOBS,
    PHASE_SUCCESS,
    TERMINAL_PHASES,
    RedisLoginRequest,
    mark_account_logged_in,
    needs_action,
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


def _kill_tree(pid: int, sig: int) -> None:
    """先杀子进程，再杀父进程。

    ★ `run_round.py` 用 `subprocess.run` 拉起 node(Pi)——**父进程死了子进程不会跟着死**。
      只杀父的话 Pi 会变成孤儿继续跑，手机还被它占着，抢占等于没做。
    """
    try:
        out = subprocess.run(
            ["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=10
        ).stdout
        kids = [int(x) for x in out.split() if x.isdigit()]
    except (OSError, ValueError, subprocess.SubprocessError):
        kids = []
    for kid in kids:
        try:
            os.kill(kid, sig)
        except OSError:
            pass
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def _record_preempted(pid: int) -> None:
    """给被硬中断的那一轮记一笔账。

    这是对「可能重复触达」的补偿：让人事后知道**有一轮被中断了**，该去核对什么。
    """
    try:
        from datetime import datetime

        from app.db import SessionLocal
        from app.models import TaskLog

        with SessionLocal() as session:
            session.add(
                TaskLog(
                    task_id="preempted_" + datetime.now().strftime("%Y%m%d%H%M%S"),
                    outcome="preempted",
                    tokens=0,
                    cost=0.0,
                )
            )
            session.commit()
    except Exception:  # noqa: BLE001  记账失败不该影响抢占
        pass


def _preempt_round() -> bool:
    """掐掉正在跑的轮次，把手机抢过来。返回「锁是否已释放」。

    ★ 硬中断的代价（用户已知悉并选择）：可能打断一次**已经点出去、但还没写
      `interaction_log`** 的触达。数据库里没记录 → 下一轮会认为这个候选人没打过招呼
      → **重复打招呼**。「一人一消息」是这套系统守得最死的一条，而杀进程正好绕过它。

      所以这里必须**大声记录**：日志 + task_logs。人得知道有这一轮，才能去核对。
    """
    try:
        pid = int(run_round.LOCK_FILE.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return True
    if not run_round._pid_alive(pid):
        return True

    print(f"[login_watch] ⚠ 硬抢占：强制中断正在跑的轮次（PID {pid}）")
    _kill_tree(pid, signal.SIGTERM)
    for _ in range(10):
        time.sleep(1)
        if not _round_running():
            break
    else:
        print("[login_watch] ⚠ SIGTERM 没生效，改用 SIGKILL")
        _kill_tree(pid, signal.SIGKILL)
        time.sleep(2)

    _record_preempted(pid)
    still = _round_running()
    print(
        "[login_watch] 锁已释放，切换接手"
        if not still
        else "[login_watch] ⚠ 杀不掉，锁仍被占——本轮放弃，等下一轮"
    )
    return not still


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

    if not needs_action(req):
        # pending 之外，只有「awaiting_code 且已经有码」才动手。
        # ★ awaiting_code 但没码 = 在等人填 —— 此时动手会拿到 None 并把请求判失败。
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
        # 用户当场发起的登录/切换比定时轮次更急 —— **直接抢占**（用户拍板选了硬抢占）。
        # 抢不到就本轮放弃（不 bump_attempts：那会把「抢不到」算成一次失败尝试，
        # 三次就把用户正常的登录判死了），等下一轮再试。
        if not _preempt_round():
            return 0

    store.bump_attempts()
    if req["phase"] == PHASE_PENDING:
        return _drive_login(store, req)
    return _drive_submit(store, req)


def _fail(store, message: str) -> int:
    """停手并如实报告。

    **刻意不按 HOME**：把 App 切到后台反而让人看不到现场。
    失败要能被人工接手，手机就该停在出错的那一屏。
    """
    print(f"[login_watch] 停手：{message}")
    store.set_phase(PHASE_FAILED, message)
    _clear_sentinel()
    return 0


def _account_phone(account_id: int) -> str | None:
    """取账号登记的手机号。抽成独立函数便于测试打桩（否则驱动测试要连真库）。"""
    from app.db import SessionLocal
    from app.models import Account

    with SessionLocal() as session:
        account = session.get(Account, account_id)
        return account.phone if account else None


def _drive_login(store, req: dict) -> int:
    """phase=pending：把手机操作到「验证码已发送」。

    `kind=switch` 时**先退出手机上登着的号**再登录目标号——这是不可逆的那一半，
    所以两种失败的措辞必须分开：旧号没退掉 vs 旧号退了但新号没登上，
    用户该做的事完全不同（前者手机上还有号，后者没有）。
    """
    number = _account_phone(req["account_id"])
    if not number:
        return _fail(store, f"账号 {req['account_id']} 没有登记手机号，无法登录")

    is_switch = req.get("kind") == KIND_SWITCH and bool(req.get("from_account_id"))
    logged_out = False

    if is_switch:
        store.set_phase(PHASE_LOGGING_OUT, "")
        print(f"[login_watch] 切换：先退出账号 {req['from_account_id']}")
        try:
            flows.logout(agree=AGREE_TERMS)
        except flows.FlowMismatch as exc:
            return _fail(
                store, f"切换停在「退出旧号」这一步：{exc}。手机上应该还登着原账号。"
            )
        except phone.AdbError as exc:
            return _fail(store, f"切换时 adb 出错：{exc}。手机上应该还登着原账号。")
        logged_out = True
        print("[login_watch] 旧号已退出")

    print(f"[login_watch] 驱动登录：账号 {req['account_id']}，手机尾号 {number[-4:]}")
    store.set_phase(PHASE_DISPATCHED, "")
    try:
        flows.login_code(number, agree=AGREE_TERMS)
    except flows.FlowMismatch as exc:
        if logged_out:
            return _fail(store, f"{_NO_ACCOUNT_HINT}｜{exc}")
        return _fail(store, f"{exc}")
    except phone.AdbError as exc:
        hint = f"{_NO_ACCOUNT_HINT}｜" if logged_out else ""
        return _fail(store, f"{hint}adb 出错：{exc}")

    store.set_phase(PHASE_AWAITING_CODE, "")
    print("[login_watch] 验证码已发出 → 前端应弹出输入框")
    return 0


# 切换失败时的关键提示：退出成功之后登录失败，手机上就是**没有账号**的。
# 只说「失败」的话用户不知道下一步该干什么。
_NO_ACCOUNT_HINT = "已退出旧号，但新号没登进去——手机上现在没有账号，请重新切换到某个账号或人工登录"


def _drive_submit(store, req: dict) -> int:
    """phase=awaiting_code：取人工填的码，输进去提交。"""
    code = store.claim_code()
    if not code:
        return _fail(store, "验证码不可用（未填、已用过或已过期）")

    print(f"[login_watch] 提交验证码（{len(code)} 位）")
    try:
        flows.submit_code(code)
    except flows.FlowMismatch as exc:
        # 切换时走到这里，退出已经成功了 —— 手机上是没有账号的
        if req.get("kind") == KIND_SWITCH and req.get("from_account_id"):
            return _fail(store, f"{_NO_ACCOUNT_HINT}｜{exc}")
        return _fail(store, f"{exc}")
    except phone.AdbError as exc:
        return _fail(store, f"adb 出错：{exc}")

    # 与 Pi 那条路共用同一份落地实现
    mark_account_logged_in(req["account_id"])
    note = _scan_jobs(store, req)
    store.set_phase(PHASE_SUCCESS, note)
    print(f"[login_watch] ✅ 登录成功，已把账号 {req['account_id']} 置为生效账号")
    _clear_sentinel()
    return 0


# 「扫不到」的几种原因，翻成人话。**它们与「这个号确实没有在招职位」
# 是不同的事**，混在一起会让用户对着「0 个职位」发呆。
_SCAN_REASONS = {
    "onboarding": "该账号还没建招聘名片（停在引导页），暂时没有职位管理页",
    "not_logged_in": "手机上不在登录态",
}


def _scan_jobs(store, req: dict) -> str:
    """登录成功后顺手扫出该账号的在招职位，返回要挂到 success 上的说明。

    ★ **扫描失败不是登录失败**：登录已经成了，打成 failed 会让前端报红、还会
      诱导用户重来一遍——而重来要再消耗一条短信验证码。所以只回一句 `notice`。

    ★ 相位在扫完之前停在 `scanning_jobs`（非终态），`.login_in_flight` 哨兵靠它
      保持新鲜，main_round 才不会在扫描中途把手机抢走。

    ★ 这里**兜住所有异常**：相位卡在 `scanning_jobs` 会让下一轮登录永久堵住，
      比丢一次扫描严重得多。
    """
    store.set_phase(PHASE_SCANNING_JOBS, "")
    print("[login_watch] 登录成功，正在扫该账号的在招职位…")

    try:
        result = flows.scan_jobs()
    except Exception as exc:  # noqa: BLE001  见 docstring 最后一条
        traceback.print_exc()
        return f"登录成功，但职位没扫到（{exc}）。可稍后重试，或在前端手工添加职位。"

    if not result["ok"]:
        reason = _SCAN_REASONS.get(result["reason"], result["reason"])
        print(f"[login_watch] 没有可扫的职位页：{reason}")
        return f"登录成功。{reason}——补齐后系统才能开始打招呼。"

    try:
        summary = sync_scanned_jobs(
            req["account_id"], result["jobs"], truncated=result["truncated"]
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return f"登录成功，扫到 {len(result['jobs'])} 个职位但写库失败（{exc}）"

    print(f"[login_watch] 职位同步：{summary}")
    if summary["created"]:
        tail = "（列表没扫全，可重扫）" if summary["truncated"] else ""
        return (
            f"已同步 {summary['created']} 个在招职位{tail}，"
            "在前端补齐 JD 并启用后才会开始打招呼"
        )
    if summary["updated"]:
        return f"已更新 {summary['updated']} 个职位的详情"
    return f"职位已是最新（{summary['unchanged']} 个已存在）"


if __name__ == "__main__":
    sys.exit(main())
