"""FastAPI 依赖：会话 / 任务队列 / screening 客户端 / 对象存储 / 登录态的单例提供者。

测试经 app.dependency_overrides 换 FakeTaskQueue / FakeScreening / FakeLoginState；
MySQL 与 MinIO 保持真实（任务测试要求：真实 MySQL + 真实 MinIO）。
"""

from functools import lru_cache

from app.db import SessionLocal
from app.login_state import RedisLoginState
from app.screening_client import ScreeningClient
from app.storage import ObjectStore
from app.task_queue import ArqTaskQueue


def get_session():
    with SessionLocal() as session:
        yield session


@lru_cache
def get_queue() -> ArqTaskQueue:
    return ArqTaskQueue()


@lru_cache
def get_screening() -> ScreeningClient:
    return ScreeningClient()


@lru_cache
def get_store() -> ObjectStore:
    return ObjectStore()


@lru_cache
def get_login_state() -> RedisLoginState:
    return RedisLoginState()
