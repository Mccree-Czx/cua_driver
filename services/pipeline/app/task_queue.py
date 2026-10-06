"""任务入队抽象（D1）：enqueue 登记派发并投递 arq；get_dispatched 供结果回调路由。

为什么 TaskQueue 还负责派发登记：`POST /internal/tasks/{id}/result|artifact`
回调体只有 task_id，pipeline 须自证"该任务派发过、且类型 / job_candidate_id
是什么"——登记表即此依据（生产走 Redis，TTL 72h；测试用内存 FakeTaskQueue）。

R8 裁定：登记读写收敛到 contracts `hr_workbuddy.task_registry`（scheduler
入队也写同一注册表）——键格式 / 序列化 / TTL 与现实现逐字一致
（pipeline:dispatched:{task_id}，72h），并随写维护在途索引供 scheduler
巡检去重。本模块只保留 arq 投递（服务级关注，不进 contracts）。

生产入队经 arq（ArqRedis.enqueue_job），job 函数名 "execute_task" 与 cua-agent
worker（T9）注册的函数一致；payload 为 AtomicTask JSON（worker 侧 pydantic 解析）。
"""

import asyncio
from typing import Protocol
from uuid import UUID

import redis
from arq.connections import ArqRedis

from app.config import get_settings
from hr_workbuddy import AtomicTask
from hr_workbuddy.task_registry import get_task, write_task

ARQ_JOB_FUNCTION = "execute_task"  # cua-agent worker（T9）注册的 arq 函数名


class TaskQueue(Protocol):
    """任务入队 + 派发登记。测试用内存替身实现同一形状。"""

    def enqueue(self, task: AtomicTask) -> None: ...

    def get_dispatched(self, task_id: UUID) -> AtomicTask | None: ...


class ArqTaskQueue:
    """生产实现：先 Redis 登记（回调路由依据）再 arq 入队。

    arq 客户端绑定事件循环；M1 入队量级（≤240 消息/天 + 巡检）下每次入队
    新建客户端（独立 loop、单次 TCP）可接受，避免常驻 loop 线程。
    """

    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = redis_url or get_settings().redis_url
        self._registry = redis.Redis.from_url(self.redis_url, decode_responses=True)

    def enqueue(self, task: AtomicTask) -> None:
        write_task(self._registry, task)  # 共享注册表（R8）
        self._enqueue_arq(task)

    def get_dispatched(self, task_id: UUID) -> AtomicTask | None:
        return get_task(self._registry, task_id)

    def _enqueue_arq(self, task: AtomicTask) -> None:
        async def _enqueue() -> None:
            # arq 0.28：ArqRedis 首参是 pool_or_conn，位置传 DSN 会被塞进
            # connection_pool 而炸——必须 from_url（T11 E2E 真实入队路径发现）
            arq_redis = ArqRedis.from_url(self.redis_url)
            try:
                await arq_redis.enqueue_job(
                    ARQ_JOB_FUNCTION,
                    task.model_dump(mode="json"),
                    _job_id=str(task.task_id),
                )
            finally:
                await arq_redis.aclose()

        asyncio.run(_enqueue())
