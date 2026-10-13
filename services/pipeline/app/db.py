"""数据库引擎与会话。

pipeline 是 MySQL 唯一写入口（D1）；schema 由 Alembic 管理（D4），
应用不 create_all。测试库经 DATABASE_URL 环境变量指向 hr_workbuddy_test。
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


# MySQL server 时区为 UTC，需在连接层固定 +08:00，使 CURRENT_TIMESTAMP/NOW()
# 与 Python datetime.now() 一致（北京时间），否则两种时区的时间戳会混在一张表里。
engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,
    connect_args={"init_command": "SET time_zone = '+08:00'"},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
