"""screening 配置（pydantic-settings）。

SCREENING_LLM_* 环境变量与根 .env.example 一致；默认模型 deepseek-flash
（controller 更正：真实账号 /v1/models 实测仅有 deepseek-flash / deepseek-v4-pro，
deepseek-chat 不存在）。API key 由用户稍后提供——缺 key 时服务照常启动，
评分走 degraded 路径（provider 鉴权失败被捕获）。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    screening_llm_base_url: str = "https://api.deepseek.com"
    screening_llm_api_key: str = ""
    screening_llm_model: str = "deepseek-flash"


@lru_cache
def get_settings() -> Settings:
    return Settings()
