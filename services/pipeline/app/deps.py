"""FastAPI 依赖：会话 / 任务队列 / screening 客户端 / 对象存储 / 登录态的单例提供者。

测试经 app.dependency_overrides 换 FakeTaskQueue / FakeScreening / FakeLoginState；
MySQL 与 MinIO 保持真实（任务测试要求：真实 MySQL + 真实 MinIO）。
"""

from functools import lru_cache

from fastapi import Depends, HTTPException
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.login_request import RedisLoginRequest
from app.login_state import RedisLoginState
from app.models import Account
from app.screening_client import ScreeningClient
from app.storage import ObjectStore
from app.task_queue import ArqTaskQueue


def get_session():
    with SessionLocal() as session:
        yield session


def get_active_account(session=Depends(get_session)) -> Account:
    """当前生效的猎聘招聘账号（多账号隔离的锚点）。

    切号由人工在手机上完成后、再在前端点选。这里的「生效账号」是系统做数据
    隔离与配额计算的依据——它不对 App 里的实际登录号做校验（见方案 §8）。

    迁移 0009 会建好「默认账号」并置为 active，activate 端点保证切换后恰好
    一个 active。因此取不到属异常配置，直接 503 让前端醒目提示，而不是猜一个
    账号继续跑——猜错会让配额算到别的号头上。
    """
    acct = (
        session.execute(
            select(Account).where(Account.is_active.is_(True)).order_by(Account.id)
        )
        .scalars()
        .first()
    )
    if acct is None:
        raise HTTPException(
            status_code=503, detail="没有生效的猎聘账号，请先在账号页选择一个"
        )
    return acct


@lru_cache
def get_queue() -> ArqTaskQueue:
    return ArqTaskQueue()


@lru_cache
def get_screening() -> ScreeningClient:
    return ScreeningClient()


@lru_cache
def get_store() -> ObjectStore:
    return ObjectStore(bucket=get_settings().minio_bucket)


@lru_cache
def get_login_state() -> RedisLoginState:
    return RedisLoginState()


@lru_cache
def get_login_request() -> RedisLoginRequest:
    return RedisLoginRequest()
