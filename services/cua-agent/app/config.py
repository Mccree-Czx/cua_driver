"""cua-agent 配置（pydantic-settings，全部可 env 覆盖）。

环境变量名（spec 任务书逐字）：CUA_DRIVER_MODE（mock|real，默认 mock）、
CUA_BRAIN_BASE_URL / CUA_BRAIN_API_KEY / CUA_BRAIN_MODEL、PIPELINE_URL
（回调地址，默认 127.0.0.1:8000）、REDIS_URL。T9 追加：
MSG_RATE_PER_HOUR（触达限频 20/hr，spec §3 逐字）、
CUA_BRAIN_PRICE_PER_1K_TOKENS（成本账目单价，占位 0，T12 校准）、
CUA_WORLD_PATH（mock 模式 World 剧本路径）。T11 追加：
CUA_E2E_INSTANT（M1 E2E 验收开关：动作延时置 0 + 触达令牌桶直通；
仅 E2E 脚本置 1，生产不设）。
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    driver_mode: Literal["mock", "real"] = Field(
        default="mock", validation_alias="CUA_DRIVER_MODE"
    )
    brain_base_url: str = Field(
        default="https://api.deepseek.com", validation_alias="CUA_BRAIN_BASE_URL"
    )
    brain_api_key: str = Field(default="", validation_alias="CUA_BRAIN_API_KEY")
    brain_model: str = Field(default="deepseek-flash", validation_alias="CUA_BRAIN_MODEL")
    brain_price_per_1k_tokens: float = Field(
        default=0.0, validation_alias="CUA_BRAIN_PRICE_PER_1K_TOKENS"
    )
    pipeline_url: str = Field(
        default="http://127.0.0.1:8000", validation_alias="PIPELINE_URL"
    )
    redis_url: str = Field(
        default="redis://127.0.0.1:6379/0", validation_alias="REDIS_URL"
    )
    msg_rate_per_hour: int = Field(default=20, validation_alias="MSG_RATE_PER_HOUR")
    # 风控防线（2026-10-06 实测教训：连续高频操作触发平台安全验证）
    task_gap_seconds: float = Field(
        default=30.0, validation_alias="CUA_TASK_GAP_SECONDS"
    )  # 任务间隔：每个任务结束后的冷却（E2E instant 自动置 0）
    retry_defer_seconds: int = Field(
        default=60, validation_alias="CUA_RETRY_DEFER_SECONDS"
    )  # 失败重试延后：禁止 0 秒快速连重试（E2E instant 自动置 0）
    world_path: str = Field(default="worlds/default.json", validation_alias="CUA_WORLD_PATH")
    e2e_instant: bool = Field(
        default=False, validation_alias="CUA_E2E_INSTANT"
    )  # M1 E2E：延时置 0 + 令牌桶直通（worker.build_worker_deps 消费）


@lru_cache
def get_settings() -> Settings:
    return Settings()
