"""测试辅助：测试库 URL + alembic 子进程运行器（conftest 与 test_migrations 共用）。"""

import os
import subprocess
import sys
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parents[1]

# 测试库与主库同构、同凭据，全部通过 Alembic 迁移建表（D4：不 create_all）。
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "mysql+pymysql://hr_user:hr_dev_pw@127.0.0.1:3306/hr_workbuddy_test",
)

# ★ 2026-10-16 补：光钉住 DATABASE_URL **不够**——`REDIS_URL` 与 `MINIO_ENDPOINT`
#   是**跟着 .env 走**的。而云机执行端（10.99.40.66）的 `.env` 指生产 197，
#   那台机器上也会跑 pytest：不钉住就会写**生产 Redis 的风控键与配额桶**
#   （里面还有 BOSS 在用的 `baidu-ocr-token`），还会往生产 MinIO 传对象。
#   与 TEST_DATABASE_URL 同理，必须在导入 `app.*` 之前钉死。
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
TEST_MINIO_ENDPOINT = os.environ.get("TEST_MINIO_ENDPOINT", "127.0.0.1:9000")


def run_alembic(*args: str) -> subprocess.CompletedProcess:
    """在 pipeline 目录下以子进程运行 alembic。

    子进程 env 携带测试库 DATABASE_URL；独立进程意味着 env.py 里
    get_settings() 无缓存干扰，与真实 `alembic upgrade head` 完全同路径。
    """
    env = dict(os.environ)
    env["DATABASE_URL"] = TEST_DATABASE_URL
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PIPELINE_DIR,
        env=env,
        capture_output=True,
        # 必须显式 utf-8：迁移消息里有中文，Windows 默认按 GBK 解码会让读取线程
        # 抛 UnicodeDecodeError，stdout 拿不全 → 断言随机失手。
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
