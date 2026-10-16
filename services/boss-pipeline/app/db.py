"""数据库引擎与会话。

schema 由 Alembic 管理，应用不 create_all（跟猎聘同一条规矩）。
测试用 `DATABASE_URL` 指向独立的测试库。
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_url = get_settings().database_url

# MySQL server 时区为 UTC，需在连接层固定 +08:00，使 CURRENT_TIMESTAMP/NOW()
# 与 Python datetime.now() 一致（北京时间），否则两种时区的时间戳会混在一张表里。
# ★ 这条是照猎聘抄的——同一台机器上两套系统的时间戳必须能直接对比。
#
# ★★ `init_command` 是 **PyMySQL 专有**的参数：无脑传给别的驱动会直接
#    `TypeError: 'init_command' is an invalid keyword argument`。
#    2026-10-16 就是这么把「用 SQLite 起个后端跑冒烟」卡住的——
#    而**能在没装 MySQL 的地方把服务拉起来**，对排查问题很值钱。
_connect_args: dict[str, object] = (
    {"init_command": "SET time_zone = '+08:00'"} if _url.startswith("mysql") else {}
)

engine = create_engine(_url, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
