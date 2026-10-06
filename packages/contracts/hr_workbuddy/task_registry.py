"""共享任务注册表（R8 裁定）：scheduler 与 pipeline 入队时写同一 Redis 注册表，
结果回调靠 task_id → AtomicTask 登记路由（回调体不含任务类型）。

键格式与 pipeline ArqTaskQueue 现实现逐字一致（不迁移存量键）：
- 派发登记：`pipeline:dispatched:{task_id}` → AtomicTask JSON，TTL 72h
  （与 72h 关闭窗口同量级，孤儿登记自愈）；
- 在途索引：`pipeline:dispatched:idx:{type}:{jc_id}` → SET[task_id]，TTL 1h
  ——供 scheduler 巡检在途去重（同一 (type, job_candidate_id) 已有在途任务
  则不重复入队）。TTL 1h 为在途窗口（任务生命周期分钟级、巡检 10min 的
  6 倍）：任务完成后索引自然过期，最坏情形该 jc 最多 1h 内不重复巡检；
  jc_id=None 的任务（LIST_UNREAD / CHECK_LOGIN / READ_RESUME 由 pipeline
  侧幂等吸收）不写索引、不参与去重。

redis 客户端为鸭子类型注入（sync：redis.Redis；与 rate_limit.TokenBucket
同约定，不依赖 redis 包）。写路径幂等：SET 覆盖 + SADD 去重，重复写同
task_id 只刷新 TTL 不重复计数。
"""

from typing import Any
from uuid import UUID

from hr_workbuddy.models import AtomicTask, AtomicTaskType

DISPATCH_KEY_PREFIX = "pipeline:dispatched:"
DISPATCH_TTL_SECONDS = 72 * 3600  # 回调窗口：与 72h 关闭窗口同量级

IN_FLIGHT_KEY_PREFIX = "pipeline:dispatched:idx:"
IN_FLIGHT_TTL_SECONDS = 3600  # 在途去重窗口（巡检 10min × 6，任务生命周期分钟级）


def dispatch_key(task_id: UUID) -> str:
    return f"{DISPATCH_KEY_PREFIX}{task_id}"


def in_flight_key(task_type: AtomicTaskType, job_candidate_id: int) -> str:
    return f"{IN_FLIGHT_KEY_PREFIX}{task_type.value}:{job_candidate_id}"


def write_task(redis_client: Any, task: AtomicTask) -> None:
    """登记派发（回调路由依据）+ 在途索引（scheduler 巡检去重依据）。"""
    redis_client.set(dispatch_key(task.task_id), task.model_dump_json(), ex=DISPATCH_TTL_SECONDS)
    if task.job_candidate_id is not None:
        key = in_flight_key(task.type, task.job_candidate_id)
        redis_client.sadd(key, str(task.task_id))
        redis_client.expire(key, IN_FLIGHT_TTL_SECONDS)


def get_task(redis_client: Any, task_id: UUID) -> AtomicTask | None:
    raw = redis_client.get(dispatch_key(task_id))
    return AtomicTask.model_validate_json(raw) if raw else None


def in_flight(redis_client: Any, task_type: AtomicTaskType, job_candidate_id: int | None) -> bool:
    """同一 (type, job_candidate_id) 是否有在途任务。jc_id=None 的任务不去重（False）。"""
    if job_candidate_id is None:
        return False
    return bool(redis_client.scard(in_flight_key(task_type, job_candidate_id)))
