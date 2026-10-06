"""init: §4 全部表 + spec 补充字段 + TaskLog（spec §5）

Revision ID: 0001
Revises:
Create Date: 2026-10-05

手写迁移，与 app/models.py 逐列一致（alembic check 守卫）：
- source/direction/msg_type：MySQL 原生 ENUM（D4）
- status 类字段：VARCHAR(32)（Python 枚举存字符串值，不建 MySQL enum）
- job_candidate UNIQUE(job_id, candidate_id) + resume_requested_at（72h 锚点）
- 外键/索引全部显式命名，避免 InnoDB 自动索引与元数据不一致
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("jd_text", sa.Text(), nullable=False),
        sa.Column("hard_rules", sa.JSON(), nullable=False),
        sa.Column("template_msgs", sa.JSON(), nullable=False),
        sa.Column("llm_threshold", sa.Integer(), nullable=False, server_default=sa.text("70")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "candidates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("liepin_user_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("online_resume_minimal", sa.JSON(), nullable=False),
        sa.Column("snapshot_object_key", sa.String(length=512), nullable=True),
        sa.Column(
            "source",
            sa.Enum("inbound", "recommended", name="candidate_source"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("liepin_user_id", name="liepin_user_id"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "job_candidate",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False, index=True),
        sa.Column("match_score", sa.Integer(), nullable=True),
        sa.Column("judge_reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'new'")),
        sa.Column("minio_object_key", sa.String(length=512), nullable=True),
        sa.Column("resume_downloaded_at", sa.DateTime(), nullable=True),
        sa.Column("last_touch_at", sa.DateTime(), nullable=True),
        sa.Column("resume_requested_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint(
            "job_id", "candidate_id", name="uq_job_candidate_job_id_candidate_id"
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name="fk_job_candidate_job_id_jobs"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidates.id"],
            name="fk_job_candidate_candidate_id_candidates",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "interactions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_candidate_id", sa.Integer(), nullable=False, index=True),
        sa.Column(
            "direction",
            sa.Enum("out", "in", name="interaction_direction"),
            nullable=False,
        ),
        sa.Column(
            "msg_type",
            sa.Enum("greet_request", "reply", "attachment", name="interaction_msg_type"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_candidate_id"],
            ["job_candidate.id"],
            name="fk_interactions_job_candidate_id_job_candidate",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "review_overrides",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_candidate_id", sa.Integer(), nullable=False, index=True),
        sa.Column("old_status", sa.String(length=32), nullable=False),
        sa.Column("new_status", sa.String(length=32), nullable=False),
        sa.Column("operator", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["job_candidate_id"],
            ["job_candidate.id"],
            name="fk_review_overrides_job_candidate_id_job_candidate",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "task_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(length=36), nullable=False, index=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("duration", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )


def downgrade() -> None:
    op.drop_table("task_logs")
    op.drop_table("review_overrides")
    op.drop_table("interactions")
    op.drop_table("job_candidate")
    op.drop_table("candidates")
    op.drop_table("jobs")
