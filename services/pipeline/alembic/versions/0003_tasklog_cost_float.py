"""task_logs：cost Integer→Float（微元精度不截断）+ note 注记列

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06

cost_est 为浮点元（保留 6 位微元精度），Integer 列截断小数 → MODIFY FLOAT；
同迁移附带 note 列（String(255) 可空）：真实模式门禁 ② 的迟到简历附件
「只存不推进」落 TaskLog 需注记字段（TaskLog 原无自由文本列），与 cost
共用 0003 槽位（门禁 ② 与账目 ③ 同批上线）。既有 dev 库直接 upgrade head
平滑应用。
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE task_logs MODIFY cost FLOAT NOT NULL DEFAULT 0")
    op.add_column(
        "task_logs", sa.Column("note", sa.String(length=255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("task_logs", "note")
    op.execute("ALTER TABLE task_logs MODIFY cost INT NOT NULL DEFAULT 0")
