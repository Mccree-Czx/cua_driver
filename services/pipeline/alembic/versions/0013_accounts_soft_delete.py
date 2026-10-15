"""账号软删除：accounts 加 deleted_at（2026-10-15）

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-15

## 为什么是软删除

用户要在账号管理里能删账号。但**指向 accounts 的四个外键都没有 ondelete**
（`jobs` / `candidates` / `job_candidate` / `task_logs`，MySQL 默认 RESTRICT），
所以有数据的账号直接删会报外键冲突；而账号名下的数据量差别极大——
id=1 有 1885 候选人 + 1407 条 jc + 两个带真 JD 的职位。

**一键下去会毁掉历史的按钮不该存在**，所以改成软删除：列表里藏起来、数据一条不动、
需要时能恢复。

## 代价（已知并接受）

`uq_accounts_phone` 是普通唯一约束，而 MySQL 不支持部分索引——软删的账号
**仍然占着它的手机号**。之后用同一个号新建账号会被拒。新建接口撞上唯一约束时会
去查是不是一个已删除的账号，把错误说清楚（「请先恢复它」），而不是让用户对着
「该手机号已登记过账号」发呆——他明明在列表里看不到那个号。

## 可空、无默认值

存量账号全部当「未删除」（NULL）。不加默认值也就不引入 server_default，
`alembic check` 不会因此报漂移（scoring_prefs 那个坑的教训）。
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE accounts ADD COLUMN deleted_at DATETIME NULL")


def downgrade() -> None:
    # 回滚会**丢失「哪些账号被软删过」这一信息**——它们会重新出现在列表里。
    # 数据本身（候选人/职位）不受影响，所以这是可接受的；但要知道会这样。
    op.execute("ALTER TABLE accounts DROP COLUMN deleted_at")
