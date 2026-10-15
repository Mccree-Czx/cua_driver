"""jobs 加扫描字段：location / education / experience / salary（2026-10-15）

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-15

背景：切到新账号后 `Job` 表里该账号名下 0 个职位 → `job_list_active()` 返回空 →
`job_quota_status()` 返回 `{"quota_each":0,"jobs":[]}` → Pi 无事可做 →
`inbound=0 outbound=0` → 触发「空未读退避」把后续 30 分钟的 main_round 全挡掉。

根因是**全库没有任何代码会去读 App 的职位列表**——职位只能靠 `job_upsert` 手工写。
于是要让「切号后自动扫职位」成立，先把扫得到的东西有地方放。

猎聘职位卡 dump 出来的原文形如：

    机械结构工程师
    上海-黄浦区 本科 5年以上 25-45k

标题已有列，其余四项此前无处可放，现在拆成四列。

四列**全部可空**：存量两个职位（id=1/3）是手工建的，没有这些值；新扫到的职位
也允许某项缺失（卡片格式变了不该让整次扫描失败）。

不加唯一约束——`jobs` 表本来就没有，而且「同一账号同名职位算同一个」这条去重
逻辑在 `app/job_sync.py` 里，靠 `(account_id, title)` 查询实现，不靠数据库约束
（加了约束反而会让 `alembic check` 因为命名对不上而报漂移，`scoring_prefs` 前车之鉴）。
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN location VARCHAR(128) NULL")
    op.execute("ALTER TABLE jobs ADD COLUMN education VARCHAR(64) NULL")
    op.execute("ALTER TABLE jobs ADD COLUMN experience VARCHAR(64) NULL")
    op.execute("ALTER TABLE jobs ADD COLUMN salary VARCHAR(64) NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN salary")
    op.execute("ALTER TABLE jobs DROP COLUMN experience")
    op.execute("ALTER TABLE jobs DROP COLUMN education")
    op.execute("ALTER TABLE jobs DROP COLUMN location")
