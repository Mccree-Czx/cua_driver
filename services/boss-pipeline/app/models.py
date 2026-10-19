"""boss-pipeline 数据模型。

schema 的唯一 owner 是 Alembic；本文件与迁移须逐列一致。

## 这些字段是怎么定出来的

**不是照 BOSS 的界面抄的，也不是凭空设计的**——是照着 `agent_boss/flows.py`
里那些**已经在真机上验证过**的只读流程，逐个字段对出来的：

| 流程 | 实际产出 |
|---|---|
| `scan_jobs()` | 职位名 / 状态 / 「上海·四川北路 ｜ 本科 ｜ 3-5年 ｜ 18-25K·13薪 ｜ 全职」 |
| `fetch_jd()` | `jd_text` + `complete` + `btn_word` 标签 |
| `scan_candidates()` | 姓名 / 概要 / 工作经历 / 标签 |
| `read_candidates()` | 公司·职位 / 在职状态 / 经验 / 学历 / 年龄 / 自我描述 / 求职期望三项 |
| `scan_conversations()` | 姓名 / 关联职位 / 时间 / 状态标记 / 最后一条 |
| `read_conversation()` | 消息（**带方向**）/ 时间分隔 |

**流程采不到的东西，这里就没有字段。** 想加字段，先去 `flows.py` 里把它采出来——
凭空加一个永远是 NULL 的列，只会让下一个人以为「这里本该有数据」。

## 两个不得已的妥协（都是 BOSS 不给稳定 id 造成的）

1. **候选人没有稳定 id**。BOSS 的卡片与详情页里都没有（猎聘至少 logcat 里有
   `oppositeUserId`）。原本打算用「姓名｜公司」拼 `boss_key`，**真机实测推翻了**：
   牛人列表卡给的公司串是 `云南白药 · 数据开发`，详情页给的是
   `云南白药集团·数据开发`——**同一个人会变成两条记录**。
   所以退回**只用姓名**。
   **代价**：同职位下同名的两个人会被并成一条。这比「一个人变两条」好得多——
   后者会让「一人一消息」的联锁直接失效（同一个人被打两次招呼）。
   公司信息照常存在 `job_and_company` 里，只是不参与主键。
   （详见 `app/api.py::_boss_key`。）
2. **消息没有稳定 id**。只能用 `(conversation_id, direction, text)` 去重。
   **同一个人把同一句话发两遍会被当成一条**——这是可接受的代价，
   因为反过来（重复入库）会让每一次同步都产生重复。
"""

import hashlib
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    # ★ 别名，**不能直接用 `text`**：`Message` 类里有一个列就叫 `text`，
    #   它会在那个类的作用域里把 sqlalchemy 的 `text` 遮蔽掉，
    #   于是同一类里后面的 `text("CURRENT_TIMESTAMP")` 变成在调用列对象 →
    #   `TypeError: 'MappedColumn' object is not callable`。
    #   （2026-10-16 真撞上，只有 Message 这个类会中招。）
    text as _text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

# ★ 状态枚举**复用猎聘那份**（packages/contracts），不在这里另立一份：
#   两边前端都要按状态上色/上标签，枚举一旦分叉就会各说各话。
from hr_workbuddy import CandidateStatus

from app.db import Base

#: 职位状态。★ **直接存 BOSS 给的中文**，不做 ascii 映射。
#: 理由：这是 BOSS 的原话，映射一层就多一层可能漂移的东西，
#: 而且前端本来就要原样显示。查询用它过滤也没问题（VARCHAR 比较）。
JOB_STATES = ("开放中", "待开放", "已关闭", "审核未通过")

#: 消息方向。in = 候选人发来的，out = 我们发出去的。
#: ★ 这个值**不是从 rid 读的**（收发同一个 rid），是 flows 按头像左右位置判出来的。
MESSAGE_DIRECTIONS = ("in", "out")

# ——— 系统侧的职位状态（跟 BOSS 原生的 `state` 是**两件事**）———
#
# `state` 是 BOSS 说的（开放中/待开放/已关闭）；`status` 是**我们敢不敢用**。
# 一个职位可以 BOSS 那边「开放中」而我们这边仍是 draft——因为 JD 还没读全。
#
# 照猎聘的联锁：**非 active 的职位在工具层就看不见**，流程不会拿一个空 JD
# 去给真实候选人发消息。联锁焊在代码里，不在提示词里。
JOB_STATUS_ACTIVE = "active"
JOB_STATUS_DRAFT = "draft"

#: 系统动作流水的类型。★ **收窄到 BOSS 真有的**：
#: 猎聘有 4 种（greet_request/reply/attachment/direct_request），BOSS 没有
#: 「直索简历」那条语义——BOSS 的「求简历」是聊天页上的一个动作。
INTERACTION_MSG_TYPES = ("greet", "request_resume", "reply", "attachment")


class Job(Base):
    """BOSS 上的一个职位。"""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: BOSS 上的职位名。**发布后不可修改**（页面原文），所以拿它当业务主键是安全的。
    title: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    #: 开放中 / 待开放 / 已关闭 / 审核未通过
    state: Mapped[str] = mapped_column(String(16), default="", index=True)
    #: 列表页那一行的原文，例如「上海·四川北路 ｜ 本科 ｜ 3-5年 ｜ 18-25K·13薪 ｜ 全职」。
    #: 拆成独立列是**故意不做**的：BOSS 的格式会变，拆了就跟着碎。
    #: 需要结构化时按 ` ｜ ` 切——但那属于展示层的事。
    details: Mapped[str] = mapped_column(String(255), default="")

    #: 职位详情页的 JD。打分与检索唯一依据。
    jd_text: Mapped[str | None] = mapped_column(Text)
    #: JD 是否**确认读全了**（flows 里做了滚动增长检测）。
    #: ★ 只有 complete 的 JD 才该进入下游——半截 JD 打出来的分是错的。
    jd_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    #: 职位标签（`btn_word`），例如 ["要求数据开发经验", "PostgreSQL", ...]
    tags: Mapped[list] = mapped_column(JSON, default=list)

    #: 系统侧启用状态：active / draft。见文件上方 JOB_STATUS_* 的说明——
    #: 它跟 BOSS 原生的 `state` **并存**，不是一回事。
    #:
    #: ★ 这里**同时**有 Python 侧 `default` 与 DB 侧 `server_default`，而且两边必须一致：
    #:   只写 Python 侧的话，`alembic check` 会因为迁移里建了 DB 默认值而报
    #:   `modify_default` 漂移（猎聘当年在 `scoring_prefs` 上栽过同一跤）；
    #:   只写 DB 侧的话，ORM 构造期读不到值。
    status: Mapped[str] = mapped_column(
        String(16), default=JOB_STATUS_ACTIVE,
        server_default=_text(f"'{JOB_STATUS_ACTIVE}'"), index=True,
    )

    #: 评分偏好。形状照猎聘（`{boosters, veto, requirements, min_stars}`）。
    #:
    #: ★ **现在没有消费者**——BOSS 的打分链路还没做（用户已定「先不做打分」）。
    #:   保留这一列是因为：① 前端那个「评分偏好」页要编辑它（用户明确要保留形状）；
    #:   ② 真接上打分时，它就是 scoring 服务的输入，不用再改表。
    #:   照猎聘的教训：**这一列刻意不设 server_default**（它当年就是因为
    #:   MySQL 反射出 `(_utf8mb4'{}')` 而 `alembic check` 漂了五天）。
    scoring_prefs: Mapped[dict] = mapped_column(JSON, default=dict)

    scanned_at: Mapped[datetime] = mapped_column(DateTime, server_default=_text("CURRENT_TIMESTAMP"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=_text("CURRENT_TIMESTAMP"), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Job {self.id} {self.title!r} {self.state}>"


class Candidate(Base):
    """BOSS 上的一个候选人（牛人）。"""

    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: ★ 「姓名｜当前公司·职位」拼出来的稳定键。**BOSS 不给稳定 id**，见模块文档。
    boss_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)

    job_and_company: Mapped[str] = mapped_column(String(255), default="")
    work_status: Mapped[str] = mapped_column(String(32), default="")   # 离职-随时到岗
    work_year: Mapped[str] = mapped_column(String(32), default="")     # 10年以上
    degree: Mapped[str] = mapped_column(String(16), default="")        # 本科
    age: Mapped[str] = mapped_column(String(16), default="")           # 37岁
    description: Mapped[str | None] = mapped_column(Text)              # 自我描述

    expect_job_city: Mapped[str] = mapped_column(String(128), default="")  # 数据开发，上海
    expect_salary: Mapped[str] = mapped_column(String(64), default="")     # 面议
    expect_industry: Mapped[str] = mapped_column(String(64), default="")   # 行业不限

    #: 列表页的概要（如「10年以上 ｜ 本科 ｜ 面议」）与标签
    summary: Mapped[str] = mapped_column(String(128), default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    #: 详情页的工作经历：["腾讯音乐娱乐科技（深圳）有限公司", "2021.01-至今", ...]
    company_history: Mapped[list] = mapped_column(JSON, default=list)
    #: 列表页的活跃度标记（如「刚刚活跃」「3日内活跃」）
    active: Mapped[str] = mapped_column(String(32), default="")

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=_text("CURRENT_TIMESTAMP"))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=_text("CURRENT_TIMESTAMP"), onupdate=func.now()
    )

    conversations: Mapped[list["Conversation"]] = relationship(back_populates="candidate")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Candidate {self.id} {self.name!r}>"


class Conversation(Base):
    """消息 tab 里的一个会话。"""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidates.id"), index=True
    )
    #: 会话列表上显示的姓名与关联职位（与 Candidate 冗余一份：
    #: 会话可能来自还没进过详情页的人，那时 Candidate 行是空的）
    boss_name: Mapped[str] = mapped_column(String(64), index=True)
    job_title: Mapped[str] = mapped_column(String(128), default="")

    last_msg: Mapped[str] = mapped_column(String(512), default="")
    #: BOSS 给的标记，如「[新招呼]」「[送达]」。原样存（同 Job.state 的理由）。
    status: Mapped[str] = mapped_column(String(32), default="")
    boss_time: Mapped[str] = mapped_column(String(32), default="")   # 「16:36」
    unread: Mapped[int] = mapped_column(Integer, default=0)

    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=_text("CURRENT_TIMESTAMP"), onupdate=func.now()
    )

    candidate: Mapped["Candidate | None"] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Conversation {self.id} {self.boss_name!r}>"


def content_hash(text: str) -> str:
    """消息内容的哈希（sha256 hex）。**去重用的键，不是给人看的。**

    ★★ 为什么要哈希而不是直接拿 `text` 做唯一键（2026-10-16 撞的）：
      **MySQL 不允许 `TEXT` 列直接进唯一键**——
      `ERROR 1170 (42000): BLOB/TEXT column 'text' used in key specification
      without a key length`。SQLite 允许，所以本地一路跑得好好的，
      直到第一次对着 MySQL 跑迁移才炸。

      三条路里选了哈希：
      - 加前缀长度 `text(191)` —— **错的**：两条前 191 字相同、后面不同的消息
        会被当成同一条而**被丢掉**。消息是要给人看的原始证据，丢不得。
      - 改成 VARCHAR(512) —— 消息长度没上界（候选人可能把简历正文贴进来），会截断。
      - **存哈希** —— 长度固定、无上界问题、语义就是「内容相同即同一行」。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Message(Base):
    """聊天页里的一条消息。"""

    __tablename__ = "messages"
    __table_args__ = (
        # ★ BOSS 不给消息 id，只能用内容去重，见模块文档第 2 条妥协。
        #   ★ 落在 `content_hash` 上而不是 `text` 上——MySQL 不接受 TEXT 进唯一键，
        #     理由见 `content_hash()`。
        UniqueConstraint("conversation_id", "direction", "content_hash",
                         name="uq_message_dedupe"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    #: in = 候选人发来的；out = 我们发出去的（flows 按头像左右位置判定）
    direction: Mapped[str] = mapped_column(String(8), index=True)
    text: Mapped[str] = mapped_column(Text)
    #: `text` 的 sha256。唯一键落在它上面——见 `content_hash()`。
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 抓取时这条消息在屏幕上的 y 坐标。**只用来排序**——
    #: BOSS 不给消息时间戳，屏幕顺序是唯一可得的先后关系。
    boss_y: Mapped[int] = mapped_column(Integer, default=0)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=_text("CURRENT_TIMESTAMP"))

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Message {self.id} {self.direction} {self.text[:20]!r}>"


class GreetLog(Base):
    """打招呼的账本（服务端侧）。

    ★ 采数端 `agent_boss/.greeted.json` 也有一份。**两边都要**：
      采数端那份保证「同一次运行内不重复」，这份保证「跨机器、跨重装也不重复」——
      而重复打招呼对真实的人是打扰，值得存两处。
    """

    __tablename__ = "greet_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    boss_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    job_title: Mapped[str] = mapped_column(String(128), default="")
    greeted_at: Mapped[datetime] = mapped_column(DateTime, server_default=_text("CURRENT_TIMESTAMP"))


# ————————————————————————————————————————————————————————————————
# 下面四张是照猎聘的形状补的（P1）。
# ————————————————————————————————————————————————————————————————

class JobCandidate(Base):
    """某人在某个职位下。**状态机跑在这一行上**（见 `app/state_machine.py`）。

    ★ BOSS 的牛人推荐**天然按职位**——牛人 tab 顶部就有个职位筛选器，
    列表是按某个在招职位推的（`agent_boss/flows.py::scan_candidates()` 也是
    按那个职位采的）。所以这边本来就是 jc 形状，**不是硬套猎聘的模型**。
    """

    __tablename__ = "job_candidate"
    __table_args__ = (
        UniqueConstraint("job_id", "candidate_id", name="uq_job_candidate_job_id_candidate_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id"), nullable=False, index=True
    )
    #: CandidateStatus 的**字符串值**（不建 MySQL enum：状态机会演进）
    #: Python 侧与 DB 侧的默认值必须一致，理由同 `Job.status`。
    status: Mapped[str] = mapped_column(
        String(32), default=CandidateStatus.NEW.value,
        server_default=_text(f"'{CandidateStatus.NEW.value}'"), index=True,
    )
    judge_reason: Mapped[str] = mapped_column(Text, default="")
    last_touch_at: Mapped[datetime | None] = mapped_column(DateTime)
    #: 「求过简历」的时刻。**72h 无人回应的锚点**（照猎聘的 sweep）。
    resume_requested_at: Mapped[datetime | None] = mapped_column(DateTime)
    resume_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime)
    #: 附件简历在 MinIO 里的对象键（`resumes/{job_id}/{candidate_id}/{filename}`）。
    #: 空 = 还没收到简历。收到后由 `/api/ingest/attachment` 写入。
    minio_object_key: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=_text("CURRENT_TIMESTAMP"))

    job: Mapped["Job"] = relationship()
    candidate: Mapped["Candidate"] = relationship()
    interactions: Mapped[list["Interaction"]] = relationship(back_populates="job_candidate")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<JobCandidate job={self.job_id} cand={self.candidate_id} {self.status}>"


class Interaction(Base):
    """**系统动作流水**——只记「系统对候选人做了什么」。

    ## ★ 跟 `messages` 的分工（别合并）

    | | `messages` | `interactions` |
    |---|---|---|
    | 是什么 | **手机聊天镜像**（in + out） | **系统动作账本** |
    | 谁写的 | 采集流程照抄屏幕 | 系统自己在动作前后写 |
    | 干什么用 | 前端展示、判「对方回没回」 | 状态机推进 + **一人一消息联锁** |

    两者有交集（我们发出去的招呼语两边都有），但**消费者不同**。
    把对方说的话并进安全账本会**污染联锁**——联锁只该看我们做过什么。
    """

    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_candidate_id: Mapped[int] = mapped_column(
        ForeignKey("job_candidate.id"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)   # out | in
    msg_type: Mapped[str] = mapped_column(String(32), nullable=False)   # 见 INTERACTION_MSG_TYPES
    content: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime] = mapped_column(DateTime, server_default=_text("CURRENT_TIMESTAMP"))

    job_candidate: Mapped["JobCandidate"] = relationship(back_populates="interactions")


class ReviewOverride(Base):
    """人工复核流水。每次 `/api/hr/candidates/{id}/review` 落一条。"""

    __tablename__ = "review_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_candidate_id: Mapped[int] = mapped_column(
        ForeignKey("job_candidate.id"), nullable=False, index=True
    )
    old_status: Mapped[str] = mapped_column(String(32), nullable=False)
    new_status: Mapped[str] = mapped_column(String(32), nullable=False)
    operator: Mapped[str] = mapped_column(String(64), default="hr")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=_text("CURRENT_TIMESTAMP"))


class TaskLog(Base):
    """轮次运行记录。

    ★ **没有 account_id**——BOSS 单账号，猎聘那个按账号分片的维度在这里不存在。
    """
    __tablename__ = "task_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    #: 轮次名：main_round / daily_round / backfill_round / login_round
    round_name: Mapped[str] = mapped_column(String(32), default="", index=True)
    outcome: Mapped[str] = mapped_column(String(32), default="")   # success / failed_needs_manual / timeout
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=_text("CURRENT_TIMESTAMP"))
