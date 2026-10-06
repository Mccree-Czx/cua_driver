#!/usr/bin/env python
"""备份：MySQL（mysqldump 全库） + MinIO（bucket 全对象）→ backups/ 目录 + 轮转。

用法：
    .venv/bin/python scripts/backup.py [--keep 14]

产物：
    backups/db/hr_workbuddy_YYYYmmdd_HHMMSS.sql.gz
    backups/minio/YYYYmmdd_HHMMSS/<object_key>...
轮转：各保留最近 --keep 份（默认 14），超出删最旧。
建议（上线后）：launchd/cron 每日 21:30 跑（工作窗外）——见 docs/runbook.md §8。
"""

import argparse
import gzip
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "services" / "pipeline"))

BACKUPS = REPO / "backups"
DB_URL = os.environ.get(
    "DATABASE_URL", "mysql+pymysql://hr_user:hr_dev_pw@127.0.0.1:3306/hr_workbuddy"
)
BUCKET = os.environ.get("MINIO_BUCKET", "hr-workbuddy")


def _parse_db_url(url: str) -> tuple[str, str, str, str, str]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return (
        parsed.hostname or "127.0.0.1",
        str(parsed.port or 3306),
        parsed.username or "root",
        parsed.password or "",
        parsed.path.lstrip("/"),
    )


def backup_db() -> Path:
    host, port, user, password, db = _parse_db_url(DB_URL)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = BACKUPS / "db"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{db}_{stamp}.sql.gz"
    cmd = [
        "mysqldump",
        "-h", host,
        "-P", port,
        "-u", user,
        f"-p{password}",
        "--single-transaction",
        "--routines",
        "--no-tablespaces",
        db,
    ]
    with gzip.open(out, "wb") as gz:
        proc = subprocess.run(cmd, stdout=gz, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"mysqldump 失败：{proc.stderr.decode(errors='replace')}")
    return out


def backup_minio() -> Path:
    from minio import Minio

    client = Minio(
        os.environ.get("MINIO_ENDPOINT", "127.0.0.1:9000"),
        access_key=os.environ.get("MINIO_ACCESS_KEY", "minioadmin"),
        secret_key=os.environ.get("MINIO_SECRET_KEY", "minioadmin"),
        secure=False,
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = BACKUPS / "minio" / stamp
    count = 0
    for obj in client.list_objects(BUCKET, recursive=True):
        target = out_dir / obj.object_name
        target.parent.mkdir(parents=True, exist_ok=True)
        client.fget_object(BUCKET, obj.object_name, str(target))
        count += 1
    if count == 0:
        out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def rotate(root: Path, keep: int) -> None:
    if not root.exists():
        return
    entries = sorted(root.iterdir())  # 时间戳命名 → 字典序即时间序
    for old in entries[:-keep] if len(entries) > keep else []:
        if old.is_dir():
            shutil.rmtree(old)
        else:
            old.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", type=int, default=14, help="各保留最近 N 份（默认 14）")
    args = parser.parse_args()

    db_path = backup_db()
    print(f"DB 备份完成：{db_path.relative_to(REPO)}（{db_path.stat().st_size} bytes）")
    minio_dir = backup_minio()
    objs = [p for p in minio_dir.rglob("*") if p.is_file()]
    print(f"MinIO 备份完成：{minio_dir.relative_to(REPO)}（{len(objs)} 个对象）")

    rotate(BACKUPS / "db", args.keep)
    rotate(BACKUPS / "minio", args.keep)
    print(f"轮转完成：各保留最近 {args.keep} 份")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
