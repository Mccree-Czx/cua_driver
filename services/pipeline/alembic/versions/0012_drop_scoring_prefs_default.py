"""去掉 jobs.scoring_prefs 的 server default（2026-10-15）

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-15

## 这条迁移在修什么

`alembic check` 从 2026-10-10 起一直报一处漂移：

    modify_default  jobs.scoring_prefs

**它是那个唯一的失败测试**（`tests/test_migrations.py::test_alembic_check_reports_no_drift`），
也就是说自 10-10 起，每次跑测试都有一条红的——红久了就没人看了，
而「测试全绿」是后面每一批改动的安全网。

**根因**：迁移 0005 给这一列设了 MySQL 的表达式默认值 `DEFAULT ('{}')`，
而 `alembic/env.py` 开了 `compare_server_default=True`，
model 侧（`app/models.py`）没有对应的 `server_default` → 判定为漂移。

## 为什么是「删默认值」而不是「把默认值写进 model」

MySQL 把 `DEFAULT ('{}')` 规范化成 `(_utf8mb4'{}')`——带字符集的内部表示。
把它抄进 model 等于**把某个 MySQL 版本的实现细节焊进代码**，换个版本或字符集就又漂。

而删掉它是安全的：写入侧本来就有 Python 侧的 `default=dict`，
服务端默认值纯属多余。删掉之后这一列与 `hard_rules` / `template_msgs`
**三个 JSON 列的写法就一致了**。

## 顺带

**不用改迁移 0005**：即使新库按 0005 建出带默认值的列，跑到本迁移也会被删掉，
净结果一致。改已上线的迁移反而对存量库无效——这正是当初要避免的。
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ALTER COLUMN scoring_prefs DROP DEFAULT")


def downgrade() -> None:
    """**刻意什么都不做**，理由不是偷懒。

    这个默认值在 0005 里是用 `ADD COLUMN ... DEFAULT ('{}')` 建的——那条语句能过，
    但 MySQL **不允许**用 `ALTER COLUMN ... SET DEFAULT` 给 JSON 列补默认值：

        1101: BLOB, TEXT, GEOMETRY or JSON column 'scoring_prefs' can't have a default value

    要真恢复它，只能删列重建，而那一列装着评分卡配置——**为一个多余的默认值丢数据
    不值得**。

    丢的也不是行为：写入侧有 Python 侧的 `default=dict`，insert 永远会给出 `{}`。
    所以回滚不回这个默认值，不会有任何可观察的差异。
    """
    return
