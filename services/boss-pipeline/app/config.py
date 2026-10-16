"""boss-pipeline 配置（pydantic-settings）。

默认值指向**本机**的 MySQL（自给自足的开发默认）。
生产在 10.99.40.197 上，`DATABASE_URL` 由 systemd unit 注入。

★ 表名/库名与猎聘刻意分开（`boss_workbuddy` vs `hr_workbuddy`）：
  两套系统同机部署，**一次敲错名字的迁移就会打到另一套**。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "mysql+pymysql://boss_user:boss_dev_pw@127.0.0.1:3306/boss_workbuddy"

    #: 采数端（Mac 上的 agent_boss）用的令牌。**只放 .env，绝不入库、不进 git。**
    #: 留空 = 不校验（仅限本机开发）。生产必须设。
    ingest_token: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
