"""OpenAIBrain：OpenAI 兼容视觉客户端，verify(screenshot, criteria) -> bool。

env：CUA_BRAIN_BASE_URL / CUA_BRAIN_API_KEY / CUA_BRAIN_MODEL（默认
deepseek-flash——用户账号实测支持 image 输入；deepseek-v4-pro 为纯文本
模型，不可用于本用途）。截图以 base64 data URL 附给模型，输出约束为
{ok: bool, reason: str}：response_format 用 json_object（T12 实测：当前
DeepSeek API 对 json_schema 返回 400「response_format type is unavailable」），
输出契约由 prompt 声明 + 解析层严格校验兜底；解析容忍 ```json 代码围栏包裹。

T9 扩展：verify_with_usage(screenshot, criteria) -> VerifyVerdict——
返回判定 + 本次调用 token 用量（resp.usage），供 worker 写
evidence.brain_tokens / cost_est；verify 委托其实现（协议不变）。
供应商响应缺 usage 字段时按零用量记账（旧替身/代理兼容），不影响判定。

错误策略（不吞）：非法输出（非 JSON / 字段类型错 / 空 content）与供应商
错误（openai.APIError 族）一律抛 BrainUnavailableError，由调用方
（T9 worker）降级为 deferred 重判；本模块不静默返回 False。
"""

import base64
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, ValidationError

from app.brain.usage import BrainUsage, VerifyVerdict

VERIFY_PROMPT = (
    "你是招聘系统的视觉校验器。请根据截图判断以下标准是否满足，"
    '输出 JSON 对象 {{"ok": bool, "reason": str}}。\n标准：{criteria}'
)

# 输出契约（以 prompt 声明 + 解析层校验兑现；T12 后不再作为 response_format 发出
# ——当前 DeepSeek API 不支持 json_schema）
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["ok", "reason"],
    "additionalProperties": False,
}


class BrainUnavailableError(Exception):
    """大脑不可用：非法输出或供应商错误。调用方降级（deferred 重判），不吞。"""


class BrainVerdict(BaseModel):
    """模型输出约束：{ok: bool, reason: str}。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    reason: str


class OpenAIBrain:
    """OpenAI 兼容视觉大脑；client_factory 供测试注入假客户端，不真打 API。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._model = model
        self._last_usage: BrainUsage | None = None
        self._client = (
            client_factory()
            if client_factory is not None
            else self._build_client(base_url, api_key)
        )

    @staticmethod
    def _build_client(base_url: str, api_key: str) -> Any:
        from openai import OpenAI  # 延迟导入：mock 模式不触发

        return OpenAI(base_url=base_url, api_key=api_key)

    @property
    def last_usage(self) -> BrainUsage | None:
        """最近一次 verify/verify_with_usage 的 token 用量（未调用过为 None）。"""
        return self._last_usage

    def verify(self, screenshot: bytes, criteria: str) -> bool:
        return self.verify_with_usage(screenshot, criteria).ok

    def verify_with_usage(self, screenshot: bytes, criteria: str) -> VerifyVerdict:
        from openai import APIError

        data_url = "data:image/png;base64," + base64.b64encode(screenshot).decode("ascii")
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": VERIFY_PROMPT.format(criteria=criteria)},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                # T12 实测：DeepSeek 当前 API 拒绝 json_schema（400
                # response_format type is unavailable）——json_object 保结构化约束
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content
            verdict = _parse_verdict(content)
        except APIError as e:
            raise BrainUnavailableError(f"供应商错误：{e}") from e
        except (ValidationError, ValueError, TypeError, IndexError, AttributeError) as e:
            raise BrainUnavailableError(f"非法输出：{e}") from e
        self._last_usage = _extract_usage(resp)
        return VerifyVerdict(ok=verdict.ok, usage=self._last_usage)


def _parse_verdict(content: Any) -> BrainVerdict:
    """解析校验输出：容忍 ```json 代码围栏包裹，其余按契约严格校验（extra=forbid）。"""
    text = str(content or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    return BrainVerdict.model_validate_json(text)


def _extract_usage(resp: Any) -> BrainUsage:
    """从供应商响应提取 usage；缺失/None 字段按 0 记账（不吞判定）。"""
    raw = getattr(resp, "usage", None)
    if raw is None:
        return BrainUsage()
    return BrainUsage(
        prompt_tokens=int(getattr(raw, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(raw, "completion_tokens", 0) or 0),
        total_tokens=int(getattr(raw, "total_tokens", 0) or 0),
    )
