"""轮次运行器（P4 外部调度）：cron/定时器调用本脚本触发一个 Pi 轮次。

用法：
  python run_round.py main_round
  python run_round.py daily_round
  python run_round.py backfill_round
  python run_round.py login_round

组装：系统提示 + 轮次指令 → `pi -p`（DeepSeek v4 flash）。
工作窗口（08:00-23:00）判断在脚本层，窗口外直接跳过不启动 Pi（backfill 与
login 例外，见 WINDOW_EXEMPT_ROUNDS）。

本脚本**只用标准库**——它跑在 Mac 的裸 python3 上，没有 pipeline 的依赖。因此
需要 Redis 的判断（比如「有没有登录在途」）一律由 login_watch.py 落成哨兵文件，
这里只读文件。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent
SYSTEM_PROMPT = AGENT_DIR / "system-prompt.md"
ROUNDS_FILE = AGENT_DIR / "rounds.md"

# 全局轮次锁：所有轮次共用一部手机，必须串行——同一时刻只允许一个轮次在跑。
# 锁为文件 + 陈旧检测。锁有效需同时满足：持有进程存活 且 mtime 新鲜。
LOCK_FILE = AGENT_DIR / ".round.lock"
LOCK_STALE_SECONDS = 3900  # 一轮最长 3600s + 300s 余量，兜底防 PID 复用

# 空未读退避：连续空轮数达阈值后，30 分钟内跳过 main_round（不启动 Pi）
INBOUND_STATE_FILE = AGENT_DIR / ".inbound_state.json"
BACKOFF_EMPTY_STREAK = 2
BACKOFF_INTERVAL_SECONDS = 1800

# 登录在途哨兵。由 login_watch.py（它有 Redis 客户端）维护，本脚本只读——
# run_round.py 只用标准库，读不了 Redis。手机停在登录页时跑 main_round 会把它
# 误判成「掉登录」→ risk_set() → 全系统假熔断，所以这两轮必须让路。
LOGIN_IN_FLIGHT_FILE = AGENT_DIR / ".login_in_flight"
# 哨兵过期时间：轮询器每 20 秒重写一次，正常永远新鲜。它若挂掉或 Redis 失联，
# 哨兵最长只能堵住这么多分钟——否则一个陈旧文件会把所有 main_round 永久挡死。
LOGIN_SENTINEL_MAX_AGE = 2400

# 工作窗口豁免：backfill 只读下载；login 是人工发起的即时动作，晚上换号是常态
WINDOW_EXEMPT_ROUNDS = ("backfill_round", "login_round")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def acquire_round_lock() -> bool:
    try:
        if LOCK_FILE.exists():
            age = time.time() - LOCK_FILE.stat().st_mtime
            try:
                pid = int(LOCK_FILE.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                pid = 0
            if _pid_alive(pid) and age < LOCK_STALE_SECONDS:
                return False
            LOCK_FILE.unlink()
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return False


def release_round_lock() -> None:
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except OSError:
        pass


def read_inbound_state() -> dict:
    try:
        return json.loads(INBOUND_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"empty_streak": 0, "last_empty_ts": 0}


def write_inbound_state(state: dict) -> None:
    try:
        INBOUND_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def should_backoff() -> bool:
    state = read_inbound_state()
    if state.get("empty_streak", 0) < BACKOFF_EMPTY_STREAK:
        return False
    last = state.get("last_empty_ts", 0)
    return (time.time() - last) < BACKOFF_INTERVAL_SECONDS


def login_in_flight() -> bool:
    """有登录在途吗？哨兵文件带过期，避免轮询器挂掉后把主线永久堵死。"""
    try:
        return (time.time() - LOGIN_IN_FLIGHT_FILE.stat().st_mtime) < LOGIN_SENTINEL_MAX_AGE
    except OSError:
        return False


def ensure_device() -> str | None:
    """跑轮次前把「用哪台设备」定死。返回 None = 可以跑；否则返回拒跑原因。

    按「这台机器是不是专门驱动云机的」分三档：

    1. **已设 `ANDROID_SERIAL`** → 直接用，什么都不做。
    2. **设了 `HR_CLOUD_PHONE=1`**（.66 那台云机专用执行端）→ 用
       `discover_cloud_phone()` 找云机**当前**的端点，写进环境。
       ★ 云机的 adb 端口是**动态**的（实测一次会话换了三次），写死必然失效。
       ★ **找不到就拒跑这一轮，不猜**。猜错不是「少干一轮」——`flows.map_path()`
         会因此走进「不含冒号 → 真机表」那条分支，**拿 1080x2244 的坐标去点
         720x1280 的屏**，而且全程不报错。
    3. **都不是**（Mac 那台跑真机的）→ 保持原样：不传 `-s`，交给 adb 挑唯一那台。
       ★ 这一档**必须保持老行为**：Mac 上没有多多云客户端，`discover_cloud_phone()`
         永远返回 None；要是把它也变成「拒跑」，等于把正在跑的生产停掉。
    """
    import phone  # 延迟导入：避免改变本脚本的导入期行为

    if os.environ.get("ANDROID_SERIAL"):
        return None
    if os.environ.get("HR_CLOUD_PHONE") != "1":
        return None

    endpoint = phone.discover_cloud_phone()
    if not endpoint:
        return (
            "HR_CLOUD_PHONE=1 但找不到云机的 adb 端点（多多云客户端没开？云机没开机？）。"
            "不猜设备：猜错会让 map_path() 选到真机锚点表，拿 1080x2244 的坐标点云机的屏"
        )

    # ★ 两行都要，缺一不可：
    #   - 环境变量是给**子进程**的（Pi → mobile-mcp / hr-tools，它们靠它选设备与锚点表）
    #   - use_device() 是给**本进程**的：`android_ui._DEVICE` 在**导入时**读一次环境变量，
    #     此后再改 os.environ 对它已经无效了
    os.environ["ANDROID_SERIAL"] = endpoint
    phone.use_device(endpoint)
    return None


def device_problem() -> str | None:
    """手机配置有问题吗？有问题返回一句原因，没问题返回 None。

    ★ 只拦**一种确定的错误配置**：接着多台设备、又没指定用哪台（`ANDROID_SERIAL`
      或 use_device()）。这种状态下**每一条 adb 命令都会报
      `more than one device/emulator`**，而 Pi 会照常启动、瞎点一轮、把 token
      烧光——日志里看起来还像「猎聘页面变了」。所以要在启动 Pi 之前拦下来。
      （2026-10-16 云机接入：本机从此同时挂着真机与云机两台。）

    ★ **fail-open**：读不到设备列表一律放行。一次 adb 抖动不该让整轮静默跳过
      ——那种跳过和「正常跳过」长得一模一样，没人会发现系统停了
      （`has_active_jobs` 的注释里说过同一件事）。只接一台设备时也直接放行：
      Mac 上就是这种，行为与从前完全一致。
    """
    import phone  # 延迟到调用时再导入，避免改变本脚本的导入期行为

    try:
        devices = phone.list_devices()
    except (phone.AdbError, OSError):
        return None
    if phone.current_device():
        # 指定了设备：顺手把可能已断的 adb connect 补回来（断没断都不拦这一轮）
        phone.ensure_connected()
        return None
    if len([d for d in devices if d["state"] == "device"]) > 1:
        return (
            "接着多台设备却没指定用哪台——设 ANDROID_SERIAL=<序列号>"
            "（云机是 127.0.0.1:端口），否则每条 adb 命令都会报 more than one device"
        )
    return None


def _job_query_python() -> Path | None:
    """能 `import app.job_sync` 的解释器；找不到返回 None（调用方 fail-open）。

    launchd 拉的本来就是仓库 venv 的 python；但本脚本的设计前提是「只用标准库」，
    也可能被人用裸 python3 手动跑——那种解释器没有 pipeline 的依赖，所以两个都试。

    ★ venv 解释器的路径**两个平台都要试**：`bin/python` 是 POSIX，Windows 是
      `Scripts/python.exe`。只写前者的话 Windows 上会静默退回 `sys.executable`，
      而那个解释器多半没有 pipeline 的依赖（2026-10-16 云机接入时补）。
    """
    for candidate in (
        REPO_ROOT / ".venv" / "bin" / "python",
        REPO_ROOT / ".venv" / "Scripts" / "python.exe",
        Path(sys.executable),
    ):
        if candidate.exists():
            return candidate
    return None


def has_active_jobs() -> bool:
    """当前生效账号名下有没有已启用的职位？

    「没有职位」是**配置问题**（刚切的号还没扫职位、或职位还没补 JD），不是
    「今天没活干」。所以它必须在 `should_backoff()` **之前**拦下来，
    而且**绝不碰 `.inbound_state.json`**——否则一轮空跑就把 empty_streak 推到 2，
    白白退避 30 分钟，而屏幕上看起来只是「系统不干活」。

    ★ **fail-open**：查不到（脚本不在、DB 不通、超时）一律当成「有职位」放行。
      反过来写的话，一次 DB 抖动会让每一轮都静默跳过，而它的表现和「正常跳过」
      一模一样——没人会发现系统已经停了。这条有专门的测试盯着。
    """
    python = _job_query_python()
    if python is None:
        return True
    script = (
        "import sys; sys.path.insert(0, 'services/pipeline')\n"
        "from app.job_sync import active_job_count\n"
        "print(active_job_count())\n"
    )
    try:
        result = subprocess.run(
            [str(python), "-c", script],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode != 0:
        return True
    return (result.stdout or "").strip() != "0"


def parse_round_result(output: str):
    m = re.search(r"@@RESULT@@\s+inbound=(\d+)\s+outbound=(\d+)", output)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _session_dir() -> Path | None:
    """找 Pi 的 session 目录（agent 目录，排除 pi-agent/mobile_spike）。"""
    root = PI_AGENT_DIR / "sessions"
    try:
        for d in root.iterdir():
            if d.is_dir() and d.name.endswith("agent--") and "pi-agent" not in d.name:
                return d
    except OSError:
        pass
    return None


def _summarize_tokens(session_file: Path) -> tuple[int, float]:
    """逐条累加 session jsonl 所有 usage 的 input/output/cacheRead/reasoning 与 cost.total。"""
    total_tokens = 0
    total_cost = 0.0
    try:
        text = session_file.read_text(encoding="utf-8")
    except OSError:
        return 0, 0.0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        stack = [d]
        while stack:
            obj = stack.pop()
            if isinstance(obj, dict):
                u = obj.get("usage")
                if isinstance(u, dict):
                    total_tokens += (
                        int(u.get("input", 0) or 0)
                        + int(u.get("output", 0) or 0)
                        + int(u.get("cacheRead", 0) or 0)
                        + int(u.get("reasoning", 0) or 0)
                    )
                    c = u.get("cost", {})
                    if isinstance(c, dict):
                        total_cost += float(c.get("total", 0) or 0)
                    continue
                stack.extend(obj.values())
            elif isinstance(obj, list):
                stack.extend(obj)
    return total_tokens, total_cost


def _session_tokens_since(sdir: Path | None, before_mtime: float) -> tuple[int, float] | None:
    """本轮**新产生**的 session 文件的 (tokens, cost)；没有就返回 None。

    抽出来是为了让正常结束与超时结束**两条路都能落账**——原先超时那条直接
    `return 124`，成本连记都不记，而超时恰恰是最贵的那种轮次。
    """
    if not sdir:
        return None
    try:
        new_files = [f for f in sdir.glob("*.jsonl") if f.stat().st_mtime > before_mtime]
    except OSError:
        return None
    if not new_files:
        return None
    return _summarize_tokens(max(new_files, key=lambda f: f.stat().st_mtime))


def _write_token_summary(
    name: str,
    tokens: int,
    cost: float,
    outcome: str,
    account_id: int | None = None,
    duration: float = 0.0,
) -> None:
    """用项目 python 写一条 token 汇总 TaskLog。

    ★ `outcome` 必须由调用方按**实际退出码**传入。这里原先硬编码 `'success'`，
      于是所有轮次（含真实失败的）在库里全记成功——成本与成功率统计因此都是假的，
      而前端看起来一切正常。**「系统说自己成功」比「系统报错」危险得多。**

    ★ `account_id` 也要带上：`/api/hr/tasklogs` 是按账号过滤的，不带的那些行
      **前端一条都看不见**。NULL 只该留给真正的系统级汇总。

    账号在同一个进程里顺手查（本来就要开会话），不额外起子进程。
    """
    script = (
        "import sys, json; sys.path.insert(0, 'services/pipeline')\n"
        "from app.db import SessionLocal\n"
        "from app.models import Account, TaskLog\n"
        "from datetime import datetime\n"
        "from sqlalchemy import select\n"
        "p = json.loads(sys.argv[1])\n"
        "with SessionLocal() as s:\n"
        "    if p['account_id'] is None:\n"
        "        p['account_id'] = s.scalar(\n"
        "            select(Account.id).where(Account.is_active.is_(True))\n"
        "        )\n"
        "    s.add(TaskLog(\n"
        "        task_id=p['name'] + '_token_' + datetime.now().strftime('%Y%m%d%H%M%S'),\n"
        "        outcome=p['outcome'], tokens=p['tokens'], cost=p['cost'],\n"
        "        duration=p['duration'], account_id=p['account_id']))\n"
        "    s.commit()\n"
    )
    payload = json.dumps(
        {
            "name": name,
            "tokens": tokens,
            "cost": cost,
            "outcome": outcome,
            "duration": duration,
            "account_id": account_id,
        }
    )
    cwd = AGENT_DIR.parent
    subprocess.run(
        ["uv", "run", "--package", "hr-workbuddy-pipeline", "python", "-c", script, payload],
        cwd=str(cwd), capture_output=True, timeout=60,
    )

def _resolve_pi_cli() -> str:
    """定位 pi 的 JS 入口，跨平台。

    HR_PI_CLI 可显式指定入口（用于固定版本、与机器上其它 Pi 隔离）。

    Windows 下 npm 的 pi.cmd shim 不能被 CreateProcess 直跑，必须用 node + JS 入口；
    macOS/Linux 上 pi 是指向 cli.js 的符号链接，解出来即可。
    """
    candidates = [os.environ.get("HR_PI_CLI"), shutil.which("pi")]
    # PATH 不可靠时（launchd / systemd / 非交互 ssh 常拿不到 Homebrew），
    # 直接探测常见安装位置。
    candidates += [
        "/opt/homebrew/bin/pi",   # Apple Silicon Homebrew
        "/usr/local/bin/pi",      # Intel Homebrew
        str(Path.home() / ".npm-global/bin/pi"),
    ]
    for exe in candidates:
        if not exe:
            continue
        real = Path(exe).resolve()
        if real.suffix == ".js" and real.exists():
            return str(real)
    try:
        root = subprocess.run(
            ["npm", "root", "-g"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
        if root:
            cand = Path(root) / "@earendil-works/pi-coding-agent/dist/bundle/cli.js"
            if cand.exists():
                return str(cand)
    except (OSError, subprocess.SubprocessError):
        pass
    # 兜底：Windows 的 npm 全局目录
    return str(
        Path(os.environ.get("APPDATA", ""))
        / "npm/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
    )


PI_CLI = _resolve_pi_cli()


def _resolve_node() -> str:
    """定位 node 可执行文件。

    ★ 和 `_resolve_pi_cli` 同一个理由，**不能赌 PATH**：launchd 会给一份 PATH，
      但手动排障、非交互 ssh、cron 拿到的 PATH 里常常没有 Homebrew。
      原先这里写的是字面量 `"node"`，于是**手动跑会直接 FileNotFoundError 抛栈**
      ——而手动跑恰恰是出问题时才会做的事，等于在最需要它的时刻坏掉。

    （`phone.py` 的 `_ADB_CANDIDATES` 也是同一套思路，注释里写着同样的话。）
    """
    candidates = [os.environ.get("HR_NODE"), shutil.which("node")]
    candidates += [
        "/opt/homebrew/bin/node",  # Apple Silicon Homebrew
        "/usr/local/bin/node",     # Intel Homebrew
        str(Path.home() / ".npm-global/bin/node"),
    ]
    for exe in candidates:
        if exe and Path(exe).exists():
            return str(exe)
    # 再试一层：PI_CLI 一定在某个 node 安装的 node_modules 里，
    # 顺着它的祖先目录找 `bin/node`（Homebrew 的全局包与 node 同前缀）。
    # 限定 6 层，别一路走到根目录。
    here = Path(PI_CLI).parent
    for _ in range(6):
        cand = here / "bin" / "node"
        if cand.exists():
            return str(cand)
        if here.parent == here:
            break
        here = here.parent
    return "node"  # 实在找不到就退回 PATH，让报错保持原样


NODE = _resolve_node()

PROVIDER = "deepseek"
MODEL = "deepseek-flash"

# Pi 配置目录。默认 ~/.pi/agent（保持既有行为）；设 HR_PI_AGENT_DIR 可切到独立目录。
# 同一台机器上若有别的 Pi 实例，必须隔离——否则共享 mcp.json，本系统的 MCP
# server 会被对方的 Pi 也拉起来（例如 adb 抢占）。
PI_AGENT_DIR = Path(os.environ.get("HR_PI_AGENT_DIR") or (Path.home() / ".pi" / "agent"))


def _load_api_key() -> str:
    """从环境变量或 cua_driver/.env 读 DeepSeek key（不硬编码）。"""
    key = os.environ.get("SCREENING_LLM_API_KEY", "")
    if key:
        return key
    env_file = AGENT_DIR.parent / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("SCREENING_LLM_API_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


API_KEY = _load_api_key()

WORK_WINDOW_START = (8, 0)
# 工作窗口 08:00–23:00。
# ★ 若想把结束推到「当天结束」，写 `(24, 0)` 而不是 `(23, 59)`——`within_window`
#   对 `>= 24` 有显式处理。**但不能直接依赖 `replace(hour=24)`**：
#   Python 的 hour 只接受 0–23，写了会 ValueError，而这一句每轮开头都跑。
WORK_WINDOW_END = (23, 0)

# rounds.md 中的轮次标题 → 节号（解析用）
ROUND_SECTIONS = [
    ("main_round", "## 1."),
    ("daily_round", "## 2."),
    ("backfill_round", "## 3."),
    ("login_round", "## 4."),
]

# 每轮的超时（秒）。登录轮次必须短：它卡住时会一直占着全局轮次锁，而在默认的
# 3600s 下，那意味着整整一小时里 main_round 全被挡住。
ROUND_TIMEOUTS = {"login_round": 420}
DEFAULT_ROUND_TIMEOUT = 3600

# ─── 可选的单轮上限（默认不设 = 行为与真机完全一致）───────────────────
#
# 为什么从外面传、而不是写死在 rounds.md：`rounds.md` §3 的「单轮上限 20 个」是
# 按真机写的，而云机**大列表滚动扫描**比真机慢约 2.3 倍（`docs/runbook.md` 记真机约
# 60 秒，云机实测 137 秒）。20 个候选在那种慢速下做不完，一轮会撞满 3600s 超时——
# 于是永远输出不了 @@RESULT@@，outcome 永远是 timeout，空轮退避的判定也跟着失真。
#
# ★ 但「云机慢」不能一概而论：2026-10-17 实测一轮 main_round（5 个候选），单次操作
#   中位 **2.4 秒**，与真机的 2–3 秒基本持平，307 秒自然跑完。慢的是批量扫描，
#   不是普通交互。所以这个上限主要给 backfill 那类批量轮次用。
#   完整数据见 scripts/cloud/README.md。
#
# ★ 为什么用「提示词里的硬限制」而不是到点 kill 进程：
#   硬中断可能打断一次**已经点出去、但还没写 interaction_log** 的触达，
#   「一人一消息」在那条路径上无法保证（login_watch.py 里有同一条代价说明）。
#   让 Pi 自己数着做、按顺序落账后收尾，账才是完整的。
#
# 例：云机执行端设 HR_ROUND_MAX_ITEMS=5；做带触达的验证时设 HR_ROUND_MAX_GREETS=3。
ROUND_MAX_ITEMS = os.environ.get("HR_ROUND_MAX_ITEMS", "").strip()
ROUND_MAX_GREETS = os.environ.get("HR_ROUND_MAX_GREETS", "").strip()


def _append_round_caps(prompt: str) -> str:
    """把可选上限拼到本轮提示词末尾；没设就原样返回（真机行为零变化）。"""
    caps: list[str] = []
    if ROUND_MAX_ITEMS:
        caps.append(
            f"本轮最多处理 {ROUND_MAX_ITEMS} 个候选人/会话，超出的**留待下一轮**"
            "（下一轮会重新取到未完成的部分），不要为了做完而赶工。"
        )
    if ROUND_MAX_GREETS:
        caps.append(
            f"本轮**最多完成 {ROUND_MAX_GREETS} 次触达**（打招呼 / 索要简历等一切会触达真人的动作）。"
            "数到上限就**立即停止一切触达**；已经发出去的那几次，务必按"
            "「验证上屏 → interaction_log(out) → state_transition → tasklog_add」"
            "顺序落完账再结束本轮。**不要重试、不要补发。**"
        )
    if not caps:
        return prompt
    lines = "\n".join(f"- {c}" for c in caps)
    return (
        f"{prompt}\n\n---\n\n## ★ 本轮硬限制（优先级高于上文的一般性描述）\n\n"
        f"{lines}\n\n"
        "达到限制后**不要继续操作手机**，按上述顺序落账，输出 `@@RESULT@@` 行后结束。"
    )


def within_window(now: datetime) -> bool:
    """现在在工作窗口内吗？窗口外直接跳过、不启动 Pi。

    ★ 结束时间允许写 `(24, 0)`（= 到当天结束）；当前配置是 23:00，走下面的正常分支。
      **不能直接 `replace(hour=24)`**
      ——Python 的 `hour` 合法范围是 0–23，写了会 ValueError 抛栈，
      而这里是在每一轮开头调的，等于整个调度直接崩。
    """
    start = now.replace(hour=WORK_WINDOW_START[0], minute=WORK_WINDOW_START[1], second=0)
    if WORK_WINDOW_END[0] >= 24:
        return now >= start
    end = now.replace(hour=WORK_WINDOW_END[0], minute=WORK_WINDOW_END[1], second=0)
    return start <= now < end


def extract_round_prompt(name: str) -> str:
    text = ROUNDS_FILE.read_text(encoding="utf-8")
    sections = {n: marker for n, marker in ROUND_SECTIONS}
    markers = sorted(sections.values(), key=text.find)
    # 取该轮次标题到下一个标题之间的文本
    start_marker = sections[name]
    start = text.find(start_marker)
    if start < 0:
        raise SystemExit(f"rounds.md 中找不到轮次 {name}")
    next_pos = len(text)
    for m in markers:
        pos = text.find(m, start + 1)
        if pos > start and pos < next_pos:
            next_pos = pos
    return text[start:next_pos].strip()


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    if name not in [n for n, _ in ROUND_SECTIONS]:
        print(f"用法: python run_round.py <{'|'.join(n for n, _ in ROUND_SECTIONS)}>")
        return 2
    now = datetime.now()
    # backfill_round 只读下载（不触达、不发消息），login_round 是人工发起的即时动作，
    # 两者都可跨工作窗口运行
    if name not in WINDOW_EXEMPT_ROUNDS and not within_window(now):
        print(f"[skip] {name}: 工作窗口外（{now:%H:%M}）")
        return 0
    # 先把「用哪台设备」定死（云机端口是动态的，见 ensure_device 的三档说明），
    # 再看设备配置有没有问题。顺序不能反：ensure_device 可能刚把 ANDROID_SERIAL 补上。
    device_denied = ensure_device()
    if device_denied:
        print(f"[skip] {name}: {device_denied}")
        return 0
    # 手机配置不对就别启动 Pi——那种状态下每条 adb 命令都会失败，而 Pi 会照跑一轮
    problem = device_problem()
    if problem:
        print(f"[skip] {name}: {problem}")
        return 0
    if name in ("main_round", "daily_round") and login_in_flight():
        print(f"[skip] {name}: 有登录在途（手机停在登录页，跑本轮会误判成掉登录）")
        return 0
    if name == "main_round" and not has_active_jobs():
        # ★ 刻意不碰 .inbound_state.json：没职位是配置问题，不是「今天没活」。
        #   记进退避的话，一轮空跑就会把后续 30 分钟全挡掉。
        print(f"[skip] {name}: 当前账号名下没有已启用的职位（在前端补齐 JD 后自动恢复）")
        return 0
    if name == "main_round" and should_backoff():
        print(f"[skip] {name}: 空未读退避（连续空，30 分钟内不再启动 Pi）")
        return 0
    if not acquire_round_lock():
        print(f"[skip] {name}: 另一轮次正在运行（全局轮次锁）")
        return 0
    try:
        # 可选上限拼在本轮提示词末尾（HR_ROUND_MAX_ITEMS / HR_ROUND_MAX_GREETS）；
        # 都没设时原样返回，真机行为不变。
        prompt = _append_round_caps(extract_round_prompt(name))
        timeout = ROUND_TIMEOUTS.get(name, DEFAULT_ROUND_TIMEOUT)
        cmd = [
            NODE, str(PI_CLI),
            "--provider", PROVIDER,
            "--model", MODEL,
            "--api-key", API_KEY,
            "--append-system-prompt", str(SYSTEM_PROMPT),
            "-p", prompt,
        ]
        sdir = _session_dir()
        before_mtime = 0.0
        if sdir:
            try:
                files = list(sdir.glob("*.jsonl"))
                if files:
                    before_mtime = max(f.stat().st_mtime for f in files)
            except OSError:
                pass
        started = time.time()
        log_path = AGENT_DIR / f".round_{name}.log"
        try:
            with open(log_path, "w", encoding="utf-8") as logf:
                env = {**os.environ, "PI_CODING_AGENT_DIR": str(PI_AGENT_DIR)}
                result = subprocess.run(
                    cmd,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=str(AGENT_DIR),
                    env=env,
                    timeout=timeout,
                )
        except subprocess.TimeoutExpired:
            print(f"[{name}] 超时（{timeout}s）强制结束，释放锁")
            # ★ 超时的一轮**最贵**（跑到上限才被砍），不能让它连账都不落：
            #   原先这里直接 return，成本与「失败」双双消失在统计之外。
            got = _session_tokens_since(sdir, before_mtime)
            if got:
                print(f"[{name}] tokens={got[0]}, cost={got[1]:.6f}（超时结束）")
                _write_token_summary(
                    name,
                    got[0],
                    got[1],
                    outcome="timeout",
                    duration=time.time() - started,
                )
            return 124
        output = log_path.read_text(encoding="utf-8")
        if output:
            print(output, end="")
        print(f"[{name}] 退出码 {result.returncode}，耗时 {time.time()-started:.0f}s")
        # ★ 成败按**真实退出码**判。原先一律写 'success'，于是失败在库里看不出来，
        #   成本与成功率统计全是假的——而前端看起来一切正常。
        got = _session_tokens_since(sdir, before_mtime)
        if got:
            print(f"[{name}] tokens={got[0]}, cost={got[1]:.6f}")
            _write_token_summary(
                name,
                got[0],
                got[1],
                outcome="success" if result.returncode == 0 else "failed",
                duration=time.time() - started,
            )
        if name == "main_round":
            parsed = parse_round_result(output)
            if parsed is not None:
                inbound, outbound = parsed
                state = read_inbound_state()
                if outbound > 0 or inbound > 0:
                    state["empty_streak"] = 0
                    state["last_empty_ts"] = 0
                else:
                    state["empty_streak"] = state.get("empty_streak", 0) + 1
                    state["last_empty_ts"] = time.time()
                write_inbound_state(state)
        return result.returncode
    finally:
        release_round_lock()


if __name__ == "__main__":
    sys.exit(main())
