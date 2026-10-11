"""candidates：删 resume_ocr_text，加 resume_html_key（2026-10-10）

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-10

OCR 方案废弃，改用 flash 视觉 → 结构化 JSON → HTML 简历文件存 MinIO。
resume_ocr_text 列删除（连带历史 OCR 数据），新增 resume_html_key（存
MinIO 简历网页键）。
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE candidates DROP COLUMN resume_ocr_text")
    op.execute("ALTER TABLE candidates ADD COLUMN resume_html_key VARCHAR(512) NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE candidates DROP COLUMN resume_html_key")
    op.execute("ALTER TABLE candidates ADD COLUMN resume_ocr_text TEXT NULL")
