"""M1 E2E 验收（mock 模式，一条命令）：scripts/run_m1_e2e.py

用法：
    uv run python scripts/run_m1_e2e.py        # 一条命令跑全部（剧本 A + B + C + D）
    uv run pytest tests/e2e -m e2e -v          # pytest 包装（同一套逻辑）

装配方式（controller 裁定）：
- 数据面隔离（2026-10-06 教训后改造）：专用测试库 hr_workbuddy_test + 独立 bucket
  hr-workbuddy-e2e + 独立 Redis 库号 /1；指向生产库时拒跑（除非 E2E_ALLOW_PROD=1）
  Redis 用专用变量 E2E_REDIS_URL（默认 /1；指向 /0 拒跑）——2026-10-07 事故：
  环境残留 REDIS_URL 会把 E2E 任务送进生产库号，常驻 real worker 误执行。
  常驻 launchd agent（pipeline/screening/worker/scheduler）在线时拒跑，提示先下线。
- 数据底座复用 compose 栈（MySQL/MinIO/Redis；未起则 docker compose up -d --wait）
- pipeline：真实 uvicorn 子进程（127.0.0.1:8000，启动即 alembic upgrade head）
- screening：tests/e2e/fake_screening.py 假服务（127.0.0.1:8001，按
  liepin_user_id 返回剧本化 ScreeningResult——不真调 LLM、不改 T5 代码）
- scheduler：不起 APScheduler——直调 scheduler_app.rounds.inbound_round /
  awaiting_resume_sweep（真实 Redis/arq 入队；工作窗口注入为恒真）
- cua-agent：arq worker 子进程（CUA_DRIVER_MODE=mock、CUA_WORLD_PATH 指向
  infra/worlds/*.json、CUA_E2E_INSTANT=1——延时置 0 + 令牌桶直通）
- 72h 剧本：DB 时间回拨 job_candidate.resume_requested_at → 调 pipeline
  POST /internal/sweeps/stale-awaiting → 断言 no_response→closed 与边界两向
- 状态清理：每剧本前 TRUNCATE pipeline 各表（FK_CHECKS=0，AUTO_INCREMENT 归 1）、
  E2E 专用 Redis 库 FLUSHDB、删 E2E bucket 的 snapshots/ 与 resumes/ 前缀对象
  ——生产数据面零接触；保证可重复运行
- 子进程在 finally 里杀干净（Windows taskkill / POSIX 进程组信号）；World 剧本文件改动在 finally 复原

断言直查 DB / MinIO / Redis（不经日志）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx
import redis
from minio import Minio
from sqlalchemy import Engine, create_engine, text

from hr_workbuddy import AtomicTaskType
from hr_workbuddy.task_registry import in_flight_key

REPO_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_DIR = REPO_ROOT / "services" / "pipeline"
CUA_DIR = REPO_ROOT / "services" / "cua-agent"
SCHEDULER_DIR = REPO_ROOT / "services" / "scheduler"
E2E_DIR = REPO_ROOT / "tests" / "e2e"
WORLDS_DIR = REPO_ROOT / "infra" / "worlds"
HAPPY_WORLD = WORLDS_DIR / "m1_happy_path.json"
NO_REPLY_WORLD = WORLDS_DIR / "m1_no_reply.json"
DIRECT_INTAKE_WORLD = WORLDS_DIR / "m1_direct_intake.json"
M2_RECOMMENDED_WORLD = WORLDS_DIR / "m2_recommended.json"

# 本机若存在代理环境变量（HTTP_PROXY/HTTPS_PROXY），httpx 默认会走代理，
# 把 127.0.0.1 的内网回调拦成 404（实测：首请求 200、复用连接后续全 404）。
# 兜底把 loopback 加入 NO_PROXY —— 同时被子进程（pipeline/worker/screening）
# 继承；服务侧另有 trust_env=False 的代码级防护。
_no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
_loopback = "127.0.0.1,localhost,::1"
os.environ["NO_PROXY"] = ",".join(p for p in (_no_proxy, _loopback) if p)
os.environ["no_proxy"] = os.environ["NO_PROXY"]

# 连接配置（2026-10-06 隔离改造：E2E 专用库/bucket/Redis 库号，不再碰生产数据面）
DATABASE_URL = os.environ.get(
    "E2E_DATABASE_URL",
    # 与单测共用测试库（均已授权、每轮自重置）；勿与单测并行跑
    "mysql+pymysql://hr_user:hr_dev_pw@127.0.0.1:3306/hr_workbuddy_test",
)
_DB_NAME = DATABASE_URL.rstrip("/").split("/")[-1].split("?")[0]
if _DB_NAME == "hr_workbuddy" and os.environ.get("E2E_ALLOW_PROD") != "1":
    raise SystemExit(
        "拒绝运行：E2E 指向生产库 hr_workbuddy（E2E reset 会清数据）。"
        "确需对生产库跑请显式 E2E_ALLOW_PROD=1（并先做 MySQL/MinIO 快照）。"
    )
# 2026-10-07 加固：E2E 专用变量（勿复用通用 REDIS_URL）——环境残留值（如 shell 导出
# 的 REDIS_URL=redis://localhost:6379，即 /0）会把 E2E 任务送进生产库号，常驻 real
# worker 会抢走并真实模式误执行（当日事故）。
REDIS_URL = os.environ.get("E2E_REDIS_URL", "redis://127.0.0.1:6379/1")
if urlparse(REDIS_URL).path.lstrip("/") in ("", "0") and os.environ.get("E2E_ALLOW_PROD") != "1":
    raise SystemExit(
        "拒绝运行：E2E 指向生产 Redis 库号（/0）——常驻 real worker 会抢任务。"
        "请用 E2E_REDIS_URL 指定测试库（默认 /1）；确需对生产库跑请显式 E2E_ALLOW_PROD=1。"
    )
PIPELINE_URL = os.environ.get("PIPELINE_URL", "http://127.0.0.1:8000")
SCREENING_URL = os.environ.get("SCREENING_URL", "http://127.0.0.1:8001")
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "127.0.0.1:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
BUCKET = os.environ.get("E2E_MINIO_BUCKET", "hr-workbuddy-e2e")  # E2E 独立 bucket

UV = shutil.which("uv") or "uv"

JOB_TITLE = "产品经理"  # binding：PDF 键含 岗位名；验收断言以本标题为基准
RESUME_TIMEOUT = timedelta(hours=72)  # spec §3（与 pipeline orchestrator 一致）
# MinimalResume 恰 7 字段（contracts 决策 10）
RESUME_7_FIELDS = {
    "name",
    "liepin_user_id",
    "education",
    "years_of_experience",
    "city",
    "salary",
    "experience_summary",
}

_LOG_DIR: Path | None = None  # 本次运行的子进程日志目录（失败诊断用）
_PROCS: list["Proc"] = []


# —— 报告 ——


@dataclass
class ScenarioReport:
    """单剧本验收报告（断言全过才产出；失败即抛）。"""

    name: str
    checks: list[str] = field(default_factory=list)


@dataclass
class E2EReport:
    """四剧本总报告。passed = 剧本 A + 剧本 B + 剧本 C + 剧本 D 全部跑完。"""

    scenarios: list[ScenarioReport]
    log_dir: Path

    @property
    def passed(self) -> bool:
        return len(self.scenarios) == 4


def format_report(report: E2EReport) -> str:
    lines = [f"=== M1 E2E 验收（mock 模式）===", f"子进程日志目录：{report.log_dir}"]
    total = len(report.scenarios)
    for i, scenario in enumerate(report.scenarios, 1):
        lines.append(f"[{i}/{total}] {scenario.name}")
        lines.extend(f"  - {check}" for check in scenario.checks)
    names = " + ".join(chr(ord("A") + i) for i in range(total))
    lines.append(f"总体：剧本 {names} 全部通过")
    return "\n".join(lines)


# —— 数据底座：compose 复用 / 端口 / 就绪 ——


def ensure_compose() -> None:
    """数据底座：compose 已在跑则复用；未起则 up -d（幂等）→ 逐一探活。

    不用 `up -d --wait`：minio-init 是一次性容器（建桶后退出 0），
    --wait 会把「exited (0)」判为失败。
    """
    try:
        subprocess.run(
            ["docker", "compose", "-f", "infra/docker-compose.yml", "up", "-d"],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError:
        raise RuntimeError("未找到 docker 命令：请先安装/启动 Docker") from None
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"docker compose 失败：\n{exc.stdout}\n{exc.stderr}") from None
    poll_until(lambda: _fetch_one("SELECT 1 AS n"), timeout=180, what="MySQL 就绪")
    poll_until(
        lambda: _redis().ping(),
        timeout=60,
        what="Redis 就绪",
    )
    poll_until(
        lambda: httpx.get(f"http://{MINIO_ENDPOINT}/minio/health/live", timeout=2).status_code == 200,
        timeout=60,
        what="MinIO 就绪",
    )


def ensure_port_free(port: int) -> None:
    """本脚本自起自停全部服务子进程，不复用外部进程——端口被占即快速失败。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(
                f"端口 {port} 已被占用：请先停掉占用进程（本脚本不经此端口复用外部服务）"
            )


def wait_http(url: str, *, name: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"{name} 未就绪：{url}（{timeout}s 超时）；日志见 {_LOG_DIR}")


def _port_of(url: str) -> int:
    parsed = urlparse(url)
    return parsed.port or (443 if parsed.scheme == "https" else 80)


# —— 子进程装配 ——


@dataclass
class Proc:
    """服务子进程：日志写文件（UTF-8）；stop 杀整棵进程树
    （Windows taskkill / POSIX 进程组 SIGTERM→SIGKILL）。"""

    name: str
    args: list[str]
    cwd: Path
    env: dict[str, str]
    log: Path
    popen: subprocess.Popen | None = None
    _log_fh: object = None

    def start(self) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self._log_fh = open(self.log, "ab")
        self.popen = subprocess.Popen(
            self.args,
            cwd=str(self.cwd),
            env=self.env,
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
            **(
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if os.name == "nt"
                else {"start_new_session": True}  # POSIX：独立进程组，stop 按组杀树
            ),
        )

    def stop(self) -> None:
        if self.popen is None:
            return
        if self.popen.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(self.popen.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )
            else:
                self._kill_group(signal.SIGTERM)
        try:
            self.popen.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                self._kill_group(signal.SIGKILL)  # 超时升级：硬杀整组
                self.popen.wait(timeout=10)
        if self._log_fh is not None:
            self._log_fh.close()  # type: ignore[union-attr]

    def _kill_group(self, sig: int) -> None:
        """POSIX：向子进程组发信号（start_new_session 保证子孙同在组内）。"""
        try:
            os.killpg(os.getpgid(self.popen.pid), sig)  # type: ignore[union-attr]
        except ProcessLookupError:
            pass


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"  # 日志落盘统一 UTF-8（Windows 控制台 GBK 无关）
    return env


def ensure_test_database() -> None:
    """确保 E2E 专用库存在（幂等）：已存在则跳过（无需 CREATE 权限）；

    缺失时尝试创建，失败则提示手工赋权（CREATE DATABASE `{db}` + GRANT）。"""
    from urllib.parse import urlparse

    import pymysql

    parsed = urlparse(DATABASE_URL)
    db_name = parsed.path.lstrip("/")
    if not db_name:
        raise SystemExit(f"无法从 DATABASE_URL 解析库名：{DATABASE_URL}")
    conn = pymysql.connect(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        user=parsed.username or "root",
        password=parsed.password or "",
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute("SHOW DATABASES LIKE %s", (db_name,))
            if cursor.fetchone() is None:
                try:
                    cursor.execute(
                        f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4"
                    )
                except pymysql.err.OperationalError as exc:
                    raise SystemExit(
                        f"E2E 库 {db_name} 不存在且无权限创建（{exc}）；"
                        f"请以 root 执行：CREATE DATABASE `{db_name}`；"
                        f"GRANT ALL PRIVILEGES ON `{db_name}`.* TO 'hr_user'@'%';"
                    ) from exc
        conn.commit()
    finally:
        conn.close()


def ensure_test_bucket() -> None:
    """建 E2E 专用 bucket（幂等）。"""
    client = _minio()
    if not client.bucket_exists(BUCKET):
        client.make_bucket(BUCKET)


def start_pipeline(log_dir: Path) -> Proc:
    env = _env()
    env.update(
        {
            "DATABASE_URL": DATABASE_URL,  # E2E 专用库 hr_workbuddy_e2e（生产隔离，2026-10-06）
            "REDIS_URL": REDIS_URL,
            "MINIO_BUCKET": BUCKET,  # E2E 专用 bucket（生产隔离）
            "SCREENING_URL": SCREENING_URL,  # 指向假 screening（不触真实 LLM）
        }
    )
    proc = Proc(
        name="pipeline",
        args=[
            UV, "run", "uvicorn", "app.main:app",
            "--app-dir", str(PIPELINE_DIR),
            "--host", "127.0.0.1",
            "--port", str(_port_of(PIPELINE_URL)),
            "--log-level", "warning",
        ],
        cwd=REPO_ROOT,
        env=env,
        log=log_dir / "pipeline.log",
    )
    proc.start()
    return proc


def start_fake_screening(log_dir: Path) -> Proc:
    proc = Proc(
        name="fake-screening",
        args=[
            UV, "run", "uvicorn", "fake_screening:app",
            "--app-dir", str(E2E_DIR),
            "--host", "127.0.0.1",
            "--port", str(_port_of(SCREENING_URL)),
            "--log-level", "warning",
        ],
        cwd=REPO_ROOT,
        env=_env(),
        log=log_dir / "fake_screening.log",
    )
    proc.start()
    return proc


def start_worker(world: Path, log_dir: Path) -> Proc:
    env = _env()
    env.update(
        {
            "CUA_DRIVER_MODE": "mock",
            "CUA_WORLD_PATH": str(world),
            "CUA_E2E_INSTANT": "1",  # 触达/读延时置 0 + 令牌桶直通（T11 E2E 开关）
            "CUA_BRAIN_API_KEY": "",  # mock 模式用 MockBrain；双保险不触真实 LLM
            "REDIS_URL": REDIS_URL,
            "PIPELINE_URL": PIPELINE_URL,
            "PYTHONPATH": str(CUA_DIR),  # arq CLI 导入 app.worker 与 cwd 无关
        }
    )
    proc = Proc(
        name=f"cua-agent[{world.name}]",
        args=[UV, "run", "arq", "app.worker.WorkerSettings"],
        cwd=CUA_DIR,
        env=env,
        log=log_dir / f"worker_{world.stem}.log",
    )
    proc.start()
    return proc


def stop_proc(proc: Proc) -> None:
    proc.stop()


# —— scheduler：不起 APScheduler，直调轮次函数（真实 Redis/arq 入队）——


def _register_scheduler_app() -> None:
    """scheduler 的 app 包以 scheduler_app 别名注册（同仓库 tests 既有手法）：
    本进程不导入 pipeline 的 app 包，无撞名，但沿用别名保持约定一致。"""
    pkg_dir = SCHEDULER_DIR / "app"
    spec = importlib.util.spec_from_file_location(
        "scheduler_app",
        pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("scheduler app 包注册失败")
    module = importlib.util.module_from_spec(spec)
    sys.modules["scheduler_app"] = module
    spec.loader.exec_module(module)


_register_scheduler_app()

from scheduler_app.notifier import Notifier  # noqa: E402
from scheduler_app.pipeline_client import HttpPipeline  # noqa: E402
from scheduler_app.rounds import (  # noqa: E402
    Gate,
    RoundDeps,
    awaiting_resume_sweep,
    build_enqueuer,
    build_in_flight,
    inbound_round,
    outbound_round,
)


def _deps() -> RoundDeps:
    """生产同款依赖组装；工作窗口注入恒真（E2E 任意时刻可跑）。

    now 显式传 datetime.now（可调用）：RoundDeps.now 的 default_factory 是
    datetime.now 调用结果而非可调用对象（T10 遗留缺陷，生产 build_deps 同样
    会踩中——见 task-11-report 关注项），此处按字段契约显式传。
    """
    return RoundDeps(
        pipeline=HttpPipeline(PIPELINE_URL),
        enqueue=build_enqueuer(REDIS_URL),
        in_flight=build_in_flight(REDIS_URL),
        notifier=Notifier(),
        gate=Gate(),
        within_window=lambda now: True,
        daily_msg_cap=240,
        outbound_enabled=True,
        outbound_limit_per_round=10,
        now=datetime.now,
    )


# —— 直查 DB / MinIO / Redis（断言不经日志）——


_ENGINE: Engine | None = None


def _engine() -> Engine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_engine(DATABASE_URL, pool_pre_ping=True)
    return _ENGINE


def _fetch_all(sql: str, **params) -> list[dict]:
    with _engine().connect() as conn:  # 每次新事务 = 新快照（MySQL RR 下可见已提交数据）
        return [dict(row) for row in conn.execute(text(sql), params).mappings()]


def _fetch_one(sql: str, **params) -> dict:
    rows = _fetch_all(sql, **params)
    return rows[0] if rows else {}


def _jc(liepin_user_id: str) -> dict:
    return _fetch_one(
        "SELECT jc.id, jc.job_id, jc.match_score, jc.judge_reason, jc.status, "
        "jc.minio_object_key, jc.resume_downloaded_at, jc.resume_requested_at, "
        "c.name, c.snapshot_object_key, c.online_resume_minimal "
        "FROM job_candidate jc JOIN candidates c ON c.id = jc.candidate_id "
        "WHERE c.liepin_user_id = :lid",
        lid=liepin_user_id,
    )


def _interactions(jc_id: int) -> list[dict]:
    return _fetch_all(
        "SELECT direction, msg_type, content FROM interactions "
        "WHERE job_candidate_id = :id ORDER BY id",
        id=jc_id,
    )


def _task_log_count() -> int:
    return int(_fetch_one("SELECT COUNT(*) AS n FROM task_logs")["n"])


def _resume_json(jc: dict) -> dict:
    value = jc["online_resume_minimal"]
    return json.loads(value) if isinstance(value, str) else value


def _rewind(jc_id: int, delta: timedelta) -> None:
    """DB 时间回拨：resume_requested_at = now + delta（72h 剧本）。"""
    with _engine().begin() as conn:
        conn.execute(
            text("UPDATE job_candidate SET resume_requested_at = :t WHERE id = :id"),
            {"t": datetime.now() + delta, "id": jc_id},
        )


def _redis() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def _minio() -> Minio:
    return Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=False,
    )


def reset_state() -> None:
    """状态清理（可重复运行）：TRUNCATE pipeline 各表（AUTO_INCREMENT 归 1）+
    Redis FLUSHDB（清 arq 队列 / 派发登记 / 在途索引 / 登录态 / 令牌桶）+
    删 MinIO snapshots/ 与 resumes/ 前缀对象。"""
    with _engine().begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for table in (
            "task_logs",
            "review_overrides",
            "interactions",
            "job_candidate",
            "candidates",
            "jobs",
        ):
            conn.execute(text(f"TRUNCATE TABLE {table}"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    redis_client = _redis()
    redis_client.flushdb()
    redis_client.close()
    client = _minio()
    if not client.bucket_exists(BUCKET):
        client.make_bucket(BUCKET)
    for prefix in ("snapshots/", "resumes/"):
        for obj in client.list_objects(BUCKET, prefix=prefix, recursive=True):
            client.remove_object(BUCKET, obj.object_name)


# —— World 剧本（worker 每任务重读文件；脚本经文件改写推进 tick）——


def _read_world(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_world(path: Path, world: dict) -> None:
    path.write_text(
        json.dumps(world, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# —— 通用小件 ——


def poll_until(condition: Callable[[], bool], *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if condition():
                return
        except Exception as exc:  # 轮询内的瞬时异常（连接抖动）不立即失败
            last_error = exc
        time.sleep(0.25)
    detail = f"（轮询期最后异常：{last_error}）" if last_error else ""
    raise AssertionError(f"等待超时（{timeout}s）：{what}{detail}；子进程日志见 {_LOG_DIR}")


def _job_payload() -> dict:
    return {
        "title": JOB_TITLE,
        "jd_text": "负责产品规划与迭代，3 年以上产品经验优先",
        "hard_rules": {"min_education": "本科", "min_years": 3},
        "template_msgs": {
            "greet_request": "您好 {name}，看到您在看{title}岗位，方便发一份简历吗？",
            "direct_request": "您好 {name}，感谢关注{title}岗位，方便发一份简历吗？",
        },
        "llm_threshold": 70,
    }


def seed_job() -> int:
    """经 POST /api/jobs 建演示岗位（jd_text/hard_rules/template_msgs/llm_threshold=70）。"""
    resp = httpx.post(f"{PIPELINE_URL}/api/jobs", json=_job_payload(), timeout=10)
    assert resp.status_code == 201, f"建岗位失败 HTTP {resp.status_code}：{resp.text}"
    body = resp.json()
    assert body["llm_threshold"] == 70 and body["status"] == "active"
    return body["id"]


def _post_sweep() -> dict:
    """72h 巡检：pipeline POST /internal/sweeps/stale-awaiting。"""
    resp = httpx.post(f"{PIPELINE_URL}/internal/sweeps/stale-awaiting", timeout=10)
    assert resp.status_code == 200, f"sweep 失败 HTTP {resp.status_code}：{resp.text}"
    return resp.json()


# —— 剧本 A：张伟/LP001（82 > 70）全链路 ——


def scenario_a(log_dir: Path) -> ScenarioReport:
    checks: list[str] = []
    reset_state()
    assert seed_job() == 1, "TRUNCATE 后岗位 id 应为 1（PDF 键断言以 job_id=1 为基准）"
    world = _read_world(HAPPY_WORLD)
    _write_world(HAPPY_WORLD, {**world, "tick": 0})  # 剧本原状复位（中断残留自愈；worker 每任务重读）
    worker = start_worker(HAPPY_WORLD, log_dir)
    try:
        report = inbound_round(_deps())
        assert report.dispatched == 1
        checks.append(f"inbound_round 入队 LIST_UNREAD × {report.dispatched}")
        poll_until(
            lambda: _jc("LP001").get("status") == "awaiting_resume",
            timeout=180,
            what="LP001 走完 new→screened_pass→resume_requested→awaiting_resume（含触达）",
        )
        jc = _jc("LP001")
        # 中间态佐证（状态链 binding：终态以 DB 为准，中间态经字段/行佐证）
        assert jc["match_score"] is None, "前置评分已免：收到简历前 match_score 为空（2026-10-06 策略）"
        assert "评分后移至简历收到后" in (jc["judge_reason"] or ""), "screened_pass 佐证：硬规则通过（未评分）"
        assert jc["resume_requested_at"] is not None, "awaiting_resume 佐证：72h 锚点已落"
        rows = _interactions(jc["id"])
        assert [(r["direction"], r["msg_type"]) for r in rows] == [("out", "direct_request")]
        # 第 1 次巡检：附件尚未送达（tick 0 < 送达 tick 1）→ 无附件不动
        # 读时探测（2026-10-06 分流）的在途键 TTL 1h——清掉让巡检可再派发（mock 时间等价物）
        client = _redis()
        client.delete(in_flight_key(AtomicTaskType.CHECK_ATTACHMENT, jc["id"]))
        client.close()
        baseline = _task_log_count()
        sweep = awaiting_resume_sweep(_deps())
        assert sweep.dispatched == 1
        poll_until(
            lambda: _task_log_count() > baseline,
            timeout=60,
            what="第 1 次 CHECK_ATTACHMENT（无附件）结果回调落 TaskLog",
        )
        assert _jc("LP001").get("status") == "awaiting_resume", "无附件 → 状态不动"
        checks.append("第 1 次巡检：附件未送达 → 仍 awaiting_resume（剧本时间线 tick 语义）")
        # 剧本推进：tick 1 → 附件送达。在途索引 TTL 1h 是真实时间窗——世界时间
        # 推进的 mock 等价物即清掉该索引（否则第 2 次巡检被在途去重跳过）。
        _write_world(HAPPY_WORLD, {**world, "tick": 1})
        client = _redis()
        client.delete(in_flight_key(AtomicTaskType.CHECK_ATTACHMENT, jc["id"]))
        client.close()
        sweep = awaiting_resume_sweep(_deps())
        assert sweep.dispatched == 1
        poll_until(
            lambda: _jc("LP001").get("status") == "resume_received",
            timeout=180,
            what="LP001 到达 resume_received（附件下载归档）",
        )
        jc = _jc("LP001")

        # —— binding 断言（剧本 A）——
        # 1) MinIO 快照键 snapshots/LP001/\d{8}_\d{6}.png
        snapshot = jc["snapshot_object_key"]
        assert snapshot and re.fullmatch(
            r"snapshots/LP001/\d{8}_\d{6}\.png", snapshot
        ), f"快照键不匹配：{snapshot}"
        assert _minio().stat_object(BUCKET, snapshot), f"快照对象不存在：{snapshot}"
        checks.append(f"快照键匹配 snapshots/LP001/\\d{{8}}_\\d{{6}}.png：{snapshot}")
        # 2) online_resume_minimal 恰 7 字段
        resume = _resume_json(jc)
        assert set(resume.keys()) == RESUME_7_FIELDS, f"字段集偏差：{sorted(resume.keys())}"
        checks.append(f"online_resume_minimal 恰 7 字段：{sorted(resume.keys())}")
        # 3) 状态链（终态 DB 为准；中间态经 match_score/锚点/interactions 佐证）
        assert jc["status"] == "resume_received"
        assert jc["resume_downloaded_at"] is not None, "resume_received 佐证：下载时间已落"
        checks.append(
            "状态链 new→screened_pass→resume_requested→awaiting_resume→resume_received"
            "（终态=resume_received；中间态经 硬规则未评分/72h 锚点/interactions 佐证）"
        )
        # 4) PDF 键 resumes/1/LP001/张伟_产品经理_\d{8}.pdf
        pdf = jc["minio_object_key"]
        assert pdf and re.fullmatch(
            rf"resumes/1/LP001/张伟_{JOB_TITLE}_\d{{8}}\.pdf", pdf
        ), f"PDF 键不匹配：{pdf}"
        assert _minio().stat_object(BUCKET, pdf), f"PDF 对象不存在：{pdf}"
        checks.append(f"PDF 键匹配 resumes/1/LP001/张伟_{JOB_TITLE}_\\d{{8}}.pdf：{pdf}")
        # 5) interactions 恰 1 行 out/direct_request + 1 行 in/attachment
        rows = _interactions(jc["id"])
        assert [(r["direction"], r["msg_type"]) for r in rows] == [
            ("out", "direct_request"),
            ("in", "attachment"),
        ], f"interactions 偏差：{rows}"
        checks.append("interactions 恰 1 行 out/direct_request + 1 行 in/attachment")
        # 6) 收到后补评分：match_score=82 + judge_reason（2026-10-06 策略）
        assert jc["match_score"] == 82, "收到后补评分：match_score=82"
        assert jc["judge_reason"], "judge_reason 为空"
        checks.append(f"收到后补评分：match_score=82；judge_reason={jc['judge_reason']}")
        return ScenarioReport(name="剧本 A：张伟/LP001（inbound 直索要 → 收到后评分 82）全链路", checks=checks)
    finally:
        stop_proc(worker)
        _write_world(HAPPY_WORLD, world)  # 剧本复原（tick 归 0，不污染仓库）


# —— 剧本 B：拒绝 / 零触达 / 72h 关闭 ——


def scenario_b(log_dir: Path) -> ScenarioReport:
    checks: list[str] = []
    reset_state()
    assert seed_job() == 1
    world_b = _read_world(NO_REPLY_WORLD)
    _write_world(NO_REPLY_WORLD, {**world_b, "tick": 0})  # 剧本原状复位（可重跑）
    worker = start_worker(NO_REPLY_WORLD, log_dir)
    try:
        report = inbound_round(_deps())
        assert report.dispatched == 1
        poll_until(
            lambda: _jc("LP002").get("status") == "awaiting_resume"
            and _jc("LP003").get("status") == "rejected_hard"
            and _jc("LP004").get("status") == "awaiting_resume",
            timeout=180,
            what="剧本 B 三候选人判定完成（LP002 直索要待回复 / LP003 硬规则拒绝 / LP004 待回复）",
        )
        # —— LP002：直索要（未前置评分）→ 收到附件 → 收到后评分 55 落账（2026-10-06 策略）——
        jc2 = _jc("LP002")
        assert jc2["status"] == "awaiting_resume"
        assert jc2["match_score"] is None, "前置评分已免：收到简历前无分数"
        rows2 = _interactions(jc2["id"])
        assert [(r["direction"], r["msg_type"]) for r in rows2] == [("out", "direct_request")]
        checks.append("LP002 直索要（direct_request）无前置评分 → awaiting_resume")
        # 首次巡检（tick 0）：LP002+LP004 均 awaiting → 各一次 CHECK_ATTACHMENT，无附件不动
        # 读时探测（2026-10-06 分流）的在途键清理（否则首巡被去重跳过）
        client = _redis()
        for jc_id in (jc2["id"], _jc("LP004")["id"]):
            client.delete(in_flight_key(AtomicTaskType.CHECK_ATTACHMENT, jc_id))
        client.close()
        sweep = awaiting_resume_sweep(_deps())
        assert sweep.dispatched == 2, f"LP002+LP004 均处 awaiting（首巡）：{sweep}"
        # 推进 tick 1 → LP002 附件送达；清在途索引（时间推进的 mock 等价 物）后复巡
        _write_world(NO_REPLY_WORLD, {**world_b, "tick": 1})
        client = _redis()
        client.delete(in_flight_key(AtomicTaskType.CHECK_ATTACHMENT, jc2["id"]))
        client.close()
        sweep = awaiting_resume_sweep(_deps())
        assert sweep.dispatched == 1, f"复巡仅 LP002（LP004 在途去重）：{sweep}"
        poll_until(
            lambda: _jc("LP002").get("status") == "resume_received",
            timeout=180,
            what="LP002 到达 resume_received（附件下载归档 + 收到后评分）",
        )
        jc2 = _jc("LP002")
        assert jc2["match_score"] == 55, "收到后补评分：match_score=55"
        assert "评分 55" in (jc2["judge_reason"] or ""), f"judge_reason 偏差：{jc2['judge_reason']}"
        rows2 = _interactions(jc2["id"])
        assert [(r["direction"], r["msg_type"]) for r in rows2] == [
            ("out", "direct_request"),
            ("in", "attachment"),
        ], f"LP002 interactions 偏差：{rows2}"
        checks.append("LP002 收到简历后补评分 55 落账（resume_received + in/attachment）")
        # 硬规则不通过 → rejected_hard 且零触达
        jc3 = _jc("LP003")
        assert jc3["status"] == "rejected_hard"
        assert jc3["judge_reason"] and "硬规则" in jc3["judge_reason"]
        assert _interactions(jc3["id"]) == [], "rejected_hard 零触达：不应有 out 消息"
        checks.append("LP003 硬规则不通过 → rejected_hard，零触达")
        # 永不回复 → awaiting_resume，out 恰 1（直索要）
        jc4 = _jc("LP004")
        assert jc4["status"] == "awaiting_resume"
        rows4 = _interactions(jc4["id"])
        assert [(r["direction"], r["msg_type"]) for r in rows4] == [("out", "direct_request")]
        checks.append("LP004 永不回复 → awaiting_resume，out 消息恰 1（direct_request）")
        # 72h 边界内侧：回拨至 now-72h+5s（余量吸收脚本→pipeline 请求延迟）→ 仍 awaiting。
        # 恰好 72h（严格 < 边界）不关的精确语义由 pipeline 单测 test_72h 注入时钟覆盖；
        # E2E 真实时钟下用 5s 余量断言边界内侧不关。
        _rewind(jc4["id"], timedelta(seconds=5) - RESUME_TIMEOUT)
        resp = _post_sweep()
        assert resp.get("closed") == 0, f"边界内侧不应关闭：{resp}"
        assert _jc("LP004").get("status") == "awaiting_resume"
        checks.append("72h 边界内侧（now-72h+5s）→ sweep → 仍 awaiting_resume（≤72h 不关）")
        # 72h 边界外侧：回拨至 now-72h-1s → no_response→closed，out 仍恰 1（零追发）
        _rewind(jc4["id"], -RESUME_TIMEOUT - timedelta(seconds=1))
        resp = _post_sweep()
        assert resp.get("closed") == 1, f"过期 awaiting 应关闭：{resp}"
        assert _jc("LP004").get("status") == "closed"
        rows4 = _interactions(jc4["id"])
        assert [(r["direction"], r["msg_type"]) for r in rows4] == [("out", "direct_request")]
        checks.append("72h 边界外侧（now-72h-1s）→ no_response→closed，out 消息仍恰 1（零追发）")
        return ScenarioReport(name="剧本 B：硬拒零触达 / 收到后评分 55 / 72h 关闭", checks=checks)
    finally:
        stop_proc(worker)
        _write_world(NO_REPLY_WORLD, world_b)  # 剧本复原（tick 归 0）


# —— 剧本 C：已有简历 → 回执 + 直接入库（硬规则不拦收）——


def scenario_c(log_dir: Path) -> ScenarioReport:
    """LP005（大专/2 年，硬规则本应拒）：主动咨询时已附简历（tick 0 送达）→

    读→CHECK 有附件→回执（out/reply）+ 下载直接入库（不调 screening）→
    收到后补评分 64。对比剧本 B LP003（无附件 + 硬规则不符 → 拒）验证分流。
    """
    checks: list[str] = []
    reset_state()
    assert seed_job() == 1
    world_c = _read_world(DIRECT_INTAKE_WORLD)
    _write_world(DIRECT_INTAKE_WORLD, {**world_c, "tick": 0})  # 剧本原状复位（可重跑）
    worker = start_worker(DIRECT_INTAKE_WORLD, log_dir)
    try:
        report = inbound_round(_deps())
        assert report.dispatched == 1
        poll_until(
            lambda: _jc("LP005").get("status") == "resume_received",
            timeout=180,
            what="LP005 直收入库：new→resume_received（回执 + 附件下载归档）",
        )
        jc = _jc("LP005")
        # 回执（resume_ack 默认模板）+ 附件入库；零索要（无 direct_request）
        rows = _interactions(jc["id"])
        assert [(r["direction"], r["msg_type"]) for r in rows] == [
            ("out", "reply"),
            ("in", "attachment"),
        ], f"LP005 interactions 偏差：{rows}"
        assert rows[0]["content"] == "您好 钱七，已收到您的简历，感谢关注！"
        checks.append("LP005 回执 out/reply（resume_ack）+ in/attachment；零索要")
        # 硬规则不拦收：大专/2 年仍入库（对比剧本 B LP003 硬拒零触达）
        pdf = jc["minio_object_key"]
        assert pdf and re.fullmatch(
            rf"resumes/1/LP005/钱七_{JOB_TITLE}_\d{{8}}\.pdf", pdf
        ), f"PDF 键不匹配：{pdf}"
        assert _minio().stat_object(BUCKET, pdf), f"PDF 对象不存在：{pdf}"
        checks.append(f"硬规则不符（大专/2年）仍直收入库：PDF={pdf}")
        # 收到后补评分 64
        assert jc["match_score"] == 64, f"补评分偏差：{jc['match_score']}"
        assert jc["resume_downloaded_at"] is not None
        checks.append(f"收到后补评分：match_score=64；judge_reason={jc['judge_reason']}")
        return ScenarioReport(
            name="剧本 C：已有简历 → 回执 + 直接入库（硬规则不拦收）", checks=checks
        )
    finally:
        stop_proc(worker)
        _write_world(DIRECT_INTAKE_WORLD, world_c)  # 剧本复原（tick 归 0）


# —— 剧本 D：推荐人 outbound（LIST_RECOMMENDED → 两层判定 → 打招呼索要）——


def scenario_d(log_dir: Path) -> ScenarioReport:
    """LP101（推荐人，两层通过 82）→ 打招呼 greet_request（含岗位名）→ awaited；

    tick1 回传附件 → 复巡下载入库（前置评分 82 保留，无收到后补评）；
    LP102（硬拒）/ LP103（LLM 拒 55）零触达。同时验证 outbound_round（M2 轮次）。
    """
    checks: list[str] = []
    reset_state()
    assert seed_job() == 1
    world_d = _read_world(M2_RECOMMENDED_WORLD)
    _write_world(M2_RECOMMENDED_WORLD, {**world_d, "tick": 0})  # 剧本原状复位（可重跑）
    worker = start_worker(M2_RECOMMENDED_WORLD, log_dir)
    try:
        report = outbound_round(_deps())
        assert report.dispatched == 1, f"outbound_round 应派发 1 个岗位：{report}"
        checks.append("outbound_round 入队 LIST_RECOMMENDED × 1（M2 轮次）")
        poll_until(
            lambda: _jc("LP101").get("status") == "awaiting_resume"
            and _jc("LP102").get("status") == "rejected_hard"
            and _jc("LP103").get("status") == "rejected_llm",
            timeout=180,
            what="剧本 D 三推荐人判定完成（LP101 向TA索要待回复 / LP102 硬拒 / LP103 LLM 拒）",
        )
        jc1 = _jc("LP101")
        assert jc1["match_score"] == 82, "outbound 前置评分保留（两层判定）"
        rows1 = _interactions(jc1["id"])
        assert [(r["direction"], r["msg_type"]) for r in rows1] == [
            ("out", "greet_request")
        ], f"LP101 interactions 偏差：{rows1}"
        assert rows1[0]["content"] == (
            "你好~我这里有个职位很适合你，待遇优厚，了解一下吗？期待回复！\n"
            "我想要一份你的简历，你是否同意？"
        ), "LP101 平台「向TA索要」系统文案（2026-10-07 实测定稿）"
        checks.append("LP101 向TA索要（原生问候+简历请求）→ awaiting_resume；前置评分 82")
        for lid, st in (("LP102", "rejected_hard"), ("LP103", "rejected_llm")):
            jcx = _jc(lid)
            assert jcx["status"] == st, f"{lid} 应为 {st}，实际 {jcx['status']}"
            assert _interactions(jcx["id"]) == [], f"{lid} 零触达"
        checks.append("LP102 硬拒 / LP103 LLM 拒 → 零触达")
        # 附件链：首巡（tick 0）无附件 → tick1 送达 → 复巡（清在途键）下载入库
        baseline = _task_log_count()
        sweep = awaiting_resume_sweep(_deps())
        assert sweep.dispatched == 1, f"LP101 awaiting 首巡：{sweep}"
        poll_until(
            lambda: _task_log_count() > baseline,
            timeout=60,
            what="LP101 首巡 CHECK（无附件）结果落 TaskLog",
        )
        _write_world(M2_RECOMMENDED_WORLD, {**world_d, "tick": 1})  # 附件送达
        client = _redis()
        client.delete(in_flight_key(AtomicTaskType.CHECK_ATTACHMENT, jc1["id"]))
        client.close()
        sweep = awaiting_resume_sweep(_deps())
        assert sweep.dispatched == 1
        poll_until(
            lambda: _jc("LP101").get("status") == "resume_received",
            timeout=180,
            what="LP101 到达 resume_received（打招呼回传附件下载归档）",
        )
        jc1 = _jc("LP101")
        rows1 = _interactions(jc1["id"])
        assert [(r["direction"], r["msg_type"]) for r in rows1] == [
            ("out", "greet_request"),
            ("in", "attachment"),
        ], f"LP101 interactions 偏差：{rows1}"
        pdf = jc1["minio_object_key"]
        assert pdf and re.fullmatch(
            rf"resumes/1/LP101/周九_{JOB_TITLE}_\d{{8}}\.pdf", pdf
        ), f"PDF 键不匹配：{pdf}"
        assert _minio().stat_object(BUCKET, pdf), f"PDF 对象不存在：{pdf}"
        assert jc1["match_score"] == 82, "outbound 前置评分不回退（无收到后补评）"
        checks.append(f"LP101 回传入库：PDF={pdf}（前置评分 82 保留）")
        return ScenarioReport(
            name="剧本 D：推荐人 outbound（两层判定 → 打招呼 → 回传入库）", checks=checks
        )
    finally:
        stop_proc(worker)
        _write_world(M2_RECOMMENDED_WORLD, world_d)  # 剧本复原（tick 归 0）


# —— 总装：一条命令跑全部 ——


LAUNCHD_AGENTS = (
    "com.hr-workbuddy.pipeline",
    "com.hr-workbuddy.screening",
    "com.hr-workbuddy.worker",
    "com.hr-workbuddy.scheduler",
)


def ensure_no_launchd_agents() -> None:
    """E2E 前置：常驻 launchd agent 必须全部下线（尤其 worker/scheduler）——
    2026-10-07 事故：环境残留导致 E2E 任务与常驻 real worker 共享队列，后者
    以真实模式误执行（幸而猎聘窗口不可达、零账户操作）。跑完再 bootstrap 恢复。
    """
    try:
        out = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:  # noqa: BLE001  # 非 macOS / 无 launchctl：跳过
        return
    loaded = [a for a in LAUNCHD_AGENTS if a in out]
    if loaded:
        bootouts = "\n".join(
            f"  launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/{a}.plist"
            for a in loaded
        )
        bootins = "\n".join(
            f"  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{a}.plist"
            for a in loaded
        )
        raise SystemExit(
            f"检测到常驻 agent 在运行：{', '.join(loaded)}\n"
            f"请先下线再跑 E2E（跑完恢复）：\n{bootouts}\n恢复：\n{bootins}"
        )


def run_e2e() -> E2EReport:
    """一条命令跑全部：compose 复用 → 建 E2E 专用库/bucket（生产隔离）→ 起 pipeline / 假
    screening 子进程 → 剧本 A → 剧本 B → 剧本 C → 剧本 D → finally 杀干净全部子进程。
    失败即抛（断言原样上抛）。"""
    global _LOG_DIR, _PROCS
    _LOG_DIR = Path(tempfile.mkdtemp(prefix="m1_e2e_"))
    _PROCS = []
    scenarios: list[ScenarioReport] = []
    try:
        ensure_no_launchd_agents()  # 常驻 agent 下线前置（2026-10-07 竞态事故加固）
        ensure_compose()
        ensure_test_database()  # E2E 专用库（生产隔离）
        ensure_test_bucket()  # E2E 专用 bucket（生产隔离）
        for port in (_port_of(PIPELINE_URL), _port_of(SCREENING_URL)):
            ensure_port_free(port)
        # 子进程逐个登记：任一 start 失败，已起的也走 finally 杀干净
        _PROCS.append(start_pipeline(_LOG_DIR))
        _PROCS.append(start_fake_screening(_LOG_DIR))
        wait_http(f"{PIPELINE_URL}/api/jobs", name="pipeline（启动即 alembic upgrade head）")
        wait_http(f"{SCREENING_URL}/health", name="假 screening")
        scenarios.append(scenario_a(_LOG_DIR))
        scenarios.append(scenario_b(_LOG_DIR))
        scenarios.append(scenario_c(_LOG_DIR))
        scenarios.append(scenario_d(_LOG_DIR))
    finally:
        for proc in reversed(_PROCS):
            stop_proc(proc)
    return E2EReport(scenarios=scenarios, log_dir=_LOG_DIR)


def _dump_proc_logs() -> None:
    if _LOG_DIR is None:
        return
    print(f"\n子进程日志目录：{_LOG_DIR}")
    for proc in _PROCS:
        try:
            content = proc.log.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if content:
            tail = "\n".join(content.splitlines()[-15:])
            print(f"--- {proc.name}（{proc.log.name} 尾部）---\n{tail}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        report = run_e2e()
    except Exception:
        traceback.print_exc()
        _dump_proc_logs()
        print("\nE2E 失败（退出码 1）")
        return 1
    print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
