"""FastAPI 依赖：会话 / 任务队列 / 对象存储 / 登录态的单例提供者。

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
from app.storage import ObjectStore
from app.task_queue import ArqTaskQueue


def get_session():
    with SessionLocal() as session:
        yield session


def get_active_account(session=Depends(get_session)) -> Account:
    """当前生效的猎聘招聘账号（多账号隔离的锚点）。

    换号走 `POST /accounts/{id}/switch`——它**真的会去手机上**退出旧号、登新号，
    成功之后才改这里的 `is_active`。所以「生效账号」与「手机上登的号」应当一致。

    本函数**不做实际核验**（手机挂在 Mac 上，服务端这一步核不了），核验由换号流程
    自己负责。这条「假设两者一致」是整套隔离的地基，所以任何**只改库、不碰手机**
    的路径都不能存在——`POST /accounts/{id}/activate` 就是这么被删掉的（2026-10-15）。

    迁移 0009 会建好「默认账号」并置为 active。因此取不到属异常配置，直接 503
    让前端醒目提示，而不是猜一个账号继续跑——猜错会让配额算到别的号头上。
    """
    acct = (
        session.execute(
            select(Account)
            .where(Account.is_active.is_(True), Account.deleted_at.is_(None))
            .order_by(Account.id)
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


# `get_screening` 已于 2026-10-15 删除：全仓库 0 引用。
# screening 那条路现在由 hr-tools 自己建连（`tools_mcp_server._screening()`），
# 不走 FastAPI 依赖注入——留着一个没人用的 DI 工厂只会让人以为有个接口在用。
# `ScreeningClient` 本身还活着，别一起删。


@lru_cache
def get_store() -> ObjectStore:
    return ObjectStore(bucket=get_settings().minio_bucket)


@lru_cache
def get_login_state() -> RedisLoginState:
    return RedisLoginState()


@lru_cache
def get_login_request() -> RedisLoginRequest:
    return RedisLoginRequest()
