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
    Boolean,
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
# 扫到了、但还没配 JD 的职位（2026-10-15）。
# `job_list_active` 与 `job_quota_status` 都硬过滤 active，所以 draft 的职位
# **在工具层就看不见**——Pi 不会拿一个空 JD 去给真实候选人打分。联锁在代码里，不在提示词里。
JOB_STATUS_DRAFT = "draft"

CANDIDATE_SOURCES = ("inbound", "recommended")
INTERACTION_DIRECTIONS = ("out", "in")
INTERACTION_MSG_TYPES = ("greet_request", "reply", "attachment", "direct_request")


class Account(Base):
    """猎聘招聘账号（多账号支持，2026-10-14）。

    系统的执行端是手机上的猎聘 App，同一时刻只登录一个招聘号。切号由人工在
    手机上完成，系统只负责「知道当前是哪个号」并按账号隔离数据。

    is_active 放数据库而不是 Redis：这是配置性状态，要持久、要进备份。
    Redis 一刷就丢失当前账号，系统便不知道该按哪个号算配额。应用层保证
    全局最多一个为 true（切换走 hr_api 的 activate 端点，事务内保证）。

    phone / password_enc 是账号的凭据（2026-10-14 补）。手机号是账号的真实
    身份——系统靠它核对「手机上登录的到底是不是这个号」；密码只存密文
    （见 app/crypto.py），用途是换号失败时的回滚保障。

    两者都可空：存量那行「默认账号」（id=1）没有凭据，且 1845 条候选人挂在
    它名下，不能为加列去动它。MySQL 唯一索引允许多个 NULL，所以 phone 上的
    唯一约束不会与存量的 NULL 冲突。
    """

    __tablename__ = "accounts"
    __table_args__ = (
        # 显式命名：Base 没有 naming_convention，匿名的 unique=True 会与迁移里
        # 手写的约束名对不上，`alembic check` 随即报漂移（scoring_prefs 前车之鉴）。
        UniqueConstraint("phone", name="uq_accounts_phone"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0"), index=True
    )
    # 一个手机号 = 一个猎聘账号，唯一约束防重复建档
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Fernet 密文，明文永不落库；解密见 app/crypto.decrypt_secret
    password_enc: Mapped[str | None] = mapped_column(String(512), nullable=True)
    credential_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("is_active", False)
        super().__init__(**kwargs)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 职位天然属于某个账号：daily_round 的「职位检测」读的就是当前登录号的职位列表
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", name="fk_jobs_account_id_accounts"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    jd_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 扫职位时从卡片上一并读回来的信息（2026-10-15）。卡片原文形如
    # 「机械结构工程师\n上海-黄浦区 本科 5年以上 25-45k」，拆成四列存。
    # 全部可空：存量职位是手工建的，没有这些值。
    location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    education: Mapped[str | None] = mapped_column(String(64), nullable=True)
    experience: Mapped[str | None] = mapped_column(String(64), nullable=True)
    salary: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    __table_args__ = (
        # 多账号支持（2026-10-14）：liepin_user_id 由「全局唯一」改为「账号内唯一」。
        # 同一个候选人在两个招聘号下要被各自建档——否则第二个号会把第一个号的
        # 记录当成自己的（或直接撞唯一键）。
        UniqueConstraint(
            "account_id", "liepin_user_id", name="uq_candidate_account_liepin"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", name="fk_candidates_account_id_accounts"),
        nullable=False,
        index=True,
    )
    liepin_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    online_resume_minimal: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    snapshot_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    resume_html_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 附件归档证据（capture_attachment_pages 写入的分页 MinIO 键列表）。
    # 仅由 capture_attachment_pages 写；snapshot_put（在线简历截图）不写。
    # 供 receive_resume 守卫区分「真附件」与「在线截图」。NULL = 尚无附件证据。
    attachment_keys: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
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
    # 冗余的账号维度（2026-10-14）：账号实际可由 job 推出，但系统里有 ~35 处
    # 查询以 job_candidate 为枢纽（hr_api 十个端点 + 26 个 hr-tools）。留着冗余列
    # 让每处过滤都是同一句 `.where(account_id == acct.id)`，不必逐个补 JOIN——
    # 漏掉任何一处 JOIN 都会静默造成跨账号数据泄露，这个风险比冗余更大。
    # 一致性由写入侧保证：建档时从 job 取一次，职位不会改账号。
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", name="fk_job_candidate_account_id_accounts"),
        nullable=False,
        index=True,
    )
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
    # 可空：轮次期间落的账（含人工队列）带账号，系统级汇总（token 成本）留 NULL。
    # /manual-queue 必须按它过滤——那些条目会带出候选人姓名，不过滤就是跨账号泄露。
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", name="fk_task_logs_account_id_accounts"),
        nullable=True,
        index=True,
    )
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
