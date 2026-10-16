"""测试夹具。

★ 用 **SQLite 内存库**，不是生产用的 MySQL。
  模型里刻意只用了 SQLite 也支持的列类型（String/Text/JSON/Boolean/Integer/
  DateTime/ForeignKey/UniqueConstraint），所以这一层能跑。
  真正需要 MySQL 才验得出来的东西（时区、ENUM、迁移）留给 `alembic upgrade` 的冒烟。
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ★ 本服务**没有装进 venv**（跟猎聘的 pipeline 一样，`uv pip list` 里只有 contracts）。
#   所以得自己把服务目录塞进 sys.path，否则 `import app` 在 pytest 下找不到。
#   猎聘的 tests/conftest.py 也是这么干的（`sys.path.insert(0, PIPELINE_DIR)`）。
SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))

from app import models  # noqa: E402, F401  让 Base.metadata 拿到全部表
from app.db import Base
from app.deps import get_session
from app.main import app


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # 内存库：不共用同一条连接的话每个连接各看一个空库
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def client(session):
    """★ 与 `session` 共用同一条会话，这样测试里塞的数据 API 立刻看得见。"""
    app.dependency_overrides[get_session] = lambda: session
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
