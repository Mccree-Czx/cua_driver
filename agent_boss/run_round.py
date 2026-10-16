#!/usr/bin/env python3
"""BOSS 轮次运行器。

## 与猎聘 `agent/run_round.py` 最大的不同：**不拉 Pi**

猎聘的轮次会 `node pi --append-system-prompt ... -p <轮次提示词>`，让 LLM
**实时决定每一步点哪里**。BOSS 这边不用：`agent_boss/flows.py` 的流程
**全是确定性的**（已在真机上验证过），所以这里直接调函数——
少一个 LLM、少一层不确定、少一份 token 账单。

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
`BOSS_INGEST_TOKEN`、`ANDROID_SERIAL`、`BOSS_GREET_ENABLED`。
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
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

WORK_WINDOW_START = (8, 0)
WORK_WINDOW_END = (23, 0)

ROUNDS = ("main_round", "daily_round", "login_round", "backfill_round")
WINDOW_EXEMPT = ("backfill_round", "login_round")

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


def usable_jobs() -> list[dict]:
    """服务端认定的「可用」职位（启用过 + BOSS 在招 + JD 读全）。

    ★ 判定在**服务端**做（`hr_tools.job_list_active`），这里只是取结果。
      联锁只该有一份，两边各判一次迟早会分叉。
    """
    try:
        return [j for j in _get("/api/jobs").get("items", [])
                if j.get("status") == "active" and j.get("state") in ("开放中", "")
                and j.get("jd_complete")]
    except Exception as exc:
        log(f"取可用职位失败（fail-open，照常跑）：{exc}")
        return []


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


def collect_candidates(job_title: str, limit: int = 10) -> int:
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
    return len(cs)


def collect_conversations(limit: int = 20) -> int:
    cs = flows.scan_conversations(limit=limit)
    if cs:
        _post("/api/ingest/conversations", [{"boss_name": c["name"], "job_title": c["position"],
                                             "last_msg": c["last_msg"], "status": c["status"],
                                             "boss_time": c["time"], "unread": c["unread"],
                                             "messages": []} for c in cs])
    log(f"  会话 {len(cs)} 个")
    return len(cs)


def read_unread_conversations(limit: int = 5) -> int:
    """把未读会话的消息读回来。★ 打开会话会把它标记为已读（BOSS 的行为）。"""
    done = 0
    for c in flows.scan_conversations(limit=limit):
        if not c.get("unread"):
            continue
        try:
            d = flows.read_conversation(c["name"])
        except flows.FlowMismatch as exc:
            log(f"  读「{c['name']}」失败：{exc}")
            continue
        _post("/api/ingest/conversations", [{
            "boss_name": d["name"], "job_title": d["position"], "last_msg": "",
            "status": "", "boss_time": "", "unread": 0,
            "messages": [{"direction": m["direction"], "text": m["text"], "boss_y": m["y"]}
                         for m in d["messages"]],
        }])
        done += 1
    return done


# ——— 轮次 ———

def main_round() -> int:
    log(f"main_round 开始（自动打招呼：{'开' if GREET_ENABLED else '**关**'}）")

    collect_jobs()
    jobs = usable_jobs()
    if not jobs:
        log("  没有可用职位（要 启用 + 开放中 + JD 读全），跳过候选人采集")
    else:
        for job in jobs[:3]:
            try:
                collect_candidates(job["title"])
            except flows.FlowMismatch as exc:
                log(f"  采「{job['title']}」的候选人失败：{exc}")

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

    if not GREET_ENABLED:
        log("  ★ 自动打招呼是关的（BOSS_GREET_ENABLED 未设）——这一轮只采集，不发消息")
    return 0


def daily_round() -> int:
    log("daily_round 开始")
    collect_jobs()
    for job in usable_jobs()[:3]:
        try:
            collect_jd(job["title"])
        except flows.FlowMismatch as exc:
            log(f"  抓「{job['title']}」的 JD 失败：{exc}")
    collect_conversations(limit=50)
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


def backfill_round() -> int:
    log("backfill_round 开始")
    collect_jobs()
    for job in usable_jobs():
        try:
            collect_jd(job["title"])
        except flows.FlowMismatch as exc:
            log(f"  抓「{job['title']}」的 JD 失败：{exc}")
    return 0


RUNNERS = {
    "main_round": main_round,
    "daily_round": daily_round,
    "login_round": login_round,
    "backfill_round": backfill_round,
}


def main(argv: list[str]) -> int:
    name = argv[1] if len(argv) > 1 else "main_round"

    # ① 轮次名
    if name not in ROUNDS:
        log(f"[skip] 未知轮次 {name!r}，可选 {ROUNDS}")
        return 0
    # ② 工作窗口
    if name not in WINDOW_EXEMPT and not within_window():
        log(f"[skip] 不在工作窗口 {WORK_WINDOW_START[0]:02d}:00–{WORK_WINDOW_END[0]:02d}:00 内")
        return 0
    # ③ 设备
    problem = device_problem()
    if problem:
        log(f"[skip] 设备有问题：{problem}")
        return 0
    # ④ 登录关卡在途
    if name in ("main_round", "daily_round") and login_gate_in_flight():
        log("[skip] 手机停在风控关卡前等人（boss:login_gate_in_flight）——安静跳过，不动标记")
        return 0
    # ⑤ 有可用职位（仅 main）
    if name == "main_round" and not usable_jobs():
        log("[skip] 服务端没有可用职位（启用 + 开放中 + JD 读全）")
        return 0
    # ⑥ 退避（仅 main）
    if name == "main_round" and should_backoff(load_inbound_state()):
        log("[skip] 空未读退避中")
        return 0
    # ⑦ 全局锁
    try:
        with RoundLock():
            return RUNNERS[name]()
    except RuntimeError as exc:
        log(f"[skip] {exc}")
        return 0


if __name__ == "__main__":
    started = time.time()
    try:
        code = main(sys.argv)
    except flows.FlowMismatch as exc:
        # ★ 走到风控页 = 立刻标出来等人（铁律 3）。这不是「失败」，是「需要人」。
        if "风控" in str(exc) or "验证码" in str(exc) or "滑块" in str(exc):
            mark_login_gate(str(exc))
        log(f"流程失配：{exc}")
        code = 1
    except Exception:
        traceback.print_exc()
        code = 1
    log(f"退出码 {code}，耗时 {time.time() - started:.1f}s")
    sys.exit(code)
