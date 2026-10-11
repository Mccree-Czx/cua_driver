"""轮次运行器（P4 外部调度）：cron/定时器调用本脚本触发一个 Pi 轮次。

用法：
  python run_round.py main_round
  python run_round.py daily_round

组装：系统提示 + 轮次指令 → `pi -p`（DeepSeek v4 flash）。
工作窗口（08:00-20:00）判断在脚本层，窗口外直接跳过不启动 Pi。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
SYSTEM_PROMPT = AGENT_DIR / "system-prompt.md"
ROUNDS_FILE = AGENT_DIR / "rounds.md"

# 全局轮次锁：所有轮次共用一部手机，必须串行——同一时刻只允许一个轮次在跑。
# 锁为文件 + 陈旧检测（轮次最长约 5 分钟，10 分钟未动的锁视为陈旧可接管）。
LOCK_FILE = AGENT_DIR / ".round.lock"
LOCK_STALE_SECONDS = 3600

# 空未读退避：连续空轮数达阈值后，30 分钟内跳过 main_round（不启动 Pi）
INBOUND_STATE_FILE = AGENT_DIR / ".inbound_state.json"
BACKOFF_EMPTY_STREAK = 2
BACKOFF_INTERVAL_SECONDS = 1800


def acquire_round_lock() -> bool:
    try:
        if LOCK_FILE.exists():
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age < LOCK_STALE_SECONDS:
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


def parse_round_result(output: str):
    m = re.search(r"@@RESULT@@\s+inbound=(\d+)\s+outbound=(\d+)", output)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _session_dir() -> Path | None:
    """找 Pi 的 session 目录（agent 目录，排除 pi-agent/mobile_spike）。"""
    root = Path.home() / ".pi" / "agent" / "sessions"
    try:
        for d in root.iterdir():
            if d.is_dir() and d.name.endswith("agent--") and "pi-agent" not in d.name:
                return d
    except OSError:
        pass
    return None


def _summarize_tokens(session_file: Path) -> tuple[int, float]:
    """汇总 session jsonl 里最后一条 usage.totalTokens / cost.total（累积值）。"""
    last_tokens = 0
    last_cost = 0.0
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
                if isinstance(u, dict) and "totalTokens" in u:
                    last_tokens = int(u.get("totalTokens", 0) or 0)
                    c = u.get("cost", {})
                    if isinstance(c, dict):
                        last_cost = float(c.get("total", 0) or 0)
                    break
                stack.extend(obj.values())
            elif isinstance(obj, list):
                stack.extend(obj)
    return last_tokens, last_cost


def _write_token_summary(name: str, tokens: int, cost: float) -> None:
    """用项目 python 写一条 token 汇总 TaskLog。"""
    script = (
        "import sys; sys.path.insert(0, 'services/pipeline')\n"
        "from app.db import SessionLocal\n"
        "from app.models import TaskLog\n"
        "from datetime import datetime\n"
        "with SessionLocal() as s:\n"
        f"    s.add(TaskLog(task_id=f'{name}_token_' + datetime.now().strftime('%Y%m%d%H%M%S'), "
        f"outcome='success', tokens={tokens}, cost={cost}))\n"
        "    s.commit()\n"
    )
    cwd = AGENT_DIR.parent
    subprocess.run(
        ["uv", "run", "--package", "hr-workbuddy-pipeline", "python", "-c", script],
        cwd=str(cwd), capture_output=True, timeout=60,
    )

# Windows 下 npm 的 pi.cmd shim 不能被 CreateProcess 直跑——用 node + JS 入口
PI_CLI = (
    Path(os.environ.get("APPDATA", ""))
    / "npm/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
)

PROVIDER = "deepseek"
MODEL = "deepseek-flash"


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
WORK_WINDOW_END = (20, 0)

# rounds.md 中的轮次标题 → 节号（解析用）
ROUND_SECTIONS = [
    ("main_round", "## 1."),
    ("daily_round", "## 2."),
]


def within_window(now: datetime) -> bool:
    start = now.replace(hour=WORK_WINDOW_START[0], minute=WORK_WINDOW_START[1], second=0)
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
    if not within_window(now):
        print(f"[skip] {name}: 工作窗口外（{now:%H:%M}）")
        return 0
    if name == "main_round" and should_backoff():
        print(f"[skip] {name}: 空未读退避（连续空，30 分钟内不再启动 Pi）")
        return 0
    if not acquire_round_lock():
        print(f"[skip] {name}: 另一轮次正在运行（全局轮次锁）")
        return 0
    try:
        prompt = extract_round_prompt(name)
        cmd = [
            "node", str(PI_CLI),
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
                result = subprocess.run(
                    cmd,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=str(AGENT_DIR),
                    timeout=900,
                )
        except subprocess.TimeoutExpired:
            print(f"[{name}] 超时（900s）强制结束，释放锁")
            return 124
        output = log_path.read_text(encoding="utf-8")
        if output:
            print(output, end="")
        print(f"[{name}] 退出码 {result.returncode}，耗时 {time.time()-started:.0f}s")
        if sdir:
            try:
                new_files = [f for f in sdir.glob("*.jsonl") if f.stat().st_mtime > before_mtime]
            except OSError:
                new_files = []
            if new_files:
                latest = max(new_files, key=lambda f: f.stat().st_mtime)
                tokens, cost = _summarize_tokens(latest)
                print(f"[{name}] tokens={tokens}, cost={cost:.6f}")
                _write_token_summary(name, tokens, cost)
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
