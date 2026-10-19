"""job_candidate 加 match_score（screening 服务的匹配分）。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-19

评分链路（P2）落地后，screening 服务给的 1-5 星匹配分写在这里。可空——
没评过分的 jc 是 None（评分在打招呼前按需跑，存量行先空着）。
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_candidate",
        sa.Column("match_score", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("job_candidate", "match_score")
