"""job_candidate 加 minio_object_key（附件简历的 MinIO 对象键）。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-19

收简历链路（P1）落地后，收到的附件简历存到 MinIO（bucket `boss-workbuddy`），
这一列记对象键。空 = 还没收到简历。可空——绝大多数 jc 本来就没有简历。
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_candidate",
        sa.Column("minio_object_key", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("job_candidate", "minio_object_key")
