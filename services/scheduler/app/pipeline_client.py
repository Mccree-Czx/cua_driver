"""scheduler → pipeline HTTP 客户端（轮次数据面）：
GET /api/jobs（active 岗位）、POST /internal/sweeps/{stale-awaiting,deferred}、
GET /internal/{state/login, quota/today, state/awaiting}。

httpx 同步客户端：scheduler 是 APScheduler 阻塞循环（无事件循环），
且 M1 巡检量级（分钟级、局域网）下同步请求可接受。
"""

from typing import Any, Protocol

import httpx

TIMEOUT_SECONDS = 10.0


class PipelineApi(Protocol):
    """轮次依赖的 pipeline 数据面形状。测试用 FakePipelineApi 同形状。"""

    def list_jobs(self) -> list[dict]: ...

    def post_stale_awaiting(self) -> dict: ...

    def post_deferred(self) -> dict: ...

    def get_login_state(self) -> dict: ...

    def get_quota_today(self) -> dict: ...

    def get_awaiting(self) -> list[dict]: ...


class HttpPipeline:
    """httpx 实现。非 2xx 一律抛错（轮次失败由 APScheduler 下轮自愈）。"""

    def __init__(self, base_url: str, timeout: float = TIMEOUT_SECONDS) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def _get(self, path: str) -> Any:
        resp = self._client.get(f"{self._base}{path}")
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str) -> Any:
        resp = self._client.post(f"{self._base}{path}")
        resp.raise_for_status()
        return resp.json()

    def list_jobs(self) -> list[dict]:
        return self._get("/api/jobs")

    def post_stale_awaiting(self) -> dict:
        return self._post("/internal/sweeps/stale-awaiting")

    def post_deferred(self) -> dict:
        return self._post("/internal/sweeps/deferred")

    def get_login_state(self) -> dict:
        return self._get("/internal/state/login")

    def get_quota_today(self) -> dict:
        return self._get("/internal/quota/today")

    def get_awaiting(self) -> list[dict]:
        return self._get("/internal/state/awaiting")
