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
        text=True,
        timeout=180,
    )
