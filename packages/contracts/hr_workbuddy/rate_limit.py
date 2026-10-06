"""共享 Redis Lua 令牌桶（R4 裁定：T9 worker 权威检查与 T10 scheduler
建议检查共用同一实现，20/hr 语义跨服务一致）。

固定窗口计数器桶：INCR 后超限回退（DECR）——Redis 单线程原子执行 EVAL，
多客户端并发下同一窗口的计数与回退无竞态；key 只经 KEYS[1] 传入（脚本
不硬编码键名，多键安全）；首次 INCR 设 PEXPIRE，窗口自然过期即回填。

正式测试归 T10（含 20/hr 第 21 次拒绝边界）；本模块在 T9 只经最小冒烟
验证（不提交测试文件）。redis 客户端为鸭子类型注入（不依赖 redis 包）：
sync 客户端（redis.Redis）用 acquire，async 客户端（redis.asyncio.Redis /
arq 的 ArqRedis）用 acquire_async——两者提交同一 Lua 脚本。
"""

from typing import Any

TOKEN_BUCKET_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
if current > tonumber(ARGV[1]) then
  redis.call('DECR', KEYS[1])
  return 0
end
return 1
"""


class TokenBucket:
    """固定窗口令牌桶。acquire 成功返回 True 并占 1 令牌；超限返回 False 且不占。"""

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    def acquire(self, key: str, rate_per_window: int, window_seconds: int) -> bool:
        """同步获取（scheduler / 测试直调）。"""
        return bool(
            self._redis.eval(
                TOKEN_BUCKET_LUA,
                1,
                key,
                rate_per_window,
                int(window_seconds * 1000),
            )
        )

    async def acquire_async(self, key: str, rate_per_window: int, window_seconds: int) -> bool:
        """异步获取（worker 内不阻塞事件循环）。"""
        return bool(
            await self._redis.eval(
                TOKEN_BUCKET_LUA,
                1,
                key,
                rate_per_window,
                int(window_seconds * 1000),
            )
        )
