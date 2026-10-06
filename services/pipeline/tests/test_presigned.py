"""预签名 URL 与存储原语测试（真实 MinIO 127.0.0.1:9000，不触 MySQL）。

- presigned_get_url 默认 expires=timedelta(minutes=15)（§4：≤15 分钟）：
  生成 URL 含 X-Amz-Expires=900，且窗口内可真实 GET 回 put 进去的字节。
- put_resume 冲突处理：无 ts 键已存在 → 带当前时刻 HHMMSS 重算键再放，返回最终键。
- ensure_bucket：不存在则创建，已存在幂等。

清理：每个测试 finally 里 remove_object 自己 put 的对象（ObjectStore 有意不提供
删除/列表 API——YAGNI，测试直接用 store.client 清理）；ensure_bucket 测试
finally 里 remove_bucket 自建桶。键含 uuid，跨运行无碰撞。
"""

import re
import uuid
from datetime import date

import httpx
import pytest
from minio.error import S3Error

from app.storage import ObjectStore

BUCKET = "hr-workbuddy"  # infra 契约（minio-init 创建）


@pytest.fixture()
def store():
    s = ObjectStore()
    s.ensure_bucket(BUCKET)
    return s


def test_presigned_get_url_15min_and_really_gettable(store):
    key = f"test/presigned/{uuid.uuid4().hex}.pdf"
    data = b"%PDF-1.4 fake resume bytes \x00\xff"
    store.put_object(BUCKET, key, data, content_type="application/pdf")
    try:
        url = store.presigned_get_url(BUCKET, key)
        # 15min = 900s（§4 预签名 ≤15 分钟，URL 参数级断言）
        assert "X-Amz-Expires=900" in url
        resp = httpx.get(url)
        assert resp.status_code == 200
        assert resp.content == data
    finally:
        store.client.remove_object(BUCKET, key)


def test_put_resume_conflict_rekeys_with_hhmmss(store):
    # uuid 保证（job_id, liepin_user_id）全新：首次放必然走无 ts 键
    job_id = 900_000 + uuid.uuid4().int % 90_000
    liepin_user_id = f"LP{uuid.uuid4().hex[:8]}"
    data = b"%PDF-1.4 fake resume"

    key1 = store.put_resume(job_id, liepin_user_id, "张伟", "产品经理", date(2026, 10, 5), data)
    key2 = store.put_resume(job_id, liepin_user_id, "张伟", "产品经理", date(2026, 10, 5), data)
    try:
        assert key1 == f"resumes/{job_id}/{liepin_user_id}/张伟_产品经理_20261005.pdf"
        assert re.fullmatch(
            re.escape(f"resumes/{job_id}/{liepin_user_id}/张伟_产品经理_20261005_") + r"\d{6}\.pdf",
            key2,
        )
        assert store.stat_object(BUCKET, key1)
        assert store.stat_object(BUCKET, key2)
    finally:
        store.client.remove_object(BUCKET, key1)
        store.client.remove_object(BUCKET, key2)


def test_stat_object_missing_returns_false(store):
    assert store.stat_object(BUCKET, f"test/absent/{uuid.uuid4().hex}") is False


def test_stat_object_reraises_non_missing_s3_errors(store, monkeypatch):
    """非「键不存在」的 S3 响应错误必须重抛，不得报告为「对象不存在」。

    否则 put_resume 会误判无冲突、用无时间戳的规范键静默覆盖已存在简历
    （评审 Important 修复点）。
    """

    def _boom(bucket, key):
        raise S3Error(
            None,
            "InternalError",
            "server exploded",
            f"/{bucket}/{key}",
            "req-1",
            "host-1",
        )

    monkeypatch.setattr(store.client, "stat_object", _boom)
    with pytest.raises(S3Error) as exc_info:
        store.stat_object(BUCKET, f"test/absent/{uuid.uuid4().hex}")
    assert exc_info.value.code == "InternalError"


def test_ensure_bucket_creates_and_idempotent(store):
    bucket = f"test-bucket-{uuid.uuid4().hex[:8]}"
    try:
        store.ensure_bucket(bucket)
        assert store.client.bucket_exists(bucket)
        store.ensure_bucket(bucket)  # 幂等：已存在不报错
    finally:
        store.client.remove_bucket(bucket)
