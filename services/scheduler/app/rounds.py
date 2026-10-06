"""调度轮次（spec §3 调度机制 + 工作窗口 + 登录健康暂停 + 配额对账 + 在途去重）。

纯逻辑 + 依赖注入（测试直调 round 函数，不依赖真实 APScheduler / HTTP / Redis）：
- pipeline: PipelineApi 替身（生产 = app.pipeline_client.HttpPipeline）
- enqueue: 入队回调（生产 = 共享注册表 + arq；测试 = 记录替身）
- in_flight: 共享注册表在途查询（同一 (type, job_candidate_id) 有在途任务
  则不重复入队——T7 复审 flagged 的巡检去重）
- notifier / gate / now / within_window 均可注入

轮次语义（binding，逐条）：
1. 工作窗口（默认 08:00-20:00 可配）：窗口外各轮次不派发（首行判断）；
2. inbound_round：对每个 active 岗位入队 LIST_UNREAD（无 jc，不参与在途去重，
   重复轮次的多余 READ_RESUME 由 pipeline 幂等吸收）；
3. awaiting_resume_sweep：72h 关闭委托 pipeline（POST /internal/sweeps/stale-awaiting，
   零触达、不受配额门约束）+ 对 awaiting_resume 的 jc 入队 CHECK_ATTACHMENT
   （在途去重后；配额触顶即跳过——评审 Important：附件链由 scheduler 触发，
   属派发半径，quota 门必须覆盖）；
4. login_health_round：入队 CHECK_LOGIN（job_id=0 哨兵，无岗位）；查最近登录态——
   失效 → 暂停全部派发（gate 内存标志）+ 告警「请扫码登录」；有效 → 解除暂停；
   未知（从未检查）→ 不动。暂停时本轮次仍入队 CHECK_LOGIN（扫码恢复的探测路径）；
5. daily_quota_reconcile：对账当日 out 计数（GET /internal/quota/today）vs 日上限，
   触顶 → gate.quota_exhausted + 告警；触顶后 scheduler 侧触达类派发全停：
   deferred_sweep（重判产出 SEND_MESSAGE）与 awaiting_resume_sweep 的
   CHECK_ATTACHMENT（附件→下载链源头）均跳过；inbound 链
   （LIST_UNREAD→READ_RESUME→SEND_MESSAGE）由 pipeline 编排入队，
   在 scheduler 半径外——披露，M2 动态阈值/配额联调时补；
6. deferred_sweep：委托 pipeline 延期重判（pipeline 在 commit 后入队 SEND_MESSAGE）。
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Callable
from uuid import uuid4

import redis
from arq.connections import ArqRedis

from hr_workbuddy import AtomicTask, AtomicTaskType
from hr_workbuddy.task_registry import in_flight, write_task

from .notifier import Notifier
from .pipeline_client import PipelineApi

ARQ_JOB_FUNCTION = "execute_task"  # 与 cua-agent worker / pipeline task_queue 逐字一致

LOGIN_PAUSE_REASON = "login"
LOGIN_PAUSE_MESSAGE = "请扫码登录：登录态失效，已暂停全部派发"
QUOTA_EVENT = "quota"

# 调度配置（spec 默认，可 env 覆盖）
DEFAULT_DAILY_MSG_CAP = 240


@dataclass
class Gate:
    """派发门（scheduler 内存标志，测试可注入）：
    paused——登录失效暂停全部派发；quota_exhausted——当日触达配额触顶停触达。"""

    paused: bool = False
    pause_reason: str | None = None
    quota_exhausted: bool = False


@dataclass
class RoundReport:
    """轮次报告：dispatched 为本轮入队数；skipped 为跳过原因（None = 正常跑完）。"""

    name: str
    dispatched: int = 0
    skipped: str | None = None


@dataclass
class RoundDeps:
    """轮次依赖注入点。in_flight / within_window 为 None 时分别表示不去重 / 不限窗口。"""

    pipeline: PipelineApi
    enqueue: Callable[[AtomicTask], None]
    notifier: Notifier
    gate: Gate
    in_flight: Callable[[AtomicTaskType, int], bool] | None = None
    within_window: Callable[[datetime], bool] | None = None
    daily_msg_cap: int = DEFAULT_DAILY_MSG_CAP
    # 注意：default_factory 会被调用一次产出默认值——须返回"可调用对象"本身，
    # 不能写 default_factory=datetime.now（那会产出 datetime 实例，轮次调用 deps.now() 时 TypeError）
    now: Callable[[], datetime] = field(default_factory=lambda: datetime.now)


# —— 工作窗口（纯函数）——


def within_work_window(now: datetime, start: time, end: time) -> bool:
    """[start, end)：08:00 起含，20:00 终不含（20:00 整点起停止派发）。"""
    return start <= now.time() < end


def parse_work_window(start: str, end: str) -> tuple[time, time]:
    """配置串（"08:00"/"20:00"）→ time 对。"""
    return time.fromisoformat(start), time.fromisoformat(end)


def window_checker(start: time, end: time) -> Callable[[datetime], bool]:
    return lambda now: within_work_window(now, start, end)


# —— 轮次公共前闸 ——


def _window_gate(deps: RoundDeps, name: str) -> RoundReport | None:
    """窗口外 → 跳过（不派发）。"""
    if deps.within_window is not None and not deps.within_window(deps.now()):
        return RoundReport(name=name, skipped="outside_work_window")
    return None


def _dispatch_gate(deps: RoundDeps, name: str) -> RoundReport | None:
    """窗口外 / 登录暂停 → 跳过。"""
    report = _window_gate(deps, name)
    if report is not None:
        return report
    if deps.gate.paused:
        return RoundReport(name=name, skipped=f"paused:{deps.gate.pause_reason}")
    return None


# —— 轮次 ——


def inbound_round(deps: RoundDeps) -> RoundReport:
    """对每个 active 岗位入队 LIST_UNREAD（未读会话 → pipeline 编排 READ_RESUME 链）。"""
    report = _dispatch_gate(deps, "inbound_round")
    if report is not None:
        return report
    jobs = [j for j in deps.pipeline.list_jobs() if j.get("status") == "active"]
    for job in jobs:
        deps.enqueue(
            AtomicTask(
                task_id=uuid4(),
                type=AtomicTaskType.LIST_UNREAD,
                job_id=job["id"],
                job_candidate_id=None,
                candidate_liepin_id=None,
                context={},
            )
        )
    return RoundReport(name="inbound_round", dispatched=len(jobs))


def awaiting_resume_sweep(deps: RoundDeps) -> RoundReport:
    """72h 关闭（委托 pipeline，零触达、不受配额门约束）+ 对 awaiting_resume 的 jc
    入队 CHECK_ATTACHMENT（在途去重后）。配额触顶 → 跳过 CHECK_ATTACHMENT 入队：
    附件链由 scheduler 触发（评审 Important），属配额门覆盖范围。"""
    report = _dispatch_gate(deps, "awaiting_resume_sweep")
    if report is not None:
        return report
    deps.pipeline.post_stale_awaiting()
    if deps.gate.quota_exhausted:
        return RoundReport(name="awaiting_resume_sweep", skipped="quota_exhausted")
    dispatched = 0
    for row in deps.pipeline.get_awaiting():
        jc_id = row["id"]
        if deps.in_flight is not None and deps.in_flight(
            AtomicTaskType.CHECK_ATTACHMENT, jc_id
        ):
            continue  # 在途去重：同一 (type, jc) 已有在途任务
        deps.enqueue(
            AtomicTask(
                task_id=uuid4(),
                type=AtomicTaskType.CHECK_ATTACHMENT,
                job_id=row["job_id"],
                job_candidate_id=jc_id,
                candidate_liepin_id=row.get("candidate_liepin_id"),
                context={},
            )
        )
        dispatched += 1
    return RoundReport(name="awaiting_resume_sweep", dispatched=dispatched)


def login_health_round(deps: RoundDeps) -> RoundReport:
    """入队 CHECK_LOGIN + 查最近登录态：失效 → 暂停全部派发 + 告警；有效 → 解除。

    暂停不挡本轮次（窗口判断仍生效）：扫码恢复依赖持续的登录检查。
    """
    report = _window_gate(deps, "login_health_round")
    if report is not None:
        return report
    deps.enqueue(
        AtomicTask(
            task_id=uuid4(),
            type=AtomicTaskType.CHECK_LOGIN,
            job_id=0,  # 哨兵：登录检查无岗位（pipeline 消费方不使用 job_id）
            job_candidate_id=None,
            candidate_liepin_id=None,
            context={},
        )
    )
    state = deps.pipeline.get_login_state()
    is_login = state.get("is_login")
    if is_login is False:
        if not deps.gate.paused:
            deps.notifier.alert(LOGIN_PAUSE_REASON, LOGIN_PAUSE_MESSAGE)
        deps.gate.paused = True
        deps.gate.pause_reason = LOGIN_PAUSE_REASON
    elif is_login is True:
        deps.gate.paused = False
        deps.gate.pause_reason = None
    return RoundReport(name="login_health_round", dispatched=1)


def daily_quota_reconcile(deps: RoundDeps) -> RoundReport:
    """对账当日 out 计数 vs 日上限：触顶 → 停止触达类派发 + 告警（回落即恢复）。"""
    report = _window_gate(deps, "daily_quota_reconcile")
    if report is not None:
        return report
    quota = deps.pipeline.get_quota_today()
    count = quota.get("count", 0)
    if count >= deps.daily_msg_cap:
        if not deps.gate.quota_exhausted:
            deps.notifier.alert(
                QUOTA_EVENT,
                f"当日触达配额已用尽（{count}/{deps.daily_msg_cap}），停止触达类任务派发",
            )
        deps.gate.quota_exhausted = True
    else:
        deps.gate.quota_exhausted = False
    return RoundReport(name="daily_quota_reconcile")


def deferred_sweep(deps: RoundDeps) -> RoundReport:
    """延期重判扫描（委托 pipeline）。重判会产出 SEND_MESSAGE（触达）——
    配额触顶即跳过（M1 停止触达类派发的卡点）。"""
    report = _dispatch_gate(deps, "deferred_sweep")
    if report is not None:
        return report
    if deps.gate.quota_exhausted:
        return RoundReport(name="deferred_sweep", skipped="quota_exhausted")
    deps.pipeline.post_deferred()
    return RoundReport(name="deferred_sweep")


# —— 生产依赖组装（main.py 调用；测试不依赖）——


def build_in_flight(redis_url: str) -> Callable[[AtomicTaskType, int], bool]:
    """共享注册表在途查询（与 pipeline ArqTaskQueue 同一注册表，R8）。"""
    registry = redis.Redis.from_url(redis_url, decode_responses=True)
    return lambda task_type, jc_id: in_flight(registry, task_type, jc_id)


def build_enqueuer(redis_url: str) -> Callable[[AtomicTask], None]:
    """入队：先写共享注册表（回调路由 + 在途索引，R8）再 arq 投递。

    job id 用 task_id（pipeline ArqTaskQueue 的防重复手法，沿用）；arq 客户端
    绑定事件循环，每次入队新建客户端（独立 loop、单次 TCP）——与 pipeline 同模式。
    """
    registry = redis.Redis.from_url(redis_url, decode_responses=True)

    def enqueue(task: AtomicTask) -> None:
        write_task(registry, task)
        _enqueue_arq(redis_url, task)

    return enqueue


def _enqueue_arq(redis_url: str, task: AtomicTask) -> None:
    async def _enqueue() -> None:
        # arq 0.28：ArqRedis 首参是 pool_or_conn，位置传 DSN 会被塞进
        # connection_pool 而炸——必须 from_url（T11 E2E 真实入队路径发现）
        arq_redis = ArqRedis.from_url(redis_url)
        try:
            await arq_redis.enqueue_job(
                ARQ_JOB_FUNCTION,
                task.model_dump(mode="json"),
                _job_id=str(task.task_id),
            )
        finally:
            await arq_redis.aclose()

    asyncio.run(_enqueue())
