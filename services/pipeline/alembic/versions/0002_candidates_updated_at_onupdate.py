"""candidates.updated_at 补 ON UPDATE CURRENT_TIMESTAMP（T3 漂移守卫补全后暴露）

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-05

模型要求 updated_at 随 UPDATE 自动刷新（onupdate=func.now()），0001 迁移只给了
server_default、缺 ON UPDATE 子句——env.py 开启 compare_server_default=True 后
alembic check 暴露该漂移（MySQL 反射把 ON UPDATE 捆进 server default 文本比较）。
本迁移用 MySQL 8 语法补齐，既有 dev 库直接 upgrade head 平滑应用。
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE candidates MODIFY updated_at DATETIME NOT NULL "
        "DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE candidates MODIFY updated_at DATETIME NOT NULL "
        "DEFAULT CURRENT_TIMESTAMP"
    )
