"""pipeline 回调客户端（D1）：任务结果 + artifact（multipart）HTTP POST。

生产走 httpx.Client（可注入 client_factory 供测试替换）；worker 调用。
错误策略（不吞）：非 2xx 一律抛 PipelineUnavailableError，由 worker 按
重试处理——pipeline 对 result/artifact 回调幂等（Review Focus 5 / 状态
检查），重放安全。

artifact 契约（T7 逐字）：POST /internal/tasks/{task_id}/artifact，
multipart：file（字节）+ task_id + kind（snapshot|resume）+ filename；
响应 {"ok": true, "object_key": key}，object_key 供 worker 写
evidence.screenshot_keys。result 契约：POST /internal/tasks/{task_id}/result，
JSON 体 TaskResult（task_id 与路径一致）。
"""

from typing import Any, Callable, Protocol
from uuid import UUID

from hr_workbuddy import TaskResult

RESULT_TIMEOUT_SECONDS = 10.0
ARTIFACT_TIMEOUT_SECONDS = 30.0


class PipelineUnavailableError(Exception):
    """pipeline 回调失败（非 2xx / 网络错误）。调用方按重试处理，不吞。"""


class Pipeline(Protocol):
    """worker 依赖的回调客户端形状（测试用内存替身实现同一形状）。"""

    def post_result(self, task_id: UUID, result: TaskResult) -> None: ...

    def post_artifact(self, task_id: UUID, kind: str, filename: str, data: bytes) -> str: ...


class PipelineClient:
    """httpx 实现。base_url 为 pipeline 地址（config PIPELINE_URL）。"""

    def __init__(
        self,
        base_url: str,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client_factory = client_factory
        self._client_instance: Any = None

    def _client(self) -> Any:
        if self._client_instance is None:
            if self._client_factory is not None:
                self._client_instance = self._client_factory()
            else:
                import httpx  # 延迟导入：注入替身的测试不触发

                self._client_instance = httpx.Client(timeout=RESULT_TIMEOUT_SECONDS)
        return self._client_instance

    def post_result(self, task_id: UUID, result: TaskResult) -> None:
        resp = self._client().post(
            f"{self._base_url}/internal/tasks/{task_id}/result",
            json=result.model_dump(mode="json"),
            timeout=RESULT_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 300:
            raise PipelineUnavailableError(
                f"结果回调失败 HTTP {resp.status_code}：{resp.text[:200]}"
            )

    def post_artifact(self, task_id: UUID, kind: str, filename: str, data: bytes) -> str:
        resp = self._client().post(
            f"{self._base_url}/internal/tasks/{task_id}/artifact",
            files={"file": (filename, data)},
            data={"task_id": str(task_id), "kind": kind, "filename": filename},
            timeout=ARTIFACT_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 300:
            raise PipelineUnavailableError(
                f"artifact 回调失败 HTTP {resp.status_code}：{resp.text[:200]}"
            )
        body = resp.json()
        return body.get("object_key") or ""
