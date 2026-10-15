"""登录态存储（R9 裁定）：CHECK_LOGIN 结果落 Redis 键，
GET /internal/state/login 供 scheduler 查询最近一次结果。

不建 notifications 表（M1 最小）：状态键 + task_results 端点通用 TaskLog
行（R10）即"记录登录态"——outcome=success 且 evidence.logged_in=False
即为失效记录。键无 TTL：最近一次检查始终可查；scheduler 侧对
is_login=None（从未检查）不动作。

多账号（2026-10-14）：键按账号分片。登录态本来就是账号级的——手机上
同一个时刻只登录一个招聘号，查的是"那个号还在不在线"。
"""

import json
from datetime import datetime
from typing import Protocol

import redis

from app.config import get_settings
from app.keys import login_state_key

__all__ = ["RedisLoginState", "login_state_key"]


class LoginStateStore(Protocol):
    """登录态存储形状：生产 RedisLoginState，测试 FakeLoginState 内存替身。"""

    def get(self, account_id: int) -> dict | None: ...

    def set(
        self, is_login: bool, task_id: str, checked_at: str, account_id: int
    ) -> None: ...

    def clear(self, account_id: int) -> None: ...


class RedisLoginState:
    """Redis 实现：pipeline:state:login:{account_id} → {"is_login","task_id","checked_at"} JSON。"""

    def __init__(self, redis_url: str | None = None) -> None:
        self._redis = redis.Redis.from_url(
            redis_url or get_settings().redis_url, decode_responses=True
        )

    def get(self, account_id: int) -> dict | None:
        raw = self._redis.get(login_state_key(account_id))
        return json.loads(raw) if raw else None

    def set(
        self, is_login: bool, task_id: str, checked_at: str, account_id: int
    ) -> None:
        self._redis.set(
            login_state_key(account_id),
            json.dumps(
                {"is_login": is_login, "task_id": task_id, "checked_at": checked_at},
                ensure_ascii=False,
            ),
        )

    def clear(self, account_id: int) -> None:
        """把某个账号标成**已登出**（写 `is_login=false`）。

        多账号（2026-10-15）：一部手机同一时刻只登一个号，所以换号之后
        旧账号那条必须被清掉——否则三个账号会同时挂着 `is_login: true`
        （实测就是如此），而读它的 `/alerts` 会把几周前的 true 当成「还在线」，
        「掉登录」告警也因此永远不会触发。

        写 false 而不是删键：`checked_at` 要留着，删了就没法区分
        「查过、已登出」和「从来没查过」。
        """
        self.set(
            is_login=False,
            task_id="",
            checked_at=datetime.now().isoformat(timespec="seconds"),
            account_id=account_id,
        )
