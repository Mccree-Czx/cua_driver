"""screening 服务 HTTP 客户端（pipeline → POST /screen）。

错误语义：网络层 / HTTP 错误原样上抛（回调失败可见、worker 可重试）；
degraded 是 screening 自身的业务响应（LLM 不可用），客户端不做二次降级。
"""

import httpx

from app.config import get_settings
from hr_workbuddy import ScreenRequest, ScreeningResult


class ScreeningClient:
    def __init__(self, base_url: str | None = None, timeout: float = 60.0) -> None:
        self.base_url = (base_url or get_settings().screening_url).rstrip("/")
        self.timeout = timeout

    def screen(self, request: ScreenRequest) -> ScreeningResult:
        response = httpx.post(
            f"{self.base_url}/screen",
            json=request.model_dump(mode="json"),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return ScreeningResult.model_validate(response.json())
