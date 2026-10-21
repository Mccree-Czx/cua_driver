"""MinIO 薄封装（BOSS 侧）。

复用 197 上同一个 MinIO 实例，但 **bucket 与猎聘分开**（`boss-workbuddy`
vs `hr-workbuddy`）——理由和数据库分开一样：一次敲错名字就会盖掉对方的对象。

只做四件事：确保 bucket、放对象、取对象流、签发预签名 URL。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from functools import lru_cache
from io import BytesIO

from minio import Minio

from app.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def client() -> Minio:
    s = get_settings()
    return Minio(
        s.minio_endpoint,
        access_key=s.minio_access_key,
        secret_key=s.minio_secret_key,
        secure=False,
    )


def ensure_bucket() -> None:
    s = get_settings()
    c = client()
    if not c.bucket_exists(s.minio_bucket):
        c.make_bucket(s.minio_bucket)


def put_object(key: str, data: bytes, content_type: str = "application/pdf") -> str:
    ensure_bucket()
    s = get_settings()
    client().put_object(s.minio_bucket, key, BytesIO(data), len(data),
                        content_type=content_type)
    return key


def open_object(key: str):
    """对象键 → get_object 响应流（调用方负责 close/release_conn）。

    失败原样抛出——用于直出端点（`/api/hr/candidates/{id}/resume`），
    那里要把 NoSuchKey 翻译成 404。预签名 URL 15 分钟过期、且把
    MinIO 凭证的信任边界扩到了浏览器里，直出端点两者都避免。
    """
    s = get_settings()
    return client().get_object(s.minio_bucket, key)


def presigned_get_url(key: str, minutes: int = 15) -> str | None:
    """对象键 → 预签名 URL。失败返回 None（不阻塞详情页）。"""
    if not key:
        return None
    try:
        s = get_settings()
        return client().presigned_get_object(s.minio_bucket, key,
                                             expires=timedelta(minutes=minutes))
    except Exception as exc:  # noqa: BLE001
        logger.warning("presigned_get_url 失败 key=%s：%s: %s",
                       key, exc.__class__.__name__, exc)
        return None
