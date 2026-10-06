"""共享任务注册表测试（R8 裁定，真实 Redis）：
write/get 往返、键格式与 pipeline 现实现一致（pipeline:dispatched:{task_id}）、
TTL 72h；在途索引（(type, jc_id) → SET）TTL 1h、in_flight 查询语义。
"""

import os
import random
from uuid import uuid4

import pytest
import redis as redis_lib

from hr_workbuddy import AtomicTask, AtomicTaskType
from hr_workbuddy.task_registry import (
    DISPATCH_KEY_PREFIX,
    DISPATCH_TTL_SECONDS,
    IN_FLIGHT_TTL_SECONDS,
    get_task,
    in_flight,
    in_flight_key,
    write_task,
)

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

# 在途索引键含 jc_id（固定值）——用随机大数隔离：2026-10-06 实测踩中，固定 id 11
# 与开发库真实运行的残留索引（TTL 1h）撞车导致测试误报；uuid 后缀约定不适用于
# (type, jc_id) 键，随机大数等价隔离且自愈。
JC_ID = random.randint(10**9, 10**10)


@pytest.fixture()
def redis_client():
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    assert client.ping(), "真实 Redis 不可达（infra 容器需在跑）"
    return client


def _task(**overrides) -> AtomicTask:
    fields = dict(
        task_id=uuid4(),
        type=AtomicTaskType.CHECK_ATTACHMENT,
        job_id=1,
        job_candidate_id=JC_ID,
        candidate_liepin_id="LP-A",
        context={},
    )
    fields.update(overrides)
    return AtomicTask(**fields)


def _cleanup(client, task: AtomicTask) -> None:
    client.delete(f"{DISPATCH_KEY_PREFIX}{task.task_id}")
    if task.job_candidate_id is not None:
        client.delete(in_flight_key(task.type, task.job_candidate_id))


def test_write_get_roundtrip_and_key_format(redis_client):
    """写后可按 task_id 取回；键格式与 pipeline ArqTaskQueue 现实现逐字一致；TTL 72h。"""
    task = _task()
    try:
        write_task(redis_client, task)
        # 键格式一致性（R8：现实现键格式 pipeline:dispatched:{task_id}）
        assert redis_client.exists(f"{DISPATCH_KEY_PREFIX}{task.task_id}") == 1
        got = get_task(redis_client, task.task_id)
        assert got == task  # JSON 序列化往返（AtomicTask 全字段）
        assert got.type is task.type
        ttl = redis_client.pttl(f"{DISPATCH_KEY_PREFIX}{task.task_id}")
        assert 0 < ttl <= DISPATCH_TTL_SECONDS * 1000
    finally:
        _cleanup(redis_client, task)


def test_get_unknown_returns_none(redis_client):
    assert get_task(redis_client, uuid4()) is None


def test_in_flight_index_and_semantics(redis_client):
    """写后 (type, jc_id) 在途可见、TTL 1h；不同 type / 不同 jc / 无 jc 均为 False。"""
    task = _task()
    try:
        write_task(redis_client, task)
        assert in_flight(redis_client, task.type, JC_ID) is True
        assert in_flight(redis_client, task.type, JC_ID + 1) is False  # 其他 jc
        assert in_flight(redis_client, AtomicTaskType.READ_RESUME, JC_ID) is False  # 其他类型
        ttl = redis_client.pttl(in_flight_key(task.type, JC_ID))
        assert 0 < ttl <= IN_FLIGHT_TTL_SECONDS * 1000
    finally:
        _cleanup(redis_client, task)


def test_no_jc_tasks_have_no_in_flight_entry(redis_client):
    """jc_id=None 的任务（LIST_UNREAD/CHECK_LOGIN）不写索引、不参与在途去重。"""
    task = _task(job_candidate_id=None, type=AtomicTaskType.LIST_UNREAD)
    try:
        write_task(redis_client, task)
        assert get_task(redis_client, task.task_id) == task  # 登记照常
        assert in_flight(redis_client, task.type, None) is False  # 无 jc → 永不去重
    finally:
        _cleanup(redis_client, task)
