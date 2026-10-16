"""FastAPI 依赖。"""

from collections.abc import Iterator

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def require_ingest_token(token: str | None) -> None:
    """采数端的鉴权。

    ★ 未配置 `INGEST_TOKEN` 时**放行**（仅限本机开发）——这是刻意的：
      否则本地起服务跑测试要先造一个假令牌。
      生产必须配，且配了之后就一定校验。
    """
    expected = get_settings().ingest_token
    if not expected:
        return
    if token != expected:
        raise PermissionError("ingest token 不对")
