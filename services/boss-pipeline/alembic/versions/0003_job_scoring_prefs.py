"""jobs 加 scoring_prefs（评分偏好）。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-16

形状照猎聘：`{boosters, veto, requirements, min_stars}`。

★ **这一列现在没有消费者**——BOSS 的打分链路还没做。加它是因为前端的
  「评分偏好」页要编辑它，而且真接上打分时它就是输入，不用再改表。

★ **刻意不设 `server_default`**：猎聘当年给 `jobs.scoring_prefs` 设了
  `DEFAULT ('{}')`，而 MySQL 把它反射成 `(_utf8mb4'{}')`，
  于是 `alembic check` 因为 `compare_server_default=True` 一直报 `modify_default`
  漂移——挂了五天、成了当时唯一的失败测试。写入侧本来就有 Python 侧
  `default=dict`，服务端默认值纯属多余。
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("scoring_prefs", sa.JSON(), nullable=False))


def downgrade() -> None:
    op.drop_column("jobs", "scoring_prefs")
