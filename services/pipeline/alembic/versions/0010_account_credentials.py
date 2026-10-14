"""账号凭据：accounts 加 phone / password_enc / credential_updated_at（2026-10-14）

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-14

背景：0009 只做了「按账号隔离数据」，账号本身仍只有一个显示名——系统并不
知道手机上登录的是哪个号，切错号也发现不了。手机号是账号的真实身份，是
系统核对「手机上的号 == 前端的号」的依据。

三列全部可空，且默认账号（id=1）保持 NULL：1845 条存量候选人挂在它名下，
不能为了加列去动它。MySQL 的唯一索引允许多个 NULL，所以 uq_accounts_phone
不会与存量 NULL 冲突。

password_enc 存 Fernet 密文（app/crypto.py），明文永不落库。主密钥
ACCOUNT_SECRET_KEY 只在 .env——延续「密钥只放 .env，绝不入库」的既有约定。
密钥丢失则已存密码不可恢复（账号登录本身不受影响，重填即可）。

downgrade 会丢弃所有已存密码（列被删），这是不可逆的——回滚前先备份。
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE accounts ADD COLUMN phone VARCHAR(32) NULL")
    op.execute("ALTER TABLE accounts ADD COLUMN password_enc VARCHAR(512) NULL")
    op.execute("ALTER TABLE accounts ADD COLUMN credential_updated_at DATETIME NULL")
    # 一个手机号 = 一个猎聘账号。约束名与 models.Account.__table_args__ 一致，
    # 否则 alembic check 会报漂移。
    op.execute("ALTER TABLE accounts ADD CONSTRAINT uq_accounts_phone UNIQUE (phone)")


def downgrade() -> None:
    op.execute("ALTER TABLE accounts DROP INDEX uq_accounts_phone")
    op.execute("ALTER TABLE accounts DROP COLUMN credential_updated_at")
    op.execute("ALTER TABLE accounts DROP COLUMN password_enc")
    op.execute("ALTER TABLE accounts DROP COLUMN phone")
