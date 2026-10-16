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
   `oppositeUserId`）。只能用 `name｜job_and_company` 拼一个 `boss_key`。
   **重名 + 同公司会撞**——所以 `boss_key` 上有唯一索引，撞了宁可报错也不要静默合并。
2. **消息没有稳定 id**。只能用 `(conversation_id, direction, text)` 去重。
   **同一个人把同一句话发两遍会被当成一条**——这是可接受的代价，
   因为反过来（重复入库）会让每一次同步都产生重复。
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

#: 职位状态。★ **直接存 BOSS 给的中文**，不做 ascii 映射。
#: 理由：这是 BOSS 的原话，映射一层就多一层可能漂移的东西，
#: 而且前端本来就要原样显示。查询用它过滤也没问题（VARCHAR 比较）。
JOB_STATES = ("开放中", "待开放", "已关闭", "审核未通过")

#: 消息方向。in = 候选人发来的，out = 我们发出去的。
#: ★ 这个值**不是从 rid 读的**（收发同一个 rid），是 flows 按头像左右位置判出来的。
MESSAGE_DIRECTIONS = ("in", "out")


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

    scanned_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
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

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
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
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    candidate: Mapped["Candidate | None"] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Conversation {self.id} {self.boss_name!r}>"


class Message(Base):
    """聊天页里的一条消息。"""

    __tablename__ = "messages"
    __table_args__ = (
        # ★ BOSS 不给消息 id，只能用内容去重，见模块文档第 2 条妥协。
        UniqueConstraint("conversation_id", "direction", "text", name="uq_message_dedupe"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    #: in = 候选人发来的；out = 我们发出去的（flows 按头像左右位置判定）
    direction: Mapped[str] = mapped_column(String(8), index=True)
    text: Mapped[str] = mapped_column(Text)
    #: 抓取时这条消息在屏幕上的 y 坐标。**只用来排序**——
    #: BOSS 不给消息时间戳，屏幕顺序是唯一可得的先后关系。
    boss_y: Mapped[int] = mapped_column(Integer, default=0)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

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
    greeted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
