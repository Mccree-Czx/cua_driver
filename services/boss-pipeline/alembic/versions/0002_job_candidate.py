"""照猎聘的形状补四张表：job_candidate / interactions / review_overrides / task_logs；
jobs 补一列 status。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16

## 这四张表补的是什么

猎聘是 **job_candidate 中心**：沟通、状态、评分全挂在「某人在某个职位下」这条记录上。
BOSS 的牛人推荐**天然按职位**（牛人 tab 顶部有职位筛选器），所以也是 jc 形状。

★ 但 **`messages` 与 `interactions` 是两件事，别合并**：
  - `messages` = 手机聊天镜像（原始证据，给前端展示、判「对方回没回」）
  - `interactions` = 系统动作账本（给状态机推进 + 「一人一消息」联锁）
  两者有交集，但把对方说的话并进安全账本会污染联锁。

★ `task_logs` **没有 account_id**——BOSS 单账号，猎聘那个按账号分片的维度在这里不存在。

★ `jobs.status` 与 BOSS 原生的 `state` **并存**：一个是「系统敢不敢用」，
  一个是「BOSS 让不让我招」。非 active 的职位在工具层看不见（联锁在代码里）。
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_MYSQL = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}


def upgrade() -> None:
    # —— jobs 补 status ——
    op.add_column(
        "jobs",
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'active'")),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "job_candidate",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("candidate_id", sa.Integer(), sa.ForeignKey("candidates.id"), nullable=False),
        # CandidateStatus 的字符串值（不建 MySQL enum：状态机会演进）
        sa.Column("status", sa.String(length=32), nullable=False,
                  server_default=sa.text("'new'")),
        sa.Column("judge_reason", sa.Text(), nullable=False),
        sa.Column("last_touch_at", sa.DateTime(), nullable=True),
        sa.Column("resume_requested_at", sa.DateTime(), nullable=True),
        sa.Column("resume_downloaded_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("job_id", "candidate_id",
                            name="uq_job_candidate_job_id_candidate_id"),
        **_MYSQL,
    )
    op.create_index("ix_job_candidate_job_id", "job_candidate", ["job_id"])
    op.create_index("ix_job_candidate_candidate_id", "job_candidate", ["candidate_id"])
    op.create_index("ix_job_candidate_status", "job_candidate", ["status"])

    op.create_table(
        "interactions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_candidate_id", sa.Integer(),
                  sa.ForeignKey("job_candidate.id"), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),   # out | in
        sa.Column("msg_type", sa.String(length=32), nullable=False),   # greet/request_resume/reply/attachment
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        **_MYSQL,
    )
    op.create_index("ix_interactions_job_candidate_id", "interactions", ["job_candidate_id"])

    op.create_table(
        "review_overrides",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_candidate_id", sa.Integer(),
                  sa.ForeignKey("job_candidate.id"), nullable=False),
        sa.Column("old_status", sa.String(length=32), nullable=False),
        sa.Column("new_status", sa.String(length=32), nullable=False),
        sa.Column("operator", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        **_MYSQL,
    )
    op.create_index("ix_review_overrides_job_candidate_id", "review_overrides",
                    ["job_candidate_id"])

    op.create_table(
        "task_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("round_name", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Float(), nullable=False),
        sa.Column("duration", sa.Float(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        **_MYSQL,
    )
    op.create_index("ix_task_logs_task_id", "task_logs", ["task_id"])
    op.create_index("ix_task_logs_round_name", "task_logs", ["round_name"])


def downgrade() -> None:
    op.drop_table("task_logs")
    op.drop_table("review_overrides")
    op.drop_table("interactions")
    op.drop_table("job_candidate")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_column("jobs", "status")
