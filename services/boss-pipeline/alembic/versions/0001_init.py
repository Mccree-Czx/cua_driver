"""init: BOSS 的第一批表（职位 / 候选人 / 会话 / 消息 / 招呼账本）

Revision ID: 0001
Revises:
Create Date: 2026-10-16

手写迁移，与 `app/models.py` 逐列一致（`alembic check` 守卫）。

★ 索引名遵循 SQLAlchemy 的默认：`ix_<表>_<列>`。`unique=True, index=True` 建的是
  **唯一索引**（不是唯一约束），名字同样是 `ix_...`——这点跟 `UniqueConstraint`
  （`uq_...`）不一样，写迁移时别混。
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_MYSQL = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("details", sa.String(length=255), nullable=False),
        sa.Column("jd_text", sa.Text(), nullable=True),
        sa.Column("jd_complete", sa.Boolean(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("scanned_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        **_MYSQL,
    )
    op.create_index("ix_jobs_title", "jobs", ["title"], unique=True)
    op.create_index("ix_jobs_state", "jobs", ["state"])

    op.create_table(
        "candidates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("boss_key", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("job_and_company", sa.String(length=255), nullable=False),
        sa.Column("work_status", sa.String(length=32), nullable=False),
        sa.Column("work_year", sa.String(length=32), nullable=False),
        sa.Column("degree", sa.String(length=16), nullable=False),
        sa.Column("age", sa.String(length=16), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("expect_job_city", sa.String(length=128), nullable=False),
        sa.Column("expect_salary", sa.String(length=64), nullable=False),
        sa.Column("expect_industry", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.String(length=128), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("company_history", sa.JSON(), nullable=False),
        sa.Column("active", sa.String(length=32), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        **_MYSQL,
    )
    op.create_index("ix_candidates_boss_key", "candidates", ["boss_key"], unique=True)
    op.create_index("ix_candidates_name", "candidates", ["name"])

    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("candidate_id", sa.Integer(), sa.ForeignKey("candidates.id"), nullable=True),
        sa.Column("boss_name", sa.String(length=64), nullable=False),
        sa.Column("job_title", sa.String(length=128), nullable=False),
        sa.Column("last_msg", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("boss_time", sa.String(length=32), nullable=False),
        sa.Column("unread", sa.Integer(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        **_MYSQL,
    )
    op.create_index("ix_conversations_candidate_id", "conversations", ["candidate_id"])
    op.create_index("ix_conversations_boss_name", "conversations", ["boss_name"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"),
                  nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("boss_y", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        # ★ BOSS 不给消息 id，只能用内容去重（见 models.py 模块文档第 2 条妥协）。
        #   同一个人把同一句话发两遍会被并成一条——这是可接受的代价，
        #   因为反过来（每次同步都产生重复）代价更大。
        sa.UniqueConstraint("conversation_id", "direction", "text", name="uq_message_dedupe"),
        **_MYSQL,
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index("ix_messages_direction", "messages", ["direction"])

    op.create_table(
        "greet_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("boss_key", sa.String(length=255), nullable=False),
        sa.Column("job_title", sa.String(length=128), nullable=False),
        sa.Column("greeted_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        **_MYSQL,
    )
    op.create_index("ix_greet_log_boss_key", "greet_log", ["boss_key"], unique=True)


def downgrade() -> None:
    op.drop_table("greet_log")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("candidates")
    op.drop_table("jobs")
