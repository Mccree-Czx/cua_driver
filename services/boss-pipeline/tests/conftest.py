"""测试夹具。

★ 用 **SQLite 内存库**，不是生产用的 MySQL。
  模型里刻意只用了 SQLite 也支持的列类型（String/Text/JSON/Boolean/Integer/
  DateTime/ForeignKey/UniqueConstraint），所以这一层能跑。
  真正需要 MySQL 才验得出来的东西（时区、ENUM、迁移）留给 `alembic upgrade` 的冒烟。
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# ★ 本服务**没有装进 venv**（跟猎聘的 pipeline 一样，`uv pip list` 里只有 contracts）。
#   所以得自己把服务目录塞进 sys.path，否则 `import app` 在 pytest 下找不到。
#   猎聘的 tests/conftest.py 也是这么干的（`sys.path.insert(0, PIPELINE_DIR)`）。
SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))

# ★★ **必须在导入任何 app.\* 之前设好 DATABASE_URL。**
#
#   `app.main` 在**导入期**就跑 `alembic upgrade head`（照猎聘的 D4：
#   schema 唯一 owner，应用启动前迁移）。所以只要 import 了它，就会去连
#   `DATABASE_URL` 指向的库并把它升到 head——测试里绝不能连到真的库上。
#
#   2026-10-16 踩到：conftest 原来在 import 之后才设，于是它去连了本机那个
#   真 MySQL（`boss_user@172.18.0.1`）。那次**被拒了**（账号不存在），
#   什么也没改；但要是账号恰好存在，就会把生产库升上去。
#
#   猎聘的 conftest 里有同一条注释，同一个道理。
os.environ.setdefault(
    "DATABASE_URL",
    f"sqlite+pysqlite:///{Path(tempfile.mkdtemp()) / 'conftest_boot.db'}",
)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

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
