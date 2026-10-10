"""candidates 加 resume_ocr_text（2026-10-10 百度 OCR 简历文本）

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-10

PDF 截图模糊，改用百度 OCR 把截图转成文本，存 candidates.resume_ocr_text
（TEXT 可空）。无默认值，存量行保持 NULL。
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE candidates ADD COLUMN resume_ocr_text TEXT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE candidates DROP COLUMN resume_ocr_text")
