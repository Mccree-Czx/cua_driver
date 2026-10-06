"""arq worker：execute_task(ctx, task_payload) -> TaskResult（D1）。

调度链：pipeline 把 AtomicTask JSON 以 job 函数名 "execute_task" 入 arq
（见 pipeline app/task_queue.ARQ_JOB_FUNCTION，两处常量须一致）；本模块
注册同名函数，执行后回调 pipeline（result / artifact），由 pipeline 推进
状态机并入队后继任务。

执行语义（spec §3 路径一 + D1，逐条）：
1. 动作前延时：触达（send_message）10-60s、读操作（其余 5 类）5-15s
   均匀随机——延时源与睡眠均注入（测试置 0，不真 sleep）；
2. 触达动作前令牌桶权威检查（R4 共享 TokenBucket，20/hr，
   key=msg-touch:{date-hour}）：桶耗尽 → 任务以 _defer_by 重排自身
   （arq enqueue_job 的 defer 机制：新 job 计数从零起，不烧 retry 预算），
   返回标记 deferred 的结果——不得失败、不得绕过桶直接发、不回调
   pipeline（deferred 无失败账）；defer 时长 = 距下一小时窗口（桶随
   {date-hour} 键自然回填）；
3. 动作后截图 → verify_success(screenshot, criteria) 经 BrainClient；
   verify 不通过按动作失败处理（见 executor）；
4. 失败重试：effective attempt = task.attempt + job_try - 1（arq 重试不
   重写 payload，靠 job_try 折算）；达 max_attempts=3 → failed_needs_manual
   不再重试；否则 failed_retryable 回调后 raise Retry（arq 按 max_tries=4
   配置重跑，1 初跑 + 3 重试）。例外（真实模式门禁 ①）：SEND_MESSAGE
   在消息已发出（perform 已返回）之后的任何失败——verify 失败、大脑不可用、
   结果上报失败之外的动作后异常——一律 failed_needs_manual 不 raise Retry：
   arq 同 payload 重跑会再次真实发送同一消息，一人一消息只防第二条任务
   的落库、防不住同一任务的重发；发送前失败（桶耗尽 defer、驱动抛错在
   send 完成前）与结果上报失败（pipeline 幂等兜底）语义不变；
5. 成功 → outcome=success，evidence 按 pipeline 契约组装（executor）；
   artifact 字节（read_online_resume 截图 → kind=snapshot；download_attachment
   简历 → kind=resume + filename）先于结果回调上传（screenshot_keys 需
   取回 object_key）；evidence 另带 attempt（折算后计数）与 duration_s
   （本次执行耗时秒）——pipeline TaskLog 按 (task_id, attempt) 逐次落账；
6. 视觉大脑不可用（BrainUnavailableError）→ deferred 重判（60s 后重排），
   不按动作失败计——与 T8 大脑模块错误策略衔接。SEND_MESSAGE 发送后
   大脑不可用除外（executor 转为 post_send 失败，见 4）。

回调失败（pipeline 不可用）→ Retry 重跑：pipeline 对 result/artifact
回调幂等（Review Focus 5 / 状态检查），重放安全；arq max_tries 为最终
兜底。at-least-once 语义：requeue 与当前 job 完成之间进程崩溃会双跑，
由 pipeline 幂等消化（不重复去重——T8 遗留注记）。

依赖注入：ctx["worker_deps"] 供测试直调（全 mock）；生产由
build_worker_deps 组装（driver/brain 按 CUA_DRIVER_MODE，桶与重排走
ctx["redis"]——arq 注入的 ArqRedis）。

注记：PipelineClient 为同步 httpx——M1 单 worker 串行（max_jobs=1）且
回调为毫秒级局域网请求，阻塞事件循环可接受；回调若慢化再迁 AsyncClient。
"""

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from arq.connections import RedisSettings
from arq.worker import Retry, func

from app.artifacts import Pipeline, PipelineClient
from app.brain.mock import MockBrain
from app.brain.openai_brain import BrainUnavailableError, OpenAIBrain
from app.config import get_settings
from app.cost import usage_evidence
from app.drivers.cua_sdk import CuaLiepinDriver
from app.drivers.fake import FakeLiepinDriver, png_bytes
from app.executor import ExecutorDeps, TaskExecutionError, execute
from app.world import load_world
from hr_workbuddy import AtomicTask, AtomicTaskType, TaskResult
from hr_workbuddy.rate_limit import TokenBucket

ARQ_JOB_FUNCTION = "execute_task"  # 与 pipeline app/task_queue.ARQ_JOB_FUNCTION 逐字一致

TOUCH_DELAY_RANGE = (10.0, 60.0)  # 触达（send_message）
READ_DELAY_RANGE = (5.0, 15.0)  # 读操作（check_login/list_unread/read_resume/check_attachment/download_attachment）
BUCKET_WINDOW_SECONDS = 3600
BUCKET_KEY_PREFIX = "msg-touch"
BRAIN_DEFER_SECONDS = 60  # 大脑不可用 → 60s 后 deferred 重判
MOCK_CAPTURE_SEED = "mock-verify-capture"  # mock 模式动作后截图：确定性 PNG


@dataclass
class WorkerDeps:
    """worker 副作用注入点：执行器 / 回调 / 桶 / 重排 / 延时 / 时钟。"""

    executor: ExecutorDeps
    pipeline: Pipeline
    bucket: TokenBucket
    requeue: Callable[[AtomicTask, int], Awaitable[None]]
    uniform: Callable[[float, float], float] = random.uniform
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    rate_per_hour: int = 20
    now: Callable[[], datetime] = datetime.now
    clock: Callable[[], float] = time.time


def bucket_key(now: datetime) -> str:
    """触达桶键：每小时一个窗口（20/hr 语义随键滚动回填）。"""
    return f"{BUCKET_KEY_PREFIX}:{now:%Y%m%d%H}"


def defer_until_next_hour(now_ts: float) -> int:
    """距下一小时窗口的秒数（桶键滚动即回填；纯函数，测试直调）。"""
    return max(1, int(BUCKET_WINDOW_SECONDS - now_ts % BUCKET_WINDOW_SECONDS))


def _deferred_result(task: AtomicTask, reason: str) -> TaskResult:
    """标记 deferred 的结果：不失败、不回调 pipeline（无失败账）。"""
    return TaskResult(
        task_id=task.task_id,
        outcome="failed_retryable",
        evidence={"deferred": True},
        error=reason,
    )


async def _noop_sleep(_seconds: float) -> None:
    """CUA_E2E_INSTANT：动作前延时置 0（E2E 不真睡）。"""
    return None


class _PassthroughBucket:
    """CUA_E2E_INSTANT：触达令牌桶直通（E2E 不拦发送；生产不启用）。"""

    async def acquire_async(self, key: str, rate_per_window: int, window_seconds: int) -> bool:
        return True


async def run_task(task: AtomicTask, deps: WorkerDeps, *, attempt: int) -> TaskResult:
    """执行单任务（attempt 为折算后的尝试计数；测试直调入口）。"""
    # 1. 动作前延时
    lo, hi = TOUCH_DELAY_RANGE if task.type is AtomicTaskType.SEND_MESSAGE else READ_DELAY_RANGE
    await deps.sleep(deps.uniform(lo, hi))

    # 2. 触达前令牌桶权威检查
    if task.type is AtomicTaskType.SEND_MESSAGE:
        key = bucket_key(deps.now())
        if not await deps.bucket.acquire_async(key, deps.rate_per_hour, BUCKET_WINDOW_SECONDS):
            await deps.requeue(task, defer_until_next_hour(deps.clock()))
            return _deferred_result(task, f"触达令牌桶耗尽（{deps.rate_per_hour}/小时），已 defer 重排")

    # 3. 执行：动作 → 截图 → verify（executor）；duration_s / attempt 随
    #    evidence 传递（pipeline TaskLog 按 (task_id, attempt) 逐次落账）
    start = deps.clock()
    try:
        result = execute(task, deps.executor)
    except BrainUnavailableError as e:
        await deps.requeue(task, BRAIN_DEFER_SECONDS)
        return _deferred_result(task, f"视觉大脑不可用，deferred 重判：{e}")
    except TaskExecutionError as e:
        evidence = usage_evidence(e.usage, deps.executor.price_per_1k_tokens)
        evidence["attempt"] = attempt
        evidence["duration_s"] = round(deps.clock() - start, 3)
        if e.post_send:
            # 消息已发出后的失败（verify 失败 / 大脑不可用 / 动作后异常）：
            # arq 同 payload 重跑会再次真实发送同一消息——一律转人工不重试
            evidence["post_send_failure"] = True
            if e.sent_at is not None:
                evidence["sent_at"] = e.sent_at
            result = TaskResult(
                task_id=task.task_id,
                outcome="failed_needs_manual",
                evidence=evidence,
                error=e.message,
            )
        else:
            outcome = (
                "failed_needs_manual" if attempt >= task.max_attempts else "failed_retryable"
            )
            result = TaskResult(
                task_id=task.task_id,
                outcome=outcome,
                evidence=evidence,
                error=e.message,
            )
    else:
        result.evidence["attempt"] = attempt
        result.evidence["duration_s"] = round(deps.clock() - start, 3)

    # 4. 结果回调（失败亦回调：failed 账目/转人工入口；pipeline 幂等）
    try:
        deps.pipeline.post_result(task.task_id, result)
    except Exception as e:  # 回调失败按重试处理：pipeline 幂等兜底，arq max_tries 为最终兜底
        raise Retry() from e

    # 5. 重试策略：未达上限 → arq 按 max_tries=4 配置重跑（1 初跑 + 3 重试）
    if result.outcome == "failed_retryable":
        raise Retry()
    return result


async def execute_task(ctx: dict, task_payload: dict | AtomicTask) -> TaskResult:
    """arq job 函数：解析 AtomicTask + 折算 attempt + 委托 run_task。"""
    task = AtomicTask.model_validate(task_payload)
    deps = ctx.get("worker_deps")
    if deps is None:
        deps = build_worker_deps(ctx)
    attempt = task.attempt + int(ctx.get("job_try", 1)) - 1
    return await run_task(task, deps, attempt=attempt)


def build_worker_deps(ctx: dict) -> WorkerDeps:
    """生产依赖组装：driver/brain 按 CUA_DRIVER_MODE；桶与重排走 arq 的
    ctx["redis"]（ArqRedis，与 worker 共享连接池）。"""
    settings = get_settings()
    if settings.driver_mode == "mock":
        driver = FakeLiepinDriver(load_world(settings.world_path))
        brain = MockBrain()  # mock 模式默认通过；剧本判定经 script 注入
        capture = lambda: png_bytes(seed=MOCK_CAPTURE_SEED)
    else:
        driver = CuaLiepinDriver()
        brain = OpenAIBrain(
            settings.brain_base_url, settings.brain_api_key, settings.brain_model
        )

        def capture() -> bytes:  # 真实桌面截图注入：待 T12 真实账号冒烟校准
            raise NotImplementedError("真实模式动作后截图：待 T12 冒烟校准（SDK get_desktop_state）")

    pipeline = PipelineClient(settings.pipeline_url)
    redis_pool = ctx["redis"]

    async def requeue(task: AtomicTask, defer_seconds: int) -> None:
        # arq defer 机制：新 job 计数从零起，不烧当前 job 的 retry 预算
        await redis_pool.enqueue_job(
            ARQ_JOB_FUNCTION, task.model_dump(mode="json"), _defer_by=defer_seconds
        )

    deps = WorkerDeps(
        executor=ExecutorDeps(
            driver=driver,
            brain=brain,
            capture=capture,
            upload_artifact=pipeline.post_artifact,
            price_per_1k_tokens=settings.brain_price_per_1k_tokens,
        ),
        pipeline=pipeline,
        bucket=TokenBucket(redis_pool),
        requeue=requeue,
        rate_per_hour=settings.msg_rate_per_hour,
    )
    if settings.e2e_instant:
        return _apply_e2e_instant(deps)
    return deps


def _apply_e2e_instant(deps: WorkerDeps) -> WorkerDeps:
    """M1 E2E 验收开关（CUA_E2E_INSTANT=1）：触达/读动作延时置 0、令牌桶直通。

    走 WorkerDeps 既有注入点（T9 uniform/sleep/bucket），不触碰执行路径；
    仅 E2E 脚本启用，生产默认关闭。
    """
    deps.uniform = lambda lo, hi: 0.0
    deps.sleep = _noop_sleep
    deps.bucket = _PassthroughBucket()
    return deps


class WorkerSettings:
    """arq CLI 入口（`arq app.worker.WorkerSettings`）。

    max_tries=4：arq 0.28 的真实属性（1 初跑 + 3 重试）——注意 arq 0.28
    无 retry_times 属性（旧版语义 max_tries = 1 + retry_times 已并入
    max_tries），WorkerSettings 里的未知属性会被 get_kwargs 静默丢弃。
    4 次总跑与 run_task 的 max_attempts 边界（第 4 跑 → failed_needs_manual）
    对齐；max_jobs=1：单账号串行（spec 推论——触达 ≥5h 主轴，串行从容成立）。
    """

    functions = [func(execute_task, name=ARQ_JOB_FUNCTION)]
    max_tries = 4
    max_jobs = 1
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
