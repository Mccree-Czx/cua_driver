"""jobs 加 scoring_prefs（2026-10-10 评分模型 0-100 → 1-5 星）

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-10

评分卡偏好（最低主动沟通星级 + 加分点 + 一票否决点 + 其他要求）落到
jobs.scoring_prefs JSON 列。默认 '{}'（空对象），app 侧读不到 min_stars 时
回落默认 3 星。MySQL JSON 列 DEFAULT 须为带括号的表达式。
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN scoring_prefs JSON NOT NULL DEFAULT ('{}')")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN scoring_prefs")
