#!/usr/bin/env python3
"""BOSS 轮次运行器。

## 控制层：Pi（LLM）+ MCP，与猎聘一致

轮次正文 spawn Pi：`node pi --provider deepseek --model deepseek-flash
--append-system-prompt system-prompt.md -p <轮次提示词>`，Pi 挂两个 MCP
server（`mobile` 看屏/点/滑 + `hr-tools` 状态/联锁/触达），**实时决定每一步**。
真机写动作仍走服务端闸门（`gated_tap(touch=True)` / `greet_check` / 配额），
LLM 只负责「看和决定」。

逃生口 `BOSS_USE_PI=0` 退回旧的确定性 `flows.*` 路径（`_*_legacy`），
真机验证 Pi 出问题时一键回退。默认 `BOSS_USE_PI=1`。

## 闸门顺序（照猎聘的 7 道，逐条说明哪里改了）

| # | 闸门 | 与猎聘的差异 |
|---|---|---|
| ① | 轮次名合法 | 照搬 |
| ② | 工作窗口 08:00–23:00 | 照搬（backfill/login 豁免） |
| ③ | 设备 | 照搬：多设备未指定 ANDROID_SERIAL 才拦，fail-open |
| ④ | **登录关卡在途** | ★ **改造过**，见下面 |
| ⑤ | 有可用职位（仅 main） | ★ 条件更严：`status=active` **且** `state=开放中` **且** `jd_complete` |
| ⑥ | 空未读退避 | 照搬 |
| ⑦ | 全局轮次锁 | 照搬 |

## ★ 闸门 ④ 为什么必须改造

猎聘的 `pipeline:state:login_req` 语义是「**有一个换号请求在途**」——
那是机器要去做的事，做完就清掉。

BOSS 不一样：登录有**三道人工关卡**（易盾滑块 / 短信 / 人脸实名），
手机随时可能停在任意一关前面。而**关卡页长得就像「掉登录了」**。
照搬猎聘的逻辑，轮次会把关卡页误判成掉登录 → 触发假熔断 →
**把好好的账号锁死**。

所以这里用 `boss:login_gate_in_flight`：只表示「手机停在关卡前等人」，
轮次看到它就**安静跳过**（不是失败），并且**不去动那个标记**——
它是人在手机上过完关卡之后才该清的。

## 用法

    python3 agent_boss/run_round.py main_round
    python3 agent_boss/run_round.py daily_round
    python3 agent_boss/run_round.py login_round      # 人工兜底，只检查不动作

环境变量：`BOSS_SERVER_URL`（默认 http://127.0.0.1:8100）、
`BOSS_INGEST_TOKEN`、`ANDROID_SERIAL`、`BOSS_GREET_ENABLED`、`BOSS_USE_PI`。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import flows  # noqa: E402
import phone  # noqa: E402

HERE = Path(__file__).resolve().parent
AGENT_DIR = HERE

# ——— 配置 ———

SERVER_URL = os.environ.get("BOSS_SERVER_URL", "http://127.0.0.1:8100").rstrip("/")
INGEST_TOKEN = os.environ.get("BOSS_INGEST_TOKEN", "")

#: ★ **自动打招呼默认关着。**
#:   用户已定：「先只采集，稳了再开」。BOSS 这个账号刚被风控判过异常、刚过完人脸，
#:   紧接着用脚本批量发消息风险太大。打开它需要显式设 `BOSS_GREET_ENABLED=1`。
GREET_ENABLED = os.environ.get("BOSS_GREET_ENABLED", "") == "1"

#: 一轮里最多打招呼多少个。★ 保守占位，不是实测值（见 services/boss-pipeline/app/config.py）。
GREET_CAP = int(os.environ.get("BOSS_GREET_CAP", "5"))

#: 干跑开关：只走到「立即沟通」按钮前、打印将发给谁，**不点**。
#: 开启 `BOSS_GREET_ENABLED=1` 之前先用它确认目标（`greet()` 自身默认也是干跑）。
GREET_DRY_RUN = os.environ.get("BOSS_GREET_DRY_RUN", "") == "1"

#: 一轮最多处理几个简历（同意请求 + 下载已投递）。附件链路一步要 ~30–60 秒。
RESUME_CAP = int(os.environ.get("BOSS_RESUME_CAP", "3"))

#: 收简历时扫会话列表的上限（默认对齐旧值，运行时调大以够到「全部」未读）。
RESUME_SCAN_LIMIT = int(os.environ.get("BOSS_RESUME_SCAN_LIMIT", "30"))
RESUME_SCAN_SCROLLS = int(os.environ.get("BOSS_RESUME_SCAN_SCROLLS", "6"))
#: 一轮读未读会话的上限（默认对齐旧值 5，运行时调大）。
UNREAD_LIMIT = int(os.environ.get("BOSS_UNREAD_LIMIT", "5"))

#: 读未读时的子 tab 顺序：先「仅沟通」（活跃对话，高优先）再「新招呼」（招呼积压）。
#: ★ 用户定的顺序（2026-10-20），不要动。
UNREAD_SUB_TABS = ("仅沟通", "新招呼")

#: 已取过的简历账本（本地；服务端另有一份 minio_object_key 兜底）。
RESUME_LEDGER = AGENT_DIR / ".resumes.json"

WORK_WINDOW_START = (8, 0)
WORK_WINDOW_END = (23, 0)

ROUNDS = ("main_round", "daily_round", "login_round", "backfill_round", "resume_round")
WINDOW_EXEMPT = ("backfill_round", "login_round", "resume_round")

LOCK_PATH = AGENT_DIR / ".round.lock"
LOGIN_GATE_PATH = AGENT_DIR / ".login_gate_in_flight"
INBOUND_STATE_PATH = AGENT_DIR / ".inbound_state.json"
ROUND_LOG_DIR = AGENT_DIR

LOCK_STALE_SECONDS = 3900      # 超过这个时长认为是上一轮死了留下的
LOGIN_GATE_STALE_SECONDS = 2400
BACKOFF_AFTER_EMPTY = 2        # 连续 N 轮没有新未读就退避
BACKOFF_MINUTES = 30


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ——— 闸门 ———

def within_window(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    start = now.replace(hour=WORK_WINDOW_START[0], minute=WORK_WINDOW_START[1], second=0, microsecond=0)
    if WORK_WINDOW_END[0] >= 24:
        return now >= start
    end = now.replace(hour=WORK_WINDOW_END[0], minute=WORK_WINDOW_END[1], second=0, microsecond=0)
    return start <= now < end


def ensure_device() -> str | None:
    """跑轮次前把「用哪台设备」定死。返回 None = 可以跑；否则返回拒跑原因。

    与猎聘的 `agent/run_round.py:ensure_device()` 同一套三档逻辑：

    1. 已设 `ANDROID_SERIAL` → 直接用
    2. 设了 `HR_CLOUD_PHONE=1`（.66 云机执行端）→ `discover_cloud_phone()` 找**当前**端点
    3. 都没设（真机 / Mac）→ 什么都不做，保持原行为

    ★ 云机的 adb 端口是动态的（实测一次会话换三次），写死必然失效。
    ★ 找不到就拒跑，**不猜**——猜错会让 `flows.map_path()` 选回真机锚点表，
      拿 1080x2244 的坐标点 720x1280 的屏。
    """
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
    # ★ 两行都要：环境变量给子进程（这里没有子进程，但保持一致），
    #   use_device() 给本进程——`android_ui._DEVICE` 在导入时读一次环境变量。
    os.environ["ANDROID_SERIAL"] = endpoint
    phone.use_device(endpoint)
    return None


def device_problem() -> str | None:
    """多设备又没指定 serial 才拦。**单设备时放行**（fail-open）——不能因为查不到就什么都不干。"""
    try:
        devs = [d for d in phone.list_devices() if d.get("state") == "device"]
    except Exception:
        return None
    if len(devs) > 1 and not phone.current_device():
        return f"adb 上有 {len(devs)} 台设备，但没指定 ANDROID_SERIAL，操作会打到哪台不确定"
    if not devs:
        return "adb 上看不到任何设备"
    return None


def login_gate_in_flight() -> bool:
    """手机是不是正停在风控关卡前等人。

    ★ 过期的标记当作没有——否则一次意外（比如断电）会把轮次永久卡死。
      但**只在明显过期时才忽略**，且要打日志让人看得见。
    """
    if not LOGIN_GATE_PATH.exists():
        return False
    age = time.time() - LOGIN_GATE_PATH.stat().st_mtime
    if age > LOGIN_GATE_STALE_SECONDS:
        log(f"登录关卡标记已过期（{age/60:.0f} 分钟），忽略它")
        return False
    return True


def mark_login_gate(reason: str) -> None:
    LOGIN_GATE_PATH.write_text(f"{datetime.now().isoformat()} {reason}\n", encoding="utf-8")
    log(f"★ 已标记「等你过风控关卡」：{reason}")


def clear_login_gate() -> None:
    LOGIN_GATE_PATH.unlink(missing_ok=True)


def load_inbound_state() -> dict:
    if INBOUND_STATE_PATH.exists():
        try:
            return json.loads(INBOUND_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"empty_streak": 0, "last_empty_at": ""}


def save_inbound_state(state: dict) -> None:
    INBOUND_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def should_backoff(state: dict) -> bool:
    """连续几轮都没有新未读 → 歇一会儿（别空转烧配额与风控）。"""
    if state.get("empty_streak", 0) < BACKOFF_AFTER_EMPTY:
        return False
    last = state.get("last_empty_at") or ""
    if not last:
        return False
    try:
        gap = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 60
    except ValueError:
        return False
    return gap < BACKOFF_MINUTES


class RoundLock:
    """全局轮次锁。文件 + PID 存活 + mtime 三重判断（照猎聘）。"""

    def __init__(self, path: Path = LOCK_PATH):
        self.path = path
        self.acquired = False

    def __enter__(self):
        if self.path.exists():
            age = time.time() - self.path.stat().st_mtime
            holder = self.path.read_text(encoding="utf-8", errors="replace").strip()
            alive = False
            try:
                pid = int(holder.split()[0])
                os.kill(pid, 0)              # 只探活，不发信号
                alive = True
            except (ValueError, IndexError, ProcessLookupError, PermissionError, OSError):
                alive = False
            if alive and age < LOCK_STALE_SECONDS:
                raise RuntimeError(f"已有轮次在跑（pid {holder.split()[0][:8]}，{age:.0f}s）")
            log(f"清掉陈旧的轮次锁（{age:.0f}s 前的）")
        self.path.write_text(f"{os.getpid()} {datetime.now().isoformat()}\n", encoding="utf-8")
        self.acquired = True
        return self

    def __exit__(self, *exc):
        if self.acquired:
            self.path.unlink(missing_ok=True)
        return False


# ——— 与服务端 ———

def _post(path: str, payload) -> dict:
    req = urllib.request.Request(
        SERVER_URL + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json", "x-ingest-token": INGEST_TOKEN},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _get(path: str) -> dict:
    with urllib.request.urlopen(SERVER_URL + path, timeout=30) as resp:
        return json.load(resp)


def _all_jobs() -> list[dict]:
    try:
        return _get("/api/jobs?limit=200").get("items", [])
    except Exception as exc:
        log(f"取职位失败（fail-open，照常跑）：{exc}")
        return []


def _is_on(job: dict) -> bool:
    """系统启用过 + BOSS 那边确实在招。**不含 JD 条件**。"""
    return job.get("status") == "active" and job.get("state") in ("开放中", "")


def usable_jobs() -> list[dict]:
    """服务端认定的「可用」职位（启用过 + BOSS 在招 + **JD 读全**）。

    ★ 判定口径与服务端 `hr_tools.job_list_active` 一致。
      （联锁的权威在服务端，这里只是为了少跑几趟。）
    """
    return [j for j in _all_jobs() if _is_on(j) and j.get("jd_complete")]


def jobs_needing_jd() -> list[dict]:
    """**JD 还没读全**的在招职位——这才是该去抓 JD 的那批。

    ★★ 这里是修一个**死锁**：原来 `daily_round` 只给 `usable_jobs()` 抓 JD，
      而 usable **要求** `jd_complete`。于是新扫进来的职位永远轮不到抓 JD、
      也永远变不成 usable——**JD 一辈子抓不到**。
      判断条件是「在招 **且** 还没读全」，不是「已经读全了」。
    """
    return [j for j in _all_jobs() if _is_on(j) and not j.get("jd_complete")]


# ——— Pi（LLM）控制层 ———
#
# 对齐猎聘：轮次正文不再直接调 flows.*，而是 spawn Pi（node + DeepSeek），
# Pi 挂两个 MCP server（mobile + hr-tools）实时决定每一步。所有真机写动作
# 仍走服务端闸门（gated_tap / greet_check / 配额）——LLM 只负责「看和决定」。
# 逃生口 BOSS_USE_PI=0 退回旧的确定性路径（_*_legacy）。

USE_PI = os.environ.get("BOSS_USE_PI", "1") == "1"

PROVIDER = "deepseek"
MODEL = "deepseek-flash"

SYSTEM_PROMPT = AGENT_DIR / "system-prompt.md"
ROUNDS_FILE = AGENT_DIR / "rounds.md"

# Pi 配置目录：BOSS 用自己的一份（mcp.json 指向 mobile + BOSS hr-tools），
# 与猎聘 ~/.pi/hr-agent 隔离——两边不会互拉对方的 MCP server。
PI_AGENT_DIR = Path(os.environ.get("HR_PI_AGENT_DIR") or (Path.home() / ".pi" / "hr-agent-boss"))

# rounds.md 中的轮次标题 → 节号（extract_round_prompt 解析用）
ROUND_SECTIONS = [
    ("main_round", "## 1."),
    ("daily_round", "## 2."),
    ("backfill_round", "## 3."),
    ("login_round", "## 4."),
]

DEFAULT_ROUND_TIMEOUT = 3600  # 一轮最长 3600s；login_round 不经 Pi，不受此限

# 可选的单轮上限（默认不设 = 真机行为零变化）。语义同猎聘 HR_ROUND_MAX_*：
# 云机大列表扫描慢、或做带触达的验证时，用提示词里的硬限制让 Pi 自己数着做、
# 按顺序落账后收尾——硬 kill 会打断一次「已点出但还没落账」的触达。
FORCE_INBOUND = os.environ.get("BOSS_FORCE_INBOUND", "").strip() == "1"
ROUND_MAX_ITEMS = os.environ.get("BOSS_ROUND_MAX_ITEMS", "").strip()
ROUND_MAX_GREETS = os.environ.get("BOSS_ROUND_MAX_GREETS", "").strip()
#: 人工锁定的**唯一**招呼对象（真机验证/人工指定单发时用）。配合 BOSS_GREET_ENABLED=1。
FORCE_GREET_JC = os.environ.get("BOSS_FORCE_GREET_JC", "").strip()
FORCE_GREET_NAME = os.environ.get("BOSS_FORCE_GREET_NAME", "").strip()

#: 本轮 Pi 消耗的 token/cost，由 _run_pi_round 在跑完后填，__main__ 落库一次。
_pi_tokens = 0
_pi_cost = 0.0


def _resolve_pi_cli() -> str:
    """定位 pi 的 JS 入口，跨平台（照猎聘 agent/run_round.py 抄）。

    HR_PI_CLI 可显式指定入口（固定版本、与机器上其它 Pi 隔离）。
    Windows 下 npm 的 pi.cmd shim 不能被 CreateProcess 直跑，必须用 node + JS 入口。
    """
    candidates = [os.environ.get("HR_PI_CLI"), shutil.which("pi")]
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
    """定位 node。不赌 PATH（计划任务/非交互 shell 的 PATH 常缺 Homebrew）。"""
    candidates = [os.environ.get("HR_NODE"), shutil.which("node")]
    candidates += [
        "/opt/homebrew/bin/node",
        "/usr/local/bin/node",
        str(Path.home() / ".npm-global/bin/node"),
    ]
    for exe in candidates:
        if exe and Path(exe).exists():
            return str(exe)
    here = Path(PI_CLI).parent
    for _ in range(6):
        cand = here / "bin" / "node"
        if cand.exists():
            return str(cand)
        if here.parent == here:
            break
        here = here.parent
    return "node"


NODE = _resolve_node()


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
    """逐条累加 session jsonl 的 input/output/cacheRead/reasoning 与 cost.total。"""
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
    """本轮新产生的 session 文件的 (tokens, cost)；没有就返回 None。"""
    if not sdir:
        return None
    try:
        new_files = [f for f in sdir.glob("*.jsonl") if f.stat().st_mtime > before_mtime]
    except OSError:
        return None
    if not new_files:
        return None
    return _summarize_tokens(max(new_files, key=lambda f: f.stat().st_mtime))


def extract_round_prompt(name: str) -> str:
    try:
        text = ROUNDS_FILE.read_text(encoding="utf-8")
    except OSError:
        raise SystemExit(f"rounds.md 不存在：{ROUNDS_FILE}")
    sections = {n: marker for n, marker in ROUND_SECTIONS}
    markers = sorted(sections.values(), key=text.find)
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


def parse_round_result(output: str):
    m = re.search(r"@@RESULT@@\s+inbound=(\d+)\s+outbound=(\d+)", output)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _append_round_caps(prompt: str) -> str:
    """把可选上限拼到本轮提示词末尾；没设就原样返回（真机行为零变化）。"""
    caps: list[str] = []
    if not GREET_ENABLED:
        # ★ 服务端 may_greet 不读 BOSS_GREET_ENABLED（那是 agent 侧闸门），
        #   所以 Pi 模式下必须在这里把「默认关打招呼」显式塞给 Pi——否则 Pi 会
        #   默认去打招呼，直接绕过这条铁律。这是写动作的兜底，不是提示词的软约束。
        caps.append(
            "**本轮禁止一切打招呼 / 发消息（`BOSS_GREET_ENABLED` 未设，自动打招呼默认关）。**"
            "只采集候选人、读详情、读未读会话；`greet_check` / `gated_tap(touch=True)` / "
            "`interaction_log(out, greet)` 一律不要做。`@@RESULT@@` 的 outbound 填 0。"
        )
    if FORCE_GREET_JC:
        target = f"jc_id={FORCE_GREET_JC}"
        if FORCE_GREET_NAME:
            target += f"（姓名={FORCE_GREET_NAME}）"
        caps.append(
            f"**本轮触达对象被人工锁定为唯一一个：{target}。**"
            "除此之外**任何**候选人都不许打招呼 / 发消息 / 索要简历。"
            "先在「牛人」列表里按姓名找到这个人、点进详情页，`jc_status` 确认 jc_id 无误后，"
            "再走 `greet_check → gated_tap(touch=True) 点「立即沟通」→ interaction_log(out,greet)`。"
            "完成这一个后立即收手，`@@RESULT@@` 的 outbound 按实际打招呼数（0 或 1）填。"
        )
    if FORCE_INBOUND:
        caps.append(
            "**本轮强制只走 inbound（处理未读消息），跳过 outbound（打招呼/采候选人）。**"
            "即使 `quota_status()` 返回 remaining > 0 也照此执行——这是人工指定的测试轮。"
            "不要为了消耗额度去打招呼；先把未读会话按 inbound 流程处理完，"
            "再按 `@@RESULT@@` 格式报告（inbound 填实际处理数、outbound 填 0）。"
        )
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


def _write_token_summary(
    name: str, tokens: int, cost: float, outcome: str, duration: float = 0.0, note: str = ""
) -> None:
    """用 HTTP 记一行轮次成本/结果。BOSS 单账号：无 account_id，走 round_name。

    与猎聘不同：BOSS 的 run_round 本就用 `/api/ingest/tasklog` 上报运行记录，
    这里把 token/cost 并进同一行，不额外起子进程、不重复上报。
    """
    try:
        _post("/api/ingest/tasklog", {
            "round_name": name,
            "outcome": outcome,
            "tokens": tokens,
            "cost": cost,
            "duration": round(duration, 1),
            "note": note,
        })
    except Exception as exc:  # noqa: BLE001
        log(f"（运行记录上报失败，不影响本轮：{exc}）")


def _run_pi_round(name: str) -> int:
    """spawn Pi 跑一轮，返回 Pi 退出码（124 = 超时）。

    镜像猎聘 agent/run_round.py 的 main()：拼 prompt → spawn node pi →
    落日志 → 数 token → 解析 @@RESULT@@。差别：
    - 轮次闸门 + 全局锁仍由本文件 main() 负责，这里只负责跑 Pi。
    - token/cost 只记到模块级 _pi_tokens/_pi_cost，落库统一在 __main__ 做一次。
    """
    global _pi_tokens, _pi_cost
    prompt = _append_round_caps(extract_round_prompt(name))
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
                stdin=subprocess.DEVNULL,  # ★ 否则 node 继承父进程打开的 stdin，pi CLI 会阻塞在读 stdin 上（真机验证踩过）
                text=True,
                cwd=str(AGENT_DIR),
                env=env,
                timeout=DEFAULT_ROUND_TIMEOUT,
            )
    except subprocess.TimeoutExpired:
        log(f"[{name}] 超时（{DEFAULT_ROUND_TIMEOUT}s）强制结束")
        got = _session_tokens_since(sdir, before_mtime)
        if got:
            _pi_tokens, _pi_cost = got
            log(f"[{name}] tokens={got[0]}, cost={got[1]:.6f}（超时结束）")
        return 124
    output = log_path.read_text(encoding="utf-8")
    if output:
        print(output, end="")
    log(f"[{name}] 退出码 {result.returncode}，耗时 {time.time()-started:.0f}s")
    got = _session_tokens_since(sdir, before_mtime)
    if got:
        _pi_tokens, _pi_cost = got
        log(f"[{name}] tokens={got[0]}, cost={got[1]:.6f}")
    if name == "main_round":
        parsed = parse_round_result(output)
        if parsed is not None:
            inbound, outbound = parsed
            state = load_inbound_state()
            if outbound > 0 or inbound > 0:
                state["empty_streak"] = 0
            else:
                state["empty_streak"] = state.get("empty_streak", 0) + 1
                state["last_empty_at"] = datetime.now().isoformat()
            save_inbound_state(state)
    if name == "main_round":
        # ★ Pi 已用 gated_tap(touch=True) 点过「同意」，这里只补跑确定性下载+落 MinIO
        #   （不点同意，避免绕过风控闸门重复同意）。
        try:
            resume_work(approve=False)
        except flows.FlowMismatch as exc:
            log(f"  简历下载补跑中断：{exc}")
    return result.returncode


# ——— 采集 ———

def collect_jobs() -> int:
    jobs = flows.scan_jobs()
    if jobs:
        _post("/api/ingest/jobs", [
            {"title": j["title"], "state": j.get("state", ""), "details": j.get("details", "")}
            for j in jobs
        ])
    log(f"  扫到 {len(jobs)} 个职位")
    return len(jobs)


def collect_jd(title: str) -> bool:
    """抓一个职位的 JD 并推上去。**只有读全了才推**（服务端也只认 complete 的）。"""
    d = flows.fetch_jd(title)
    if not d.get("complete"):
        log(f"  「{title}」的 JD 没读全（{len(d.get('jd_text') or '')} 字），不推")
        return False
    _post("/api/ingest/jobs", [{
        "title": title, "state": "开放中", "details": "",
        "jd_text": d["jd_text"], "jd_complete": True, "tags": d.get("tags") or [],
    }])
    log(f"  「{title}」的 JD {len(d['jd_text'])} 字，已推")
    return True


def collect_candidates(job_title: str, limit: int = 10) -> list[dict]:
    """采一个职位下的推荐候选人（列表卡上的那些字段）。

    ★ **不发 `boss_key`**——由服务端用姓名兜底。
      原因是实测发现列表卡与详情页给的公司字符串**不是同一个**
      （`云南白药 · 数据开发` vs `云南白药集团·数据开发`），
      在这边拼键会让同一个人变成两条。详见 `services/boss-pipeline/app/api.py::_boss_key`。

    ★ 列表卡上的 `works` / `bounds` 不是 `CandidateIn` 的字段：
      `works` 是「公司·职位 / 求职期望」那几行的原文，`bounds` 是屏幕坐标——
      两个都不该进库（前者拆不开，后者换台设备就废）。
    """
    cs = flows.scan_candidates(limit=limit)
    if cs:
        _post("/api/ingest/candidates", [{
            "name": c["name"],
            "job_title": job_title,
            "summary": c.get("summary", ""),
            "tags": c.get("tags", []),
            "active": c.get("active", ""),
        } for c in cs])
    log(f"  「{job_title}」推荐 {len(cs)} 人")
    return cs


def greet_candidates(collected: list[tuple[str, str]]) -> int:
    """给本轮采到的候选人打招呼。★ **写操作**——会真的发消息。

    - 只在 `GREET_ENABLED` 时才被调用；`GREET_DRY_RUN=1` 时只干跑
    - 只挑**此刻在「牛人」当屏**的：`flows.greet()` 不往回滚，回滚会刷新推荐流
      （实测代价见 flows.py 里那段注释）
    - 单个失败只记日志、继续下一个
    - 上限 `GREET_CAP`；每次真发后停 20 秒
    """
    if not collected:
        log("  没有可打招呼的候选人")
        return 0

    xml = phone.ensure_tab("牛人")
    on_screen = [(n, j) for n, j in collected if phone.find_exact(xml, n)]
    off = len(collected) - len(on_screen)
    if off:
        log(f"  {off} 人不在当屏（不往回滚，跳过）")

    sent = 0
    for name, job_title in on_screen:
        if sent >= GREET_CAP:
            log(f"  已达本轮上限 {GREET_CAP}")
            break

        # ★ 真发前必须先过服务端联锁（风控熔断 → 重复打招呼 → 扣配额）。
        #   fail-closed：服务端不可达就不发——宁可少发一条，也不要绕过闸门。
        #   干跑不调它（不消耗配额）。
        if not GREET_DRY_RUN:
            try:
                chk = _post("/api/greet/check", {"boss_name": name, "job_title": job_title})
            except Exception as exc:  # noqa: BLE001
                log(f"  服务端联锁不可用（{exc}）——本轮跳过「{name}」")
                continue
            if not chk.get("allowed"):
                log(f"  服务端拒绝给「{name}」打招呼：{chk.get('reason')}")
                continue

        try:
            res = flows.greet(name, allow_send=not GREET_DRY_RUN)
        except flows.FlowMismatch as exc:
            log(f"  给「{name}」打招呼失败：{exc}")
            continue
        if res.get("skipped"):
            log(f"  「{name}」已在本地账本里（{res.get('first_at')}），跳过")
            continue
        if res.get("dry_run"):
            log(f"  [干跑] 将发给「{res['candidate']}」按钮坐标 {res['would_tap']}")
            continue
        if not res.get("sent"):
            log(f"  「{name}」结果未知：{res}")
            continue

        sent += 1
        log(f"  ✅ 已发给「{res['candidate']}」（屏上我方消息 {res['messages_on_screen']} 条）")
        boss_name = str(res["candidate"]).split("｜")[0]
        for path, payload in (
            ("/api/ingest/greet-log", [{"boss_key": res["candidate"], "job": job_title}]),
            ("/api/ingest/interactions", [{
                "boss_name": boss_name,
                "job_title": job_title,
                "direction": "out",
                "msg_type": "greet",
                "content": "",
            }]),
        ):
            try:
                _post(path, payload)
            except Exception as exc:  # noqa: BLE001
                log(f"  （{path} 上报失败：{exc}）")
        time.sleep(20)

    if sent:
        log(f"  本轮共发出 {sent} 条招呼")
    return sent


def _back_to_main() -> None:
    """退回 BOSS 主界面（MainActivity）。

    ★ 普通页面按 BACK 就退；但「分享到 UC」后手机会留在 UC 浏览器里，
      那里的 BACK 是网页后退、永远退不回 BOSS——这时 monkey 把 BOSS
      拉回前台（可能停在聊天页），再按几下 BACK 回主界面。纯导航无触达。
    """
    for _ in range(6):
        if phone.current_activity().endswith("MainActivity"):
            return
        phone.back()
    phone.open_app(flows.PKG)
    for _ in range(6):
        if phone.current_activity().endswith("MainActivity"):
            return
        phone.back()
    log(f"  （退不回主界面，当前 {phone.current_activity()}）")


def _load_resumes() -> dict:
    if RESUME_LEDGER.exists():
        try:
            return json.loads(RESUME_LEDGER.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def _save_resumes(data: dict) -> None:
    RESUME_LEDGER.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


#: 列表预览里「附件简历」相关文案的匹配：offer 态是「对方想发送附件简历给您」，
#:   投递后是「对方已投递简历」，另外文件名可能带 .pdf/.docx 后缀。
_ATTACH_PREVIEW_RE = re.compile(r"\.(pdf|docx?|wps)\b|附件简历|投递", re.IGNORECASE)


def _server_pending_resumes() -> list[str]:
    """服务端账本里「已收到附件简历、还没落 MinIO」的候选人名。

    ★ 列表预览文案会变（offer 时「对方想发送附件简历」、投递后「对方已投递简历」），
      纯靠预览子串匹配会静默漏人（2026-10-20 实测：任香帅/陈伟伟/卞志超三份附件
      一整轮都没被拾取）。服务端 status=resume_received 且 minio_object_key 为空
      = 附件到了但还没归档，这才是权威账本。查不到就退回预览兜底（返回空表）。
    """
    try:
        data = _get("/api/hr/candidates?status=resume_received&limit=200")
    except Exception as exc:  # noqa: BLE001
        log(f"  查服务端待归档候选人失败（退回列表预览兜底）：{exc}")
        return []
    return [
        it["name"] for it in data.get("items", [])
        if not it.get("minio_object_key") and it.get("name")
    ]


def resume_work(approve: bool = True) -> int:
    """同意「对方想发送简历」的请求，并下载已投递的附件简历。

    ★ 只处理**候选人主动提出**的简历；主动「求简历」是另一种触达，本轮不做。
    ★ 每轮最多 `RESUME_CAP` 个：附件链路（预览 → 分享 → UC → pull）一步 30–60 秒。
    ★ approve=False 时跳过「点同意」那段（Pi 已用 gated_tap 点过），只下载+上传。
    """
    # ★ Pi 轮次结束时列表常停在「筛选·未读」态（Pi 处理未读后的残留），
    #   已读会话（刚投递简历的）会被筛掉、扫多少屏都看不到——先退主界面、
    #   清筛选、切「全部」子 tab 再扫（都是只读导航，无触达）。
    #   ★ Pi 结束可能停在会话/简历页（没有底部 tab），必须先 ensure_main_tabs
    #   再动筛选，否则 ensure_tab 的 goto_tab 会抛 RuntimeError。
    #   ★ 甚至可能停在 UC 浏览器（上一轮「分享到 UC」后 pull 失败留下的）——
    #   BACK 退不出 UC，此时 open_app 把 BOSS 拉回前台再退（只读导航，无触达）。
    try:
        result = flows.ensure_main_tabs()
        if not result["ok"]:
            log(f"  退不回主界面（{result.get('seen', [])}），open_app 拉回 BOSS 再试")
            phone.open_app(flows.PKG)
            result = flows.ensure_main_tabs()
        if not result["ok"]:
            log(f"  仍退不回主界面，按当前列表继续扫：{result}")
        else:
            flows.clear_message_filter()
            flows.switch_sub_tab("全部")
    except (flows.FlowMismatch, RuntimeError) as exc:
        log(f"  清筛选/切「全部」失败，按当前列表继续扫：{exc}")
    convs = flows.scan_conversations(limit=RESUME_SCAN_LIMIT, max_scroll=RESUME_SCAN_SCROLLS)
    # ★ 列表预览文本有两种形态（实测）：「[附件简历]」或「对方想发送附件简历给您…」，
    #   而且有时是同一行里并排的两个节点——只认「附件简历」这个共同子串最稳。
    offers = [c["name"] for c in convs if "附件简历" in (c.get("last_msg") or "")]
    # ★ 拾取已投递简历以**服务端账本**为准（status=resume_received 且没落 MinIO），
    #   预览正则只当兜底——预览文案在投递后会变，只认预览会静默漏人。
    pending = _server_pending_resumes()
    delivered = [
        (c["name"], (c.get("last_msg") or "").strip())
        for c in convs
        if c["name"] in pending or _ATTACH_PREVIEW_RE.search(c.get("last_msg") or "")
    ]
    log(f"  扫描 {len(convs)} 个会话：附件 offer {len(offers)}，待归档 {len(delivered)}"
        + (f"（服务端待归档 {len(pending)} 人）" if pending else ""))
    if pending:
        seen = {c["name"] for c in convs}
        missed = [n for n in pending if n not in seen]
        if missed:
            log(f"  服务端待归档但本轮列表没扫到：{'、'.join(missed)}")
    if not offers and not delivered:
        return 0

    approved = 0
    if approve:
        for name in offers[:RESUME_CAP]:
            try:
                flows.open_conversation(name)
                if flows.approve_resume_offer():
                    approved += 1
                    log(f"  已同意「{name}」发送附件简历")
            except flows.FlowMismatch as exc:
                log(f"  同意「{name}」的简历请求失败：{exc}")
            finally:
                _back_to_main()

    book = _load_resumes()
    saved = 0
    # ★ 上传 500 = 服务端 MinIO 凭证没配好——此时后续每份 fetch（2–5 分钟）都是
    #   白跑（PDF 落到临时目录即弃）。第一份失败后本轮短路，凭证修好后下一轮自动恢复。
    storage_dead = False
    for name, preview in delivered[:RESUME_CAP]:
        if storage_dead:
            break
        if f"{name}｜{preview}" in book:
            continue
        # ★ fetch_resume 走「分享到 UC→等 PDF」链路，doc 类附件拉不下来；
        #   预览里明说 docx/doc 的就不白开一次会话了，直接转人工。
        if re.search(r"\.(docx?|wps)\b", preview, re.IGNORECASE) and ".pdf" not in preview.lower():
            log(f"  「{name}」投的是 doc 类附件（{preview[:40]}），自动下载暂不支持，转人工")
            continue
        try:
            flows.open_conversation(name)
            got = flows.fetch_resume()
        except (flows.FlowMismatch, RuntimeError) as exc:
            # ★ RuntimeError（AdbError 等）也要接住——单份简历失败不该炸穿整轮补跑
            #   （2026-10-20 实测：pull 中文名失败把后面几份也带崩了）。
            log(f"  取「{name}」的简历失败：{exc}")
            continue
        finally:
            # ★ fetch_resume 会「分享到 UC」——结束时手机常留在 UC 浏览器里，
            #   BACK 退不出（那是浏览器的网页后退，2026-10-20 实测整轮卡死）。
            #   不在 BOSS 包里就 open_app 直接拉回。
            _back_to_main()
            if "InnerUCMobile" in (phone.current_activity() or ""):
                phone.open_app(flows.PKG)

        try:
            _post("/api/ingest/attachment", {
                "boss_name": name,
                "job_title": got.get("job_title", ""),
                "filename": got["filename"],
                "content_b64": base64.b64encode(
                    Path(got["local_path"]).read_bytes()
                ).decode("ascii"),
            })
            book[f"{name}｜{got['filename']}"] = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "size": got["size"],
            }
            _save_resumes(book)
            saved += 1
            log(f"  ✅ 已归档「{name}」的简历：{got['filename']}（{got['size']} 字节）")
        except Exception as exc:  # noqa: BLE001
            detail = ""
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    detail = f" body={exc.read().decode('utf-8', 'replace')[:300]}"
                except Exception:  # noqa: BLE001
                    pass
            log(f"  （「{name}」的简历上传失败：{exc}{detail}）")
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (500, 502):
                storage_dead = True
                log("  服务端存储未就绪（500/502）——本轮剩余候选人跳过 fetch，"
                    "修好 MinIO 凭证后下一轮自动重试")

    log(f"  简历：同意 {approved} 个请求，归档 {saved} 份")
    return approved + saved


def collect_conversations(limit: int = 20) -> int:
    cs = flows.scan_conversations(limit=limit)
    if cs:
        _post("/api/ingest/conversations", [{"boss_name": c["name"], "job_title": c["position"],
                                             "last_msg": c["last_msg"], "status": c["status"],
                                             "boss_time": c["time"], "unread": c["unread"],
                                             "messages": []} for c in cs])
    log(f"  会话 {len(cs)} 个")
    return len(cs)


def _post_conversation_messages(d: dict) -> None:
    _post("/api/ingest/conversations", [{
        "boss_name": d["name"], "job_title": d["position"], "last_msg": "",
        "status": "", "boss_time": "", "unread": 0,
        "messages": [{"direction": m["direction"], "text": m["text"], "boss_y": m["y"]}
                     for m in d["messages"]],
    }])


def _read_unread_deep_scroll(limit: int) -> int:
    """旧深滚兜底：滚回顶部、顺读未读会话（筛不出未读/切不到子 tab 时才走这条）。"""
    done = 0
    for c in flows.scan_conversations(limit=limit):
        if not c.get("unread"):
            continue
        try:
            d = flows.read_conversation(c["name"])
        except flows.FlowMismatch as exc:
            log(f"  读「{c['name']}」失败：{exc}")
            continue
        _post_conversation_messages(d)
        done += 1
    return done


def _read_top_unread(limit: int) -> int:
    """在「已筛未读」列表上读最顶上那条，直到读完或到 `limit`。

    打开会话会把它标已读，返回后它就从筛选列表消失、下一条顶上（实测）。
    """
    done = 0
    tried: set[str] = set()
    while done < limit:
        convs = [c for c in flows.screen_conversations() if c["name"] not in tried]
        if not convs:
            # 当前屏没有还没读的：往下滚一屏；屏不变说明读完了
            before = phone.texts(phone.dump_stable())
            phone.swipe_v(0.5, 0.71, 0.31)
            if phone.texts(phone.dump_stable()) == before:
                break
            continue
        c = convs[0]
        tried.add(c["name"])
        try:
            d = flows.read_conversation(c["name"], from_top=False)
        except flows.FlowMismatch as exc:
            log(f"  读「{c['name']}」失败：{exc}")
            continue
        _post_conversation_messages(d)
        done += 1
    return done


def read_unread_conversations(limit: int = 0) -> int:
    """把未读会话的消息读回来。★ 打开会话会把它标记为已读（BOSS 的行为）。

    「筛选→未读」+ 子 tab 排序（2026-10-20）：
      1. `flows.filter_unread()` 把列表筛成只剩未读；
      2. 按 `UNREAD_SUB_TABS` 顺序（先「仅沟通」再「新招呼」）逐个 `flows.switch_sub_tab`；
      3. 每个子 tab 里读**最顶上**那条，打开会标已读、返回后从筛选列表消失、下一条顶上；
      4. 循环直到读完或到总 `limit`（两个子 tab 共享这份预算）。
    全程不深滚、不 `ensure_main_tabs`（那会清筛选）。筛不出/切不到子 tab 才退回旧深滚路径。
    """
    if limit <= 0:
        limit = UNREAD_LIMIT
    try:
        flows.filter_unread()
    except flows.FlowMismatch as exc:
        log(f"  筛未读失败，退回旧深滚路径：{exc}")
        return _read_unread_deep_scroll(limit)

    done = 0
    for tab in UNREAD_SUB_TABS:
        if done >= limit:
            break
        try:
            flows.switch_sub_tab(tab)
        except flows.FlowMismatch as exc:
            log(f"  切「{tab}」子tab失败，退回旧深滚路径：{exc}")
            return done + _read_unread_deep_scroll(limit - done)
        n = _read_top_unread(limit - done)
        log(f"  「{tab}」读了 {n} 个未读会话")
        done += n
    return done


# ——— 轮次 ———

def _main_round_legacy() -> int:
    log(f"main_round 开始（自动打招呼：{'开' if GREET_ENABLED else '**关**'}）")

    collect_jobs()

    # ★ 顺手补**一个** JD（每轮最多一个）。
    #
    #   为什么 main 也要做这件事：`daily_round` 只在 19:50 跑一次，
    #   而「可用职位」（= 采候选人的前提）要求 JD 读全。
    #   不补的话，全新库上**上午装好的系统要等到晚上 19:50 才有第一个可用职位**，
    #   中间每一轮都在「扫到 3 个职位 → 跳过候选人采集」——看着在跑，其实空转。
    #
    #   一轮补一个：几个职位几轮就齐了，而且每轮只多花 ~60 秒（间隔 900 秒，够）。
    #   抓失败不影响本轮其余部分——JD 抓取是最容易撞上风控/页面不对的那个动作。
    for job in jobs_needing_jd()[:1]:
        try:
            collect_jd(job["title"])
        except flows.FlowMismatch as exc:
            log(f"  抓「{job['title']}」的 JD 失败：{exc}")

    jobs = usable_jobs()
    collected: list[tuple[str, str]] = []  # (姓名, 职位名) —— 供本轮打招呼用
    if not jobs:
        log("  没有可用职位（要 启用 + 开放中 + JD 读全），跳过候选人采集")
    else:
        for job in jobs[:3]:
            try:
                cs = collect_candidates(job["title"])
            except flows.FlowMismatch as exc:
                log(f"  采「{job['title']}」的候选人失败：{exc}")
                continue
            collected += [(c["name"], job["title"]) for c in cs]

    state = load_inbound_state()
    n_unread = collect_conversations()
    if n_unread == 0:
        state["empty_streak"] = state.get("empty_streak", 0) + 1
        state["last_empty_at"] = datetime.now().isoformat()
    else:
        state["empty_streak"] = 0
    save_inbound_state(state)

    if state["empty_streak"] >= BACKOFF_AFTER_EMPTY:
        log(f"  连续 {state['empty_streak']} 轮没读到会话——这一轮不读未读了（退避 {BACKOFF_MINUTES} 分钟）")
    else:
        log(f"  读了 {read_unread_conversations()} 个未读会话")

    try:
        resume_work()
    except flows.FlowMismatch as exc:
        log(f"  简历处理中断：{exc}")

    if not GREET_ENABLED:
        log("  ★ 自动打招呼是关的（BOSS_GREET_ENABLED 未设）——这一轮只采集，不发消息")
    else:
        greet_candidates(collected)
    return 0


def _daily_round_legacy() -> int:
    log("daily_round 开始")
    collect_jobs()
    # ★ 抓的是「在招但 JD 还没读全」的那批，不是「已经可用的」——否则死锁，见 jobs_needing_jd
    for job in jobs_needing_jd()[:5]:
        try:
            collect_jd(job["title"])
        except flows.FlowMismatch as exc:
            log(f"  抓「{job['title']}」的 JD 失败：{exc}")
    collect_conversations(limit=50)

    # ★ 服务端收尾：72h 无人回应关闭 + 延迟补评分（照猎聘的 daily sweep）。
    try:
        swept = _post("/api/ingest/daily-sweep", {})
        log(f"  日常收尾：关闭 {swept.get('closed', 0)} 个，补评分 {swept.get('scored', 0)} 个")
    except Exception as exc:  # noqa: BLE001
        log(f"  日常收尾失败（{exc}）——不阻塞")

    # 配额可见：把今天的打招呼余量打到日志（不自动纠偏，只报告）。
    try:
        ov = _get("/api/hr/overview")
        q = ov.get("today", {})
        log(f"  今日打招呼配额 {q.get('quota_used', '?')}/{q.get('quota_cap', '?')}")
    except Exception as exc:  # noqa: BLE001
        log(f"  取配额失败（{exc}）")

    log("daily_round 结束")
    return 0


def login_round() -> int:
    """★ **人工兜底，不自动登录。**

    BOSS 的登录有三道人工关卡（滑块 / 短信 / 人脸），自动提交验证码
    会直接撞风控。所以这一轮只做一件事：**看看是不是掉登录了**，
    是的话标出来通知人。
    """
    log("login_round 开始（只检查，不动作）")
    st = flows.state()
    texts = " ".join(st.get("seen", []))
    if st.get("risk"):
        mark_login_gate(f"风控页：{st['risk']}")
        log("★ 停在风控页上——等人过")
        return 0
    if "登录/注册" in texts or "请输入您的手机号码" in texts:
        mark_login_gate("掉登录了，需要人工在手机上登")
        log("★ 掉登录了——需要人工")
        return 0
    log("  登录态正常")
    clear_login_gate()
    return 0


def _backfill_round_legacy() -> int:
    log("backfill_round 开始")
    collect_jobs()
    for job in jobs_needing_jd():
        try:
            collect_jd(job["title"])
        except flows.FlowMismatch as exc:
            log(f"  抓「{job['title']}」的 JD 失败：{exc}")
    return 0


def main_round() -> int:
    if USE_PI:
        return _run_pi_round("main_round")
    return _main_round_legacy()


def daily_round() -> int:
    if USE_PI:
        return _run_pi_round("daily_round")
    return _daily_round_legacy()


def backfill_round() -> int:
    if USE_PI:
        return _run_pi_round("backfill_round")
    return _backfill_round_legacy()


def resume_round() -> int:
    """只跑简历补跑（扫描→下载→上传 MinIO），不点同意、不起 Pi。

    ★ 循环收尾（inbound=0）后，积压的附件简历还要继续归档；每次 main_round
      都白跑 9 分钟 Pi 初始化不划算。这个 runner 直接调确定性 resume_work。
      approve=False：同意是触达动作，只能 Pi 用 gated_tap 点，这里绝不碰。
    """
    return resume_work(approve=False)


RUNNERS = {
    "main_round": main_round,
    "daily_round": daily_round,
    "login_round": login_round,
    "backfill_round": backfill_round,
    "resume_round": resume_round,
}


#: 本轮是否真的执行了（拿到全局锁才算）。闸门跳过的轮次不上报 task_logs。
_ran_round = False


def main(argv: list[str]) -> int:
    # ★ Windows 计划任务下 stdout 是 GBK（cp936），日志里的 ✅ / ⚠️ 会 UnicodeEncodeError。
    #   猎聘侧 2026-10-17 因此整轮误报 exit 1（且崩在落账之前），这里提前降级。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    name = argv[1] if len(argv) > 1 else "main_round"

    # ① 轮次名
    if name not in ROUNDS:
        log(f"[skip] 未知轮次 {name!r}，可选 {ROUNDS}")
        return 0
    # ② 工作窗口
    if name not in WINDOW_EXEMPT and not within_window():
        log(f"[skip] 不在工作窗口 {WORK_WINDOW_START[0]:02d}:00–{WORK_WINDOW_END[0]:02d}:00 内")
        return 0
    # ③ 设备：**先**把「用哪台」定死（云机端口动态），**再**看配置有没有问题。
    #    顺序不能反——ensure_device 可能刚把 ANDROID_SERIAL 补上。
    device_denied = ensure_device()
    if device_denied:
        log(f"[skip] 设备：{device_denied}")
        return 0
    problem = device_problem()
    if problem:
        log(f"[skip] 设备有问题：{problem}")
        return 0
    # ④ 登录关卡在途
    if name in ("main_round", "daily_round") and login_gate_in_flight():
        log("[skip] 手机停在风控关卡前等人（boss:login_gate_in_flight）——安静跳过，不动标记")
        return 0
    # ⑤ 有可用职位 —— ★ **只看一眼、不再拦**（2026-10-16 改）
    #
    #   原来这里 `return 0`（照猎聘 `has_active_jobs` 的写法）。在猎聘那套是对的，
    #   因为**它的职位是人手工建的**：库里没有 active 职位，说明这个号确实没在招，
    #   白跑一轮没意义。
    #
    #   BOSS 反过来：**职位就是轮次自己扫出来的**。于是全新库上成了死锁——
    #   闸门说「没有可用职位，别跑」，而不跑就永远不会有可用职位。
    #   第一次上调度就是这么卡住的（18:45:58 `[skip]`，退出码 0，看起来一切正常）。
    #
    #   而且「没有可用职位」也不等于「没事可做」：inbound（读未读会话）完全
    #   不依赖职位。所以这里降级成**只记一笔**，真正该拦的地方在下面
    #   `main_round` 里（没有可用职位就跳过候选人采集那一段）。
    if name == "main_round" and not usable_jobs():
        log("[note] 服务端还没有可用职位（启用 + 开放中 + JD 读全）——"
            "本轮只做采集与未读会话，不采候选人")
    # ⑥ 退避（仅 main）
    if name == "main_round" and should_backoff(load_inbound_state()):
        log("[skip] 空未读退避中")
        return 0
    # ⑦ 全局锁
    try:
        with RoundLock():
            # 记一笔「这轮真的跑了」——闸门跳过的轮次不该上报 task_logs，
            # 否则控制台的运行日志会被 [skip] 刷满。
            global _ran_round
            _ran_round = True
            return RUNNERS[name]()
    except RuntimeError as exc:
        log(f"[skip] {exc}")
        return 0


if __name__ == "__main__":
    started = time.time()
    round_name = sys.argv[1] if len(sys.argv) > 1 else "main_round"
    outcome = "success"
    note = ""
    try:
        code = main(sys.argv)
    except flows.FlowMismatch as exc:
        # ★ 走到风控页 = 立刻标出来等人（铁律 3）。这不是「失败」，是「需要人」。
        if "风控" in str(exc) or "验证码" in str(exc) or "滑块" in str(exc):
            mark_login_gate(str(exc))
            outcome = "failed_needs_manual"
        else:
            outcome = "failed"
        note = str(exc)[:400]
        log(f"流程失配：{exc}")
        code = 1
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        outcome = "failed"
        note = f"{type(exc).__name__}: {exc}"[:400]
        code = 1
    else:
        if code == 124:
            outcome = "timeout"
        elif code != 0:
            outcome = "failed"

    duration = time.time() - started
    log(f"退出码 {code}，耗时 {duration:.1f}s")

    # 上报本轮运行记录（控制台的「运行日志」/「人工队列」读它）。
    # ★ 只报真正跑过的轮次——闸门跳过的别报，否则日志会被 [skip] 刷满。
    # ★ fail-soft：上报失败只记日志，绝不改退出码。
    if _ran_round:
        _write_token_summary(round_name, _pi_tokens, _pi_cost, outcome, duration, note)

    sys.exit(code)
