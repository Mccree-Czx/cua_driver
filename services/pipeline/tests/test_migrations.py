"""迁移测试（D4：测试库同样走迁移，不 create_all）。

- `alembic upgrade head` 从零干净成功：downgrade base 清空后升级，6 张业务表齐全
- `alembic check` 无漂移：数据库 schema 与 app.models 元数据完全一致
"""

from sqlalchemy import text

from app.db import engine
from helpers import run_alembic

EXPECTED_TABLES = {
    "jobs",
    "candidates",
    "job_candidate",
    "interactions",
    "review_overrides",
    "task_logs",
    "alembic_version",
}


def _table_names() -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE()"
            )
        ).scalars()
    return set(rows)


def test_upgrade_head_from_scratch_is_clean():
    """downgrade base 清空 → upgrade head：返回 0，6 张业务表全部存在。"""
    down = run_alembic("downgrade", "base")
    assert down.returncode == 0, f"downgrade base 失败\nstdout:\n{down.stdout}\nstderr:\n{down.stderr}"
    remaining = _table_names() & EXPECTED_TABLES
    assert remaining == {"alembic_version"}, f"downgrade 后仍有表残留: {remaining}"

    up = run_alembic("upgrade", "head")
    assert up.returncode == 0, f"upgrade head 失败\nstdout:\n{up.stdout}\nstderr:\n{up.stderr}"
    assert EXPECTED_TABLES <= _table_names()


def test_alembic_check_reports_no_drift():
    """迁移与模型一致（schema 唯一 owner 的守卫）：alembic check 返回 0 且无待生成操作。"""
    result = run_alembic("check")
    assert result.returncode == 0, (
        f"alembic check 检测到漂移\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "New upgrade operations detected" not in result.stdout
