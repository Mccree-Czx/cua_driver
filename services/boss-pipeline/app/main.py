"""boss-pipeline 应用入口。

启动：
    cd services/boss-pipeline && uv run uvicorn app.main:app --port 8100

★ 端口用 **8100**，跟猎聘的 8000 错开——两套同机部署，
  一次敲错命令的 `systemctl restart` 就会打到另一套。

schema 由 Alembic 管理，应用不 create_all（跟猎聘同一条规矩）。
"""

from fastapi import FastAPI

from app.api import router

app = FastAPI(title="BOSS 招聘 控制面", version="0.1.0")
app.include_router(router)
