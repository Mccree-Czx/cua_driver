"""触达限频（scheduler 侧建议性检查，R4）：与 worker 权威检查共用 contracts
TokenBucket（同一 Lua 脚本）；键格式与 cua-agent worker 逐字一致
（msg-touch:{YYYYMMDDHH}），20/hr 语义跨服务一致。

M1 说明：scheduler 不直接入队触达任务（SEND_MESSAGE 由 pipeline 编排入队），
权威检查在 worker 触达前；本模块为建议检查点 + 正式边界测试
（test_limiter.py，真实 Redis：1 小时内第 21 次获取失败【Review Focus 4】、
窗口后回填）。
"""

from datetime import datetime
from typing import Any

from hr_workbuddy.rate_limit import TokenBucket

BUCKET_WINDOW_SECONDS = 3600
BUCKET_KEY_PREFIX = "msg-touch"  # 与 cua-agent worker.bucket_key 的 BUCKET_KEY_PREFIX 逐字一致


def touch_bucket_key(now: datetime) -> str:
    """触达桶键：每小时一个窗口（20/hr 语义随键滚动回填）。"""
    return f"{BUCKET_KEY_PREFIX}:{now:%Y%m%d%H}"


class TouchBucket:
    """触达令牌桶建议检查：acquire 成功 = 本小时窗口内还有余量（占 1 令牌）。"""

    def __init__(self, redis_client: Any, rate_per_hour: int = 20) -> None:
        self._bucket = TokenBucket(redis_client)
        self.rate_per_hour = rate_per_hour

    def acquire(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        return self._bucket.acquire(
            touch_bucket_key(now), self.rate_per_hour, BUCKET_WINDOW_SECONDS
        )
