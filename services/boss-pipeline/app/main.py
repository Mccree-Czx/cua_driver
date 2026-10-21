"""boss-pipeline 入口：启动前执行迁移 + FastAPI 装配。

跟猎聘的 `services/pipeline/app/main.py` 同构：**Alembic 是本服务 schema 的唯一 owner**，
应用启动前 `alembic upgrade head`（幂等；失败即抛错，拒绝带旧 schema 启动）。

`script_location` 显式指到绝对路径，与进程 cwd 无关——
猎聘那边踩过：alembic 的 `env_file=".env"` 是按 CWD 解析的，从别处跑会静默读到默认值。

启动：
    cd services/boss-pipeline && uv run uvicorn app.main:app --port 8100

★ 端口用 **8100**，跟猎聘的 8000 错开——两套会跑在同一台机器上，
  一次敲错命令的 `systemctl restart` 就会打到另一套。
"""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI

from app.config import get_settings

logger = logging.getLogger(__name__)

SERVICE_DIR = Path(__file__).resolve().parents[1]


def run_migrations() -> None:
    """启动前把 schema 迁移到 head（幂等；失败即抛错）。"""
    alembic_cfg = Config(str(SERVICE_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(SERVICE_DIR / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(alembic_cfg, "head")


settings = get_settings()
if not (settings.minio_access_key and settings.minio_secret_key):
    logger.warning(
        "MinIO 凭证未配置——附件上传会 502、简历链接会消失。"
        "补 MINIO_ACCESS_KEY / MINIO_SECRET_KEY 后重启。"
    )
run_migrations()

app = FastAPI(title="BOSS 招聘 控制面", version="0.1.0")

# 路由装配在迁移之后（下面这两个 import 挪上来就会在迁移前碰 DB）
from app.api import router as api_router  # noqa: E402
from app.hr_api import router as hr_router  # noqa: E402

app.include_router(api_router)
app.include_router(hr_router)
