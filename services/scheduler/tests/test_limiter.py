"""TokenBucket 正式测试（R4 归属，真实 Redis）：
- 1 小时内第 21 次获取失败【Review Focus 4】，且拒绝不占令牌
- 窗口后回填（下一小时键滚动即新桶；PEXPIRE 窗口自然过期）
- TouchBucket 键格式与 cua-agent worker 逐字一致（跨服务 20/hr 同语义）

真实 Redis（127.0.0.1:6379/0，与 infra 一致）；键名带 uuid 后缀，
测试间互不污染，finally 清理。
"""

import os
import uuid

import pytest
import redis as redis_lib
from hr_workbuddy.rate_limit import TokenBucket

from scheduler_app.limiter import BUCKET_WINDOW_SECONDS, TouchBucket, touch_bucket_key

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")


@pytest.fixture()
def redis_client():
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    assert client.ping(), "真实 Redis 不可达（infra 容器需在跑）"
    return client


def _test_keys() -> tuple[str, str]:
    """两个相邻小时的桶键（uuid 隔离，防跨测试/跨运行污染）。"""
    token = uuid.uuid4().hex[:12]
    return (
        f"hrw-limiter-test:{token}:2026100510",
        f"hrw-limiter-test:{token}:2026100511",
    )


def test_20_acquires_succeed_21st_fails(redis_client):
    """【Review Focus 4】1 小时内第 20 次放行、第 21 次拒绝，且拒绝不占令牌。"""
    bucket = TokenBucket(redis_client)
    key, _ = _test_keys()
    try:
        for _ in range(20):
            assert bucket.acquire(key, 20, BUCKET_WINDOW_SECONDS) is True
        assert bucket.acquire(key, 20, BUCKET_WINDOW_SECONDS) is False  # 第 21 次拒绝
        assert redis_client.get(key) == "20"  # 拒绝回退：计数停在 20，不占令牌
        ttl = redis_client.pttl(key)
        assert 0 < ttl <= BUCKET_WINDOW_SECONDS * 1000  # 首次 INCR 设窗口过期，自然回填
    finally:
        redis_client.delete(key)


def test_refill_after_window_rollover(redis_client):
    """窗口后回填：第 1 小时耗尽，第 2 小时（键滚动）即新桶，第 1 次获取成功。"""
    bucket = TokenBucket(redis_client)
    key_h1, key_h2 = _test_keys()
    try:
        for _ in range(20):
            bucket.acquire(key_h1, 20, BUCKET_WINDOW_SECONDS)
        assert bucket.acquire(key_h1, 20, BUCKET_WINDOW_SECONDS) is False  # 小时 1 耗尽
        assert bucket.acquire(key_h2, 20, BUCKET_WINDOW_SECONDS) is True  # 小时 2 回填
        assert redis_client.get(key_h2) == "1"
    finally:
        redis_client.delete(key_h1, key_h2)


def test_touch_bucket_key_matches_worker_format():
    """键格式与 cua-agent worker bucket_key 逐字一致（msg-touch:{YYYYMMDDHH}）。"""
    from datetime import datetime

    assert touch_bucket_key(datetime(2026, 10, 5, 14, 30)) == "msg-touch:2026100514"


def test_touch_bucket_end_to_end(redis_client):
    """TouchBucket 经真实 Redis 获取成功（时钟注入，键确定性；用后删除）。"""
    from datetime import datetime

    fixed_now = datetime(2030, 1, 1, 0, 0)  # 合成小时：不与生产 msg-touch 键撞
    bucket = TouchBucket(redis_client, rate_per_hour=20)
    try:
        assert bucket.acquire(fixed_now) is True
        assert redis_client.get(touch_bucket_key(fixed_now)) == "1"
    finally:
        redis_client.delete(touch_bucket_key(fixed_now))
