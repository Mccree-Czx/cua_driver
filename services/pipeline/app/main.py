"""pipeline 入口：配置加载 + 启动前执行迁移（D4）+ FastAPI 装配。

D4：Alembic 只在 pipeline（schema 唯一 owner），应用启动前 `alembic upgrade head`。
端点：管理端点（api）与 HR 面（hr_api）；状态机 T4、存储 T6。
B 方案（2026-10-09）：内部回调端点（/internal/*）已随旧编排层删除——执行由
Pi 内核经 hr-tools 直写。
script_location 显式指到绝对路径：进程 cwd 无关（测试从仓库根启动同理可跑）。
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI

from app.config import get_settings

PIPELINE_DIR = Path(__file__).resolve().parents[1]


def run_migrations() -> None:
    """启动前把 schema 迁移到 head（幂等；失败即抛错，拒绝带旧 schema 启动）。"""
    alembic_cfg = Config(str(PIPELINE_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PIPELINE_DIR / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(alembic_cfg, "head")


settings = get_settings()
run_migrations()

app = FastAPI(title="hr-workbuddy pipeline")

from app.api import router as jobs_router  # noqa: E402  （路由装配在迁移之后）
from app.hr_api import router as hr_router  # noqa: E402

app.include_router(jobs_router)
app.include_router(hr_router)
