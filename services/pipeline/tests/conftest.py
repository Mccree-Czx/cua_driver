"""pipeline 测试 conftest。

关键顺序约束（D4）：数据源必须在导入任何 app.* 之前指向**测试用**的实例
——SQLAlchemy engine 与 pydantic-settings 都在导入期固化配置。
alembic 子进程则经由 helpers.run_alembic 携带同一个 URL。

★ 2026-10-16：从「只钉 DATABASE_URL」扩成「钉住全部数据源 + 拒绝打非本地」。
  起因：云机执行端（10.99.40.66）也跑这套代码，而它的 `.env` 指**生产 197**。
  `REDIS_URL` / `MINIO_ENDPOINT` 从前是跟着 `.env` 走的，所以在那种机器上跑一次
  pytest 就会写**生产 Redis 的风控键与配额桶**、往生产 MinIO 传对象——而且不报错。
"""

import os
import sys

import pytest

from helpers import (
    PIPELINE_DIR,
    TEST_DATABASE_URL,
    TEST_MINIO_ENDPOINT,
    TEST_REDIS_URL,
    run_alembic,
)

# 迁移 0009 会建好 id=1 的默认账号；测试统一用它作为归属
DEFAULT_ACCOUNT_ID = 1

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["REDIS_URL"] = TEST_REDIS_URL
os.environ["MINIO_ENDPOINT"] = TEST_MINIO_ENDPOINT
# 账号密码加密主密钥：测试用固定值。与 DATABASE_URL 同理，必须在导入 app.* 之前
# 固化——get_settings 有 lru_cache，导入后再设就晚了。真实密钥只放 .env，绝不入库。
os.environ.setdefault(
    "ACCOUNT_SECRET_KEY", "H3D-OE4iGm6D4ctIG8TkNbq_oI1GtcQYSN_3064cARM="
)
sys.path.insert(0, str(PIPELINE_DIR))  # 使 `import app` 在任意 cwd 下可用

from app import models  # noqa: E402, F401  注册全部表到 Base.metadata
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402

#: host 是这些之一就算「本地」。空串 = 这个设置压根没有 host 可言。
_LOCAL_HOSTS = {"", "127.0.0.1", "localhost", "::1"}


def _host_of(value: str) -> str:
    """从 `scheme://[user:pass@]host[:port]/...` 或 `host:port` 里抠出 host。"""
    rest = value.split("://", 1)[1] if "://" in value else value
    authority = rest.split("@")[-1].split("/", 1)[0]
    return authority.rsplit(":", 1)[0].strip("[]") if ":" in authority else authority


def _refuse_if_datasources_are_remote() -> None:
    """★ 护栏：测试只许打本地数据底座。

    ★ 为什么光钉住那三个变量还不够：将来**新增**一个带 host 的设置又会漏，
      而漏了的代价是「悄悄写生产且不报错」。所以这里在开跑前把解析出来的设置
      **统一校验一遍**——不靠人记得，漏了就停。
    ★ 应用层 lru_cache 已固化，所以这必须在 `app.*` 导入之后、任何测试之前跑（即此处）。
    """
    if os.environ.get("HR_ALLOW_NONLOCAL_TESTS") == "1":
        return
    settings = get_settings()
    offenders = [
        f"{name} → {_host_of(getattr(settings, name, '') or '')}"
        for name in ("database_url", "redis_url", "minio_endpoint", "screening_url")
        if _host_of(getattr(settings, name, "") or "") not in _LOCAL_HOSTS
    ]
    if offenders:
        pytest.exit(
            "测试拒绝跑：这些数据源指向非本地 —— "
            + "；".join(offenders)
            + "。测试会真的写它们（Redis 风控键/配额桶、MinIO 对象），默认只许打本地。"
            "要么起本地数据底座（docker compose -f infra/docker-compose.yml "
            "--profile docker-mysql up -d），要么用 TEST_DATABASE_URL / TEST_REDIS_URL / "
            "TEST_MINIO_ENDPOINT 指向测试实例；确实要打远端就显式设 "
            "HR_ALLOW_NONLOCAL_TESTS=1。",
            returncode=4,
        )


_refuse_if_datasources_are_remote()


@pytest.fixture(scope="session", autouse=True)
def migrated_schema():
    """整场测试前把测试库迁移到 head（幂等），test_models 不依赖文件执行顺序。"""
    result = run_alembic("upgrade", "head")
    assert result.returncode == 0, (
        f"alembic upgrade head 失败\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    yield
    engine.dispose()


@pytest.fixture()
def session():
    """函数级会话：测试内可 commit/flush，结束后回滚残留变更。"""
    with SessionLocal() as s:
        yield s
        s.rollback()
