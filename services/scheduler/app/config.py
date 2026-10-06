"""scheduler 配置（pydantic-settings，全部可 env 覆盖；默认值 = 根 .env.example）。

轮次间隔均可在 env 覆盖（spec：默认 inbound 5min、sweep 10min、
login_health 每窗口首轮+每小时、reconcile 每小时、deferred 30min）。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    redis_url: str = "redis://127.0.0.1:6379/0"
    pipeline_url: str = "http://127.0.0.1:8000"
    work_window_start: str = "08:00"
    work_window_end: str = "20:00"
    msg_rate_per_hour: int = 20
    daily_msg_cap: int = 240
    inbound_interval_seconds: int = 300
    sweep_interval_seconds: int = 600
    login_health_interval_seconds: int = 3600
    reconcile_interval_seconds: int = 3600
    deferred_interval_seconds: int = 1800


@lru_cache
def get_settings() -> Settings:
    return Settings()
