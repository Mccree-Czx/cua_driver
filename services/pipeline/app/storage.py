"""MinIO 对象存储（spec v1.6 §4）：规范对象键 + 上传/存在性/预签名原语。

对象键规范（§4，binding）：
- PDF 简历：resumes/{job_id}/{liepin_user_id}/{姓名}_{岗位}_{YYYYMMDD}[_{HHMMSS}].pdf
  重名（同键已存在）才加时间戳；姓名/岗位原样拼入，日期 YYYYMMDD。
- 在线简历截图：snapshots/{liepin_user_id}/{YYYYMMDD}_{HHMMSS}.png
  同一人重复截图保留历史，恒带时间戳。

预签名 URL ≤15 分钟（决策 6）：presigned_get_object
expires=timedelta(minutes=15)（SDK 生成 X-Amz-Expires=900）。

YAGNI 边界：不提供删除/列表/多版本 API；上传/下载业务调用方在 T7。
"""

import io
from datetime import date, datetime, timedelta

from minio import Minio
from minio.error import S3Error

from app.config import get_settings

DEFAULT_BUCKET = "hr-workbuddy"  # infra 契约（docker-compose minio-init 创建）


def resume_object_key(
    job_id: int,
    liepin_user_id: str,
    name: str,
    job_title: str,
    date: date,
    ts: str | None = None,
) -> str:
    """PDF 简历键。无冲突时不含 _HHMMSS；ts 传非 None（HHMMSS）时带时间戳。"""
    key = f"resumes/{job_id}/{liepin_user_id}/{name}_{job_title}_{date.strftime('%Y%m%d')}"
    if ts is not None:
        key += f"_{ts}"
    return f"{key}.pdf"


def snapshot_object_key(liepin_user_id: str, date: date, ts: str) -> str:
    """在线简历截图键。恒带 _HHMMSS（同一人重复截图保留历史）。"""
    return f"snapshots/{liepin_user_id}/{date.strftime('%Y%m%d')}_{ts}.png"


class ObjectStore:
    """MinIO 薄封装：构造读 app.config MINIO_* 配置建 SDK client。"""

    def __init__(self, bucket: str = DEFAULT_BUCKET) -> None:
        settings = get_settings()
        self.client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=False,
        )
        self.bucket = bucket

    def ensure_bucket(self, bucket: str | None = None) -> None:
        """桶不存在则创建；已存在幂等。默认 self.bucket。"""
        target = bucket or self.bucket
        if not self.client.bucket_exists(target):
            self.client.make_bucket(target)

    def put_object(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(
            bucket, key, io.BytesIO(data), length=len(data), content_type=content_type
        )

    def presigned_get_url(
        self, bucket: str, key: str, expires: timedelta = timedelta(minutes=15)
    ) -> str:
        """预签名 GET URL。默认 15 分钟（§4 上限）。"""
        return self.client.presigned_get_object(bucket, key, expires=expires)

    def stat_object(self, bucket: str, key: str) -> bool:
        """对象存在性检查（put_resume 冲突检测用）。

        只有「键不存在」（NoSuchKey/NoSuchObject）才判 False；其余 S3 响应
        错误（服务端 500、MinIO 瞬时抖动等）一律重抛——若吞掉会被 put_resume
        误判为无冲突，静默覆盖已存在的简历。网络层错误（urllib3）不经 S3Error，
        本就正常传播。
        """
        try:
            self.client.stat_object(bucket, key)
            return True
        except S3Error as e:
            if e.code in ("NoSuchKey", "NoSuchObject"):
                return False
            raise

    def put_resume(
        self,
        job_id: int,
        liepin_user_id: str,
        name: str,
        job_title: str,
        date: date,
        data: bytes,
    ) -> str:
        """上传 PDF 简历，返回最终键。

        先用无 ts 键做存在性检查；已存在（重名冲突）则带当前时刻 HHMMSS
        重算键再放（§4：重名加时间戳）。
        """
        key = resume_object_key(job_id, liepin_user_id, name, job_title, date)
        if self.stat_object(self.bucket, key):
            ts = datetime.now().strftime("%H%M%S")
            key = resume_object_key(job_id, liepin_user_id, name, job_title, date, ts)
        self.put_object(self.bucket, key, data, content_type="application/pdf")
        return key
