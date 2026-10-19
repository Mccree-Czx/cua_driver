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

    #: ★ **db 1，不是 db 0** —— 猎聘用的是 db 0。两套同机部署时共用同一个 Redis
    #:   实例，用不同 db 是最省事的一层隔离（键名上还有第二层，见 `app/keys.py`）。
    redis_url: str = "redis://127.0.0.1:6379/1"

    #: 每日打招呼上限。★ **这是个保守占位值，不是实测出来的。**
    #:   猎聘那边的安全值是 35/轮；BOSS 的风控比猎聘重得多（账号刚被判定过异常），
    #:   所以先按很小的数来，等观察一段时间、确认账号稳了再往上调。
    daily_greet_cap: int = 20

    #: 采数端（Mac 上的 agent_boss）用的令牌。**只放 .env，绝不入库、不进 git。**
    #: 留空 = 不校验（仅限本机开发）。生产必须设。
    ingest_token: str = ""

    #: 复用猎聘的 screening 评分服务（197 上的 8001）。BOSS 打分走同一套契约。
    #: 默认值指向本机；生产与 pipeline 一样由环境变量注入。
    screening_url: str = "http://127.0.0.1:8001"

    #: 简历对象存储：**复用 197 上的 MinIO**，但 bucket 与猎聘刻意分开。
    #: ★ 与 DATABASE_URL 同一个理由：一次敲错名字就会覆盖对方的东西。
    minio_endpoint: str = "127.0.0.1:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "boss-workbuddy"


@lru_cache
def get_settings() -> Settings:
    return Settings()
