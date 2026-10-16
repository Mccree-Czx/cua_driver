"""Alembic 环境：URL 来自 boss-pipeline Settings（DATABASE_URL），metadata 来自 app.models。

跟猎聘的 `services/pipeline/alembic/env.py` 同构。

★ **一处刻意的不同**：这里**没有**开 `compare_server_onupdate=True`。
  猎聘的模型里写了 `server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")`
  ——那是 MySQL 专有语法，代价是它**只能跑在真 MySQL 上**。
  BOSS 的模型保持 SQLite 兼容（本地要能起后端跑冒烟，这个能力很值钱），
  所以时间戳的更新交给 SQLAlchemy 的 `onupdate`（ORM 侧），不写进 DDL。

  ⚠️ 后果：**MySQL 上的 `alembic check` 可能会报 `onupdate` 漂移**。
     第一次部署到 MySQL 时要核一遍，别当成代码问题。
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import app.models  # noqa: F401  注册全部表到 Base.metadata
from app.config import get_settings
from app.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
