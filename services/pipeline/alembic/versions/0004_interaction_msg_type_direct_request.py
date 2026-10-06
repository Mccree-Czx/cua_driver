"""interactions.msg_type ENUM 增加 direct_request（2026-10-06 inbound 直索要策略）

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-06

inbound（主动咨询者）直索要话术变体 direct_request 需落 interactions.msg_type；
MySQL 原生 ENUM（D4）新增值需 MODIFY COLUMN。既有行不受影响；downgrade 先
将 direct_request 行回落映射为 greet_request（同为 out 索要型 msg，数据保全）
再收缩枚举，保证 downgrade 可重跑（测试库 downgrade→upgrade 回路）。
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE interactions MODIFY msg_type "
        "ENUM('greet_request','reply','attachment','direct_request') NOT NULL"
    )


def downgrade() -> None:
    # 数据保全：direct_request 回落为 greet_request（同为 out 索要型 msg）后收缩枚举
    op.execute(
        "UPDATE interactions SET msg_type = 'greet_request' WHERE msg_type = 'direct_request'"
    )
    op.execute(
        "ALTER TABLE interactions MODIFY msg_type "
        "ENUM('greet_request','reply','attachment') NOT NULL"
    )
