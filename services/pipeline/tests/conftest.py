"""pipeline 测试 conftest。

关键顺序约束（D4）：DATABASE_URL 必须在导入任何 app.* 之前指向测试库
hr_workbuddy_test——SQLAlchemy engine 与 pydantic-settings 都在导入期固化配置。
alembic 子进程则经由 helpers.run_alembic 携带同一 URL。
"""

import os
import sys

import pytest

from helpers import PIPELINE_DIR, TEST_DATABASE_URL, run_alembic

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
sys.path.insert(0, str(PIPELINE_DIR))  # 使 `import app` 在任意 cwd 下可用

from app import models  # noqa: E402, F401  注册全部表到 Base.metadata
from app.db import SessionLocal, engine  # noqa: E402


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
