"""pipeline 数据模型（spec v1.6 §4，SQLAlchemy 2.0 Mapped 风格）。

schema 唯一 owner 是 Alembic（alembic/versions/0001_init.py）；本文件与迁移
须逐列一致，`alembic check`（tests/test_migrations.py）守卫无漂移。

架构裁定（D4）：
- source / direction / msg_type：MySQL 原生 ENUM（sa.Enum 原生渲染）
- status 类字段：VARCHAR(32) 存 Python 枚举字符串值（CandidateStatus 来自
  contracts 包；状态机会演进，不建 MySQL enum）
- 一人一消息（每 job_candidate 至多 1 条 out）：应用层保证（T4/T7），
  M1 不建 DB 级约束
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from hr_workbuddy import CandidateStatus

# jobs.status：spec §4 未枚举取值，M1 岗位创建后即为 active
JOB_STATUS_ACTIVE = "active"

CANDIDATE_SOURCES = ("inbound", "recommended")
INTERACTION_DIRECTIONS = ("out", "in")
INTERACTION_MSG_TYPES = ("greet_request", "reply", "attachment", "direct_request")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    jd_text: Mapped[str] = mapped_column(Text, nullable=False)
    hard_rules: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    template_msgs: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    scoring_prefs: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    llm_threshold: Mapped[int] = mapped_column(
        Integer, nullable=False, default=40, server_default=text("70")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=JOB_STATUS_ACTIVE,
        server_default=text("'active'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    def __init__(self, **kwargs: Any) -> None:
        """构造期默认值：列 default 只在 INSERT 时生效，构造期读不到。"""
        kwargs.setdefault("hard_rules", {})
        kwargs.setdefault("template_msgs", {})
        kwargs.setdefault("scoring_prefs", {})
        kwargs.setdefault("llm_threshold", 40)
        kwargs.setdefault("status", JOB_STATUS_ACTIVE)
        super().__init__(**kwargs)


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    liepin_user_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    online_resume_minimal: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    snapshot_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    resume_ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(
        Enum(*CANDIDATE_SOURCES, name="candidate_source"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        # MySQL 反射把 ON UPDATE 捆进 server default 文本，alembic MySQL impl
        # 按「两侧都带 on update 子句」比较——server_default 必须显式带上
        server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        onupdate=func.now(),
    )

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("online_resume_minimal", {})
        super().__init__(**kwargs)


class JobCandidate(Base):
    __tablename__ = "job_candidate"
    __table_args__ = (
        UniqueConstraint(
            "job_id", "candidate_id", name="uq_job_candidate_job_id_candidate_id"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", name="fk_job_candidate_job_id_jobs"), nullable=False
    )
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", name="fk_job_candidate_candidate_id_candidates"),
        nullable=False,
        index=True,
    )
    match_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    judge_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CandidateStatus.NEW.value,
        server_default=text("'new'"),
    )
    minio_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    resume_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_touch_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # spec 补充字段（D4）：72h 关闭锚点，last_touch_at 不够
    resume_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    # 关系非列、不进 schema（Alembic 无感知）：状态机（T4）经 jc.candidate.source
    # 做 inbound/outbound 路径消歧，编排层读候选人快照
    candidate: Mapped["Candidate"] = relationship(
        "Candidate", foreign_keys=[candidate_id], lazy="selectin"
    )

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("status", CandidateStatus.NEW.value)
        super().__init__(**kwargs)


class Interaction(Base):
    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_candidate_id: Mapped[int] = mapped_column(
        ForeignKey("job_candidate.id", name="fk_interactions_job_candidate_id_job_candidate"),
        nullable=False,
        index=True,
    )
    direction: Mapped[str] = mapped_column(
        Enum(*INTERACTION_DIRECTIONS, name="interaction_direction"), nullable=False
    )
    msg_type: Mapped[str] = mapped_column(
        Enum(*INTERACTION_MSG_TYPES, name="interaction_msg_type"), nullable=False
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ReviewOverride(Base):
    """人工复核流水（spec §4：复核推翻可跨任意非终态，写本表并直接改 status）。"""

    __tablename__ = "review_overrides"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_candidate_id: Mapped[int] = mapped_column(
        ForeignKey("job_candidate.id", name="fk_review_overrides_job_candidate_id_job_candidate"),
        nullable=False,
        index=True,
    )
    old_status: Mapped[str] = mapped_column(String(32), nullable=False)
    new_status: Mapped[str] = mapped_column(String(32), nullable=False)
    operator: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class TaskLog(Base):
    """任务成本落账（spec §5 成本失控；M1 只落账，M4 按日/轮聚合）。

    tokens/cost 单位为整数 token 数与浮点元（保留 6 位微元精度，列取
    Float）；duration 为任务耗时秒数；note 为事件注记（如「迟到附件，只存
    不推进」），可空。B 方案：由 hr-tools 的 tasklog_add 工具落账。
    """

    __tablename__ = "task_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    cost: Mapped[float] = mapped_column(Float, nullable=False, default=0, server_default=text("0"))
    duration: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default=text("0"))
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("attempt", 0)
        kwargs.setdefault("tokens", 0)
        kwargs.setdefault("cost", 0)
        kwargs.setdefault("duration", 0.0)
        super().__init__(**kwargs)
