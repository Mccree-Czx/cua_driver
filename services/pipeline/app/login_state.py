"""登录态存储（R9 裁定）：CHECK_LOGIN 结果落 Redis 键，
GET /internal/state/login 供 scheduler 查询最近一次结果。

不建 notifications 表（M1 最小）：状态键 + task_results 端点通用 TaskLog
行（R10）即"记录登录态"——outcome=success 且 evidence.logged_in=False
即为失效记录。键无 TTL：最近一次检查始终可查；scheduler 侧对
is_login=None（从未检查）不动作。
"""

import json
from typing import Protocol

import redis

from app.config import get_settings

LOGIN_STATE_KEY = "pipeline:state:login"


class LoginStateStore(Protocol):
    """登录态存储形状：生产 RedisLoginState，测试 FakeLoginState 内存替身。"""

    def get(self) -> dict | None: ...

    def set(self, is_login: bool, task_id: str, checked_at: str) -> None: ...


class RedisLoginState:
    """Redis 实现：pipeline:state:login → {"is_login", "task_id", "checked_at"} JSON。"""

    def __init__(self, redis_url: str | None = None) -> None:
        self._redis = redis.Redis.from_url(
            redis_url or get_settings().redis_url, decode_responses=True
        )

    def get(self) -> dict | None:
        raw = self._redis.get(LOGIN_STATE_KEY)
        return json.loads(raw) if raw else None

    def set(self, is_login: bool, task_id: str, checked_at: str) -> None:
        self._redis.set(
            LOGIN_STATE_KEY,
            json.dumps(
                {"is_login": is_login, "task_id": task_id, "checked_at": checked_at},
                ensure_ascii=False,
            ),
        )
