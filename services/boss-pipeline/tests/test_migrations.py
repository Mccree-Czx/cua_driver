"""迁移守卫。

跟猎聘那边同一个套路（`services/pipeline/tests/test_migrations.py`）：
**模型与迁移必须逐列一致**，靠 `alembic check` 钉住。

★ 这条测试的价值在于——`alembic check` 报的漂移**看起来永远像小问题**
  （「就一个默认值不一样嘛」），但它意味着「从迁移建出来的库」和「模型以为的库」
  是两份东西。猎聘当年在 `jobs.scoring_prefs` 上栽过：一个 server_default
  漂了五天，一直挂着唯一的失败测试。

★ 这里跑在 **SQLite** 上（本机没有 MySQL 也能验）。
  MySQL 上的 `onupdate` 漂移是另一回事，见 `alembic/env.py` 顶部的说明——
  第一次部署到 MySQL 时要单独核一遍。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1]

EXPECTED_TABLES = {
    "jobs", "candidates", "conversations", "messages", "greet_log",
    "job_candidate", "interactions", "review_overrides", "task_logs",
}


def _alembic(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    db = tmp_path / "mig.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite+pysqlite:///{db}"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=SERVICE_DIR, env=env, capture_output=True, text=True, timeout=180,
    )


@pytest.fixture()
def migrated(tmp_path):
    r = _alembic(tmp_path, "upgrade", "head")
    assert r.returncode == 0, f"upgrade head 失败：\n{r.stdout}\n{r.stderr}"
    return tmp_path


def test_upgrade_creates_all_tables(migrated):
    import sqlite3

    con = sqlite3.connect(str(migrated / "mig.db"))
    names = {n for (n,) in con.execute("select name from sqlite_master where type='table'")}
    con.close()
    missing = EXPECTED_TABLES - names
    assert not missing, f"迁移没建出这些表：{sorted(missing)}"


def test_no_drift_between_models_and_migrations(migrated):
    """★ 核心断言：`alembic check` 必须干净。"""
    r = _alembic(migrated, "check")
    assert r.returncode == 0, (
        "模型与迁移漂移了——从迁移建出来的库和模型以为的库是两份东西。\n"
        f"{r.stdout}\n{r.stderr}"
    )


def test_downgrade_to_base_is_clean(migrated):
    """能一路退回去（证明 downgrade 没漏写）。"""
    assert _alembic(migrated, "downgrade", "base").returncode == 0
    import sqlite3

    con = sqlite3.connect(str(migrated / "mig.db"))
    names = {n for (n,) in con.execute("select name from sqlite_master where type='table'")}
    con.close()
    assert not (EXPECTED_TABLES & names), f"downgrade 之后还剩：{EXPECTED_TABLES & names}"
