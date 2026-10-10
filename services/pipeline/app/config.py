"""pipeline 配置（pydantic-settings）。

默认值 = 根 .env.example（本地开发占位值，无真实密钥）：
DATABASE_URL 指向 127.0.0.1:3306/hr_workbuddy，hr_user/hr_dev_pw 与 infra 默认一致。
SCREENING_URL 为 screening 服务地址（T5 起用），本地默认 127.0.0.1:8001。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "mysql+pymysql://hr_user:hr_dev_pw@127.0.0.1:3306/hr_workbuddy"
    redis_url: str = "redis://127.0.0.1:6379/0"
    minio_endpoint: str = "127.0.0.1:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "hr-workbuddy"  # E2E 用独立 bucket（hr-workbuddy-e2e）隔离生产归档
    screening_url: str = "http://127.0.0.1:8001"
    daily_resume_target: int = 50  # 日回收目标（回收端 KPI；未达标日低分简历照常入库）
    baidu_ocr_api_key: str = ""  # 百度 OCR（简历截图 → 文本）
    baidu_ocr_secret_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
