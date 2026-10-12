"""candidates：加 attachment_keys（2026-10-12 附件误判修复）

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-12

问题：Pi 对「只有在线简历、无附件」的候选人，误把在线简历当附件，调用
state_transition(receive_resume) 把候选人标成 resume_received。根因是当时
无法区分「附件归档」（capture_attachment_pages）与「在线简历截图」
（snapshot_put）——两者都只写 candidate.snapshot_object_key。

新增 attachment_keys（JSON 列表，存附件分页截图 MinIO 键），仅由
capture_attachment_pages 写入；state_transition(receive_resume) 据此守卫：
无附件证据一律拒绝，迫使走索要简历路径。NULL 表示尚无附件证据。
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE candidates ADD COLUMN attachment_keys JSON NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE candidates DROP COLUMN attachment_keys")
