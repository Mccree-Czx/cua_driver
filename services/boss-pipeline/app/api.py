"""职位接口 + 采数端写入接口。

分两块：

- **`/api/health`、`/api/jobs*`**：跟猎聘的 `api.py` 对位（它也有这两个）。
- **`/api/ingest/*`**：给 Mac 上的 `agent_boss` 采数端推数据用，带令牌。
  ★ 猎聘**没有**这一层——它的执行端跟服务端同机、直接写库。
    BOSS 的采数端在 Mac 上，所以走 HTTP。

HR 面（驾驶舱/候选人/会话/人工队列/日志/日报/评分偏好）都在 `hr_api.py`。

★ 这里**没有账号/角色体系**——那是「统一身份」（飞书登录 + admin/user）那条线的事，
  还没开始。现在跑在本机/内网上，别把它暴露到公网。
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import hr_tools, models, storage
from app.deps import get_session, require_ingest_token
from app.state_machine import InvalidTransition, StateEvent, TransitionContext

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ——— 职位（只读；改职位的系统侧属性在 /api/hr/jobs/{id}）———

def _job_out(job: models.Job, *, with_jd: bool) -> dict[str, Any]:
    out = {
        "id": job.id, "title": job.title, "state": job.state, "details": job.details,
        "status": job.status, "jd_complete": job.jd_complete, "tags": job.tags or [],
        "jd_chars": len(job.jd_text or ""),
        "scanned_at": job.scanned_at.isoformat() if job.scanned_at else None,
    }
    if with_jd:
        out["jd_text"] = job.jd_text
        out["scoring_prefs"] = job.scoring_prefs or {}
    return out


@router.get("/api/jobs")
def list_jobs(
    session: SessionDep,
    state: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    stmt = select(models.Job)
    if state:
        stmt = stmt.where(models.Job.state == state)
    if status:
        stmt = stmt.where(models.Job.status == status)
    if q:
        stmt = stmt.where(models.Job.title.contains(q))
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(
        stmt.order_by(models.Job.state, models.Job.title).limit(limit).offset(offset)
    ).all()
    return {"total": total, "items": [_job_out(r, with_jd=False) for r in rows]}


@router.get("/api/jobs/{job_id}")
def get_job(job_id: int, session: SessionDep) -> dict[str, Any]:
    job = session.get(models.Job, job_id)
    if job is None:
        raise HTTPException(404, "没有这个职位")
    out = _job_out(job, with_jd=True)
    # 这个职位下的 jc，给「职位详情页」用
    rows = session.scalars(
        select(models.JobCandidate).where(models.JobCandidate.job_id == job_id)
    ).all()
    out["candidates"] = [
        {"jc_id": r.id, "candidate_id": r.candidate_id, "status": r.status,
         "name": r.candidate.name if r.candidate else ""}
        for r in rows
    ]
    return out


# ——— 写入：采数端推上来的 ———

def _token_header(x_ingest_token: Annotated[str | None, Header()] = None) -> None:
    require_ingest_token(x_ingest_token)


class JobIn(BaseModel):
    title: str
    state: str = ""
    details: str = ""
    jd_text: str | None = None
    jd_complete: bool = False
    tags: list[str] = Field(default_factory=list)


class CandidateIn(BaseModel):
    #: ★ **可以不传**，服务端会退化成用 `name`（见下面 `_boss_key`）。
    boss_key: str = ""
    name: str
    #: ★ 这个候选人是**从哪个职位的推荐流里采到的**。BOSS 的牛人推荐按职位，
    #:   所以有它才能建 job_candidate 那行。留空则不建（比如从会话列表看到的）。
    job_title: str = ""
    job_and_company: str = ""
    work_status: str = ""
    work_year: str = ""
    degree: str = ""
    age: str = ""
    description: str | None = None
    expect_job_city: str = ""
    expect_salary: str = ""
    expect_industry: str = ""
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    company_history: list[str] = Field(default_factory=list)
    active: str = ""


class MessageIn(BaseModel):
    direction: str
    text: str
    boss_y: int = 0


class ConversationIn(BaseModel):
    boss_name: str
    job_title: str = ""
    last_msg: str = ""
    status: str = ""
    boss_time: str = ""
    unread: int = 0
    messages: list[MessageIn] = Field(default_factory=list)


#: ★★ BOSS **对「开放中」的职位不打状态标签**（实测：职位管理页「全部」tab 里，
#: 在线职位那一行没有 `tv_job_state`，待开放/已关闭的才有）。
#: 所以采数端读回来的是**空串**。
#:
#: 不归一的话会出一条**静止的死链**：库里存 `""` → `hr_tools.job_list_active`
#: 筛 `state == "开放中"` 永远匹配不上 → `usable` 恒为 0 → 轮次闸门⑤永远跳过 →
#: **整条自动化什么都不做，而且看起来一切正常**。
#: （2026-10-16 真机联调就是这么撞上的：明明有个在招职位，overview 里「开放中」是 0。）
def _norm_state(raw: str) -> str:
    return raw.strip() or "开放中"


@router.post("/api/ingest/jobs", dependencies=[Depends(_token_header)])
def ingest_jobs(items: list[JobIn], session: SessionDep) -> dict[str, int]:
    """采数端推职位。按 `title` upsert（BOSS 的职位名发布后不可修改，拿它当业务键是安全的）。"""
    created = updated = 0
    for item in items:
        row = session.scalar(select(models.Job).where(models.Job.title == item.title))
        if row is None:
            row = models.Job(title=item.title)
            session.add(row)
            created += 1
        else:
            updated += 1
        row.state = _norm_state(item.state)
        row.details = item.details
        row.tags = item.tags
        # ★ JD 只在**这次读到、且读全了**的时候覆盖。
        #   否则一次失败的抓取会把好 JD 冲成半截——半截 JD 打出来的分是错的。
        if item.jd_text and item.jd_complete:
            row.jd_text = item.jd_text
            row.jd_complete = True
    session.commit()
    return {"created": created, "updated": updated}


def _boss_key(item: CandidateIn) -> str:
    """候选人的业务主键。

    ★★ **实测纠正**：原本打算用「姓名｜公司」，但真机上发现**两个页面拿到的
    公司字符串不是同一个东西**：

    | 来源 | 字段 | 实测值 |
    |---|---|---|
    | 牛人列表卡 | `works[0]`（`ll_work_exp`） | `云南白药 · 数据开发` |
    | 候选人详情页 | `job_and_company`（`tv_job_and_com`） | `云南白药集团·数据开发` |

    拼起来的话，**同一个人从两个页面进来会变成两条记录**。
    所以退回**只用姓名**——它在两个页面上都一样。

    代价：同一个职位下同名的两个人会被并成一条。取舍上这比「一个人变两条」
    好得多（后者会让「一人一消息」的联锁直接失效——同一个人被打两次招呼）。
    公司信息照常存在 `job_and_company` 里，只是不参与主键。
    """
    return item.boss_key or item.name


@router.post("/api/ingest/candidates", dependencies=[Depends(_token_header)])
def ingest_candidates(items: list[CandidateIn], session: SessionDep) -> dict[str, int]:
    """采数端推候选人。按 `boss_key` upsert；带了 `job_title` 就顺带建 job_candidate。"""
    created = updated = jc_created = 0
    for item in items:
        key = _boss_key(item)
        row = session.scalar(
            select(models.Candidate).where(models.Candidate.boss_key == key)
        )
        if row is None:
            row = models.Candidate(boss_key=key, name=item.name)
            session.add(row)
            created += 1
        else:
            updated += 1
        for field, value in item.model_dump(exclude={"boss_key", "job_title"}).items():
            # 详情页才有的字段，列表页推上来是空的——**别用空值盖掉已有的**
            if value in (None, "", [], {}) and getattr(row, field, None):
                continue
            setattr(row, field, value)
        session.flush()

        if item.job_title:
            job = session.scalar(select(models.Job).where(models.Job.title == item.job_title))
            if job is not None:
                before = session.scalar(
                    select(func.count()).select_from(models.JobCandidate)
                    .where(models.JobCandidate.job_id == job.id)
                    .where(models.JobCandidate.candidate_id == row.id)
                )
                hr_tools.jc_get_or_create(session, job.id, row.id)
                if not before:
                    jc_created += 1
    session.commit()
    return {"created": created, "updated": updated, "jc_created": jc_created}


@router.post("/api/ingest/conversations", dependencies=[Depends(_token_header)])
def ingest_conversations(items: list[ConversationIn], session: SessionDep) -> dict[str, int]:
    """采数端推会话与消息。

    消息按 `(conversation_id, direction, text)` 去重——BOSS 不给消息 id，
    见 `models.py` 模块文档第 2 条妥协。
    """
    conv_created = conv_updated = msg_created = msg_skipped = 0
    for item in items:
        row = session.scalar(
            select(models.Conversation).where(models.Conversation.boss_name == item.boss_name)
        )
        if row is None:
            row = models.Conversation(boss_name=item.boss_name)
            session.add(row)
            session.flush()
            conv_created += 1
        else:
            conv_updated += 1
        row.job_title, row.last_msg = item.job_title, item.last_msg
        row.status, row.boss_time, row.unread = item.status, item.boss_time, item.unread

        # 顺手把会话挂到候选人上（如果这个人采过）
        if row.candidate_id is None:
            cand = session.scalar(
                select(models.Candidate).where(models.Candidate.name == item.boss_name)
            )
            if cand is not None:
                row.candidate_id = cand.id

        # 去重键是 (direction, content_hash) 而**不是** (direction, text)：
        # 唯一键落在哈希上（MySQL 不接 TEXT 进唯一键），这里跟着走，
        # 免得「应用层判重」和「DB 层唯一键」两套口径对不上。
        existing = {(m.direction, m.content_hash) for m in row.messages}
        for msg in item.messages:
            h = models.content_hash(msg.text)
            if (msg.direction, h) in existing:
                msg_skipped += 1
                continue
            session.add(models.Message(
                conversation_id=row.id, direction=msg.direction,
                text=msg.text, content_hash=h, boss_y=msg.boss_y,
            ))
            existing.add((msg.direction, h))
            msg_created += 1
    session.commit()
    return {"conversations_created": conv_created, "conversations_updated": conv_updated,
            "messages_created": msg_created, "messages_skipped": msg_skipped}


@router.post("/api/ingest/greet-log", dependencies=[Depends(_token_header)])
def ingest_greet_log(items: list[dict[str, str]], session: SessionDep) -> dict[str, int]:
    """采数端推招呼账本。

    ★ 服务端这份是**第二道**保险：采数端那份只保证「同一次运行内不重复」，
      重装/换机就丢了。而重复打招呼对真实的人是打扰。
    """
    added = 0
    for item in items:
        key = item.get("boss_key", "")
        if not key:
            continue
        if session.scalar(select(models.GreetLog).where(models.GreetLog.boss_key == key)):
            continue
        session.add(models.GreetLog(boss_key=key, job_title=item.get("job", "")))
        added += 1
    session.commit()
    return {"added": added}


class TaskLogIn(BaseModel):
    """一轮运行的记录（控制台的「运行日志」与「人工队列」都读它）。

    ★ BOSS 的 `TaskLog` **没有 `account_id`**——BOSS 是单账号，猎聘那个按账号
      分片的维度在这里不存在。
    """

    round_name: str
    #: success / failed / **failed_needs_manual** / timeout
    #: ★ `failed_needs_manual` 是人工队列的唯一筛选条件（hr_api.manual_queue）。
    outcome: str = "success"
    task_id: str = ""
    attempt: int = 0
    tokens: int = 0
    cost: float = 0.0
    duration: float = 0.0
    note: str = ""


@router.post("/api/ingest/tasklog", dependencies=[Depends(_token_header)])
def ingest_tasklog(item: TaskLogIn, session: SessionDep) -> dict[str, int]:
    """采数端推一轮的运行记录。

    ★ 原先没有这个端点：`agent_boss` 不写 task_logs，于是控制台的「运行日志」
      和「人工队列」两个页面**永远是空的**——掉登录/采集失败时人也不知道。
    """
    row = models.TaskLog(
        task_id=item.task_id or f"{item.round_name}_{datetime.now():%Y%m%d_%H%M%S}",
        round_name=item.round_name,
        outcome=item.outcome,
        attempt=item.attempt,
        tokens=item.tokens,
        cost=item.cost,
        duration=item.duration,
        note=item.note[:2000],
    )
    session.add(row)
    session.commit()
    return {"id": row.id}


class InteractionIn(BaseModel):
    boss_name: str
    job_title: str = ""
    direction: str = "out"          # out | in
    msg_type: str = ""              # greet 等，见 INTERACTION_MSG_TYPES
    content: str = ""


@router.post("/api/ingest/interactions", dependencies=[Depends(_token_header)])
def ingest_interactions(items: list[InteractionIn], session: SessionDep) -> dict[str, int]:
    """采数端推「系统动作流水」。

    ★ `hr_tools.has_out_greet()`（一人一消息联锁）读的就是这张表：
      `direction=out` + `msg_type=greet`。而在此之前**没有任何写入者**——
      服务端的重复打招呼联锁等于从未生效。

    ★ 与 `messages` 的分工见 models.Interaction 的 docstring：这里只记
      **我们做过什么**，不把对方说的话并进来（那会污染联锁）。
    """
    created = skipped = 0
    for item in items:
        jc = session.scalar(
            select(models.JobCandidate)
            .join(models.Candidate, models.Candidate.id == models.JobCandidate.candidate_id)
            .join(models.Job, models.Job.id == models.JobCandidate.job_id)
            .where(models.Candidate.name == item.boss_name)
            .where(models.Job.title == item.job_title)
        )
        if jc is None:
            skipped += 1
            continue
        session.add(models.Interaction(
            job_candidate_id=jc.id,
            direction=item.direction,
            msg_type=item.msg_type,
            content=item.content,
        ))
        # ★ 真发了招呼 → 状态推进到 greeted（让闸门与控制台都看得到）。
        #   已经在终态/非 screened_pass 的只落流水、不推进（忽略 InvalidTransition）。
        if item.direction == "out" and item.msg_type == "greet":
            try:
                hr_tools.apply_transition(
                    session, jc, StateEvent.GREET, TransitionContext(source="recommended")
                )
            except InvalidTransition:
                pass
        created += 1
    session.commit()
    return {"created": created, "skipped": skipped}


class GreetCheckIn(BaseModel):
    boss_name: str
    job_title: str = ""
    #: 干跑：只查风控与重复，**不扣配额**。
    dry_run: bool = False


@router.post("/api/greet/check", dependencies=[Depends(_token_header)])
def greet_check(item: GreetCheckIn, session: SessionDep) -> dict[str, Any]:
    """打招呼前的服务端联锁：**风控熔断 → 重复打招呼 → 评分 → 扣配额**。

    ★ `hr_tools.may_greet()` 的唯一调用方。在加这个端点之前**没有任何人调它**，
      于是每日配额和风控熔断对 BOSS 执行端形同虚设——只要本地账本没记过，
      想发多少条就发多少条。

    ★ 评分闸门（P2）：`may_greet` 会在 `new` 上**按需评一次分**，只有
      `screened_pass` 才放行；评分服务不可用则 fail-closed。

    ★ 真发路径在「检查通过」时就 `quota_acquire()` 扣一次配额；即使随后手机
      点击失败，这个名额也算用掉。保守方向，可接受。
    """
    jc = session.scalar(
        select(models.JobCandidate)
        .join(models.Candidate, models.Candidate.id == models.JobCandidate.candidate_id)
        .join(models.Job, models.Job.id == models.JobCandidate.job_id)
        .where(models.Candidate.name == item.boss_name)
        .where(models.Job.title == item.job_title)
    )
    if jc is None:
        return {"allowed": False, "reason": "库里没有这个候选人/职位的档案（先采集）"}

    if item.dry_run:
        # 干跑不评分（评分是有副作用的落库动作）、不扣配额。
        if hr_tools.risk_check():
            return {"allowed": False, "reason": f"风控熔断中（{hr_tools.risk_reason()}）"}
        if hr_tools.has_out_greet(session, jc):
            return {"allowed": False, "reason": "这个人已经打过招呼了（系统账本）"}
        reason = "未评分（干跑不评分）" if jc.status == "new" else ""
        return {"allowed": True, "reason": reason, "dry_run": True}

    ok, why = hr_tools.may_greet(session, jc)
    return {"allowed": ok, "reason": why}


class AttachmentIn(BaseModel):
    boss_name: str
    job_title: str = ""
    filename: str
    #: PDF 的 base64（采数端从 UC 缓存 pull 出来再编码）
    content_b64: str


@router.post("/api/ingest/attachment", dependencies=[Depends(_token_header)])
def ingest_attachment(item: AttachmentIn, session: SessionDep) -> dict[str, Any]:
    """采数端推回一份附件简历。

    ★ BOSS 的简历是**候选人主动提出、我们同意后**才送达的（聊天页卡片
      「对方已投递简历」）。采数端走「点卡片预览 → 分享到 UC → pull 缓存」
      拿到真 PDF，这里负责：落 MinIO、写 `minio_object_key` /
      `resume_downloaded_at`、状态推进到 `resume_received`、
      并记一条 `interaction(attachment)`。

    幂等：jc 已有 object_key 就直接返回，不重复上传。
    """
    try:
        raw = base64.b64decode(item.content_b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"content_b64 解不开：{exc}") from exc
    if not raw.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="不是 PDF（魔数不符）")

    jc = session.scalar(
        select(models.JobCandidate)
        .join(models.Candidate, models.Candidate.id == models.JobCandidate.candidate_id)
        .join(models.Job, models.Job.id == models.JobCandidate.job_id)
        .where(models.Candidate.name == item.boss_name)
        .where(models.Job.title == item.job_title)
    )
    if jc is None:
        raise HTTPException(status_code=404, detail="找不到候选人/职位的档案")
    if jc.minio_object_key:
        return {"skipped": True, "object_key": jc.minio_object_key}

    key = f"resumes/{jc.job_id}/{jc.candidate_id}/{item.filename}"
    try:
        storage.put_object(key, raw)
    except Exception as exc:  # noqa: BLE001
        # 盲 500 变可读 502：MinIO 凭证/网络问题一眼可见（detail 会进执行端日志）。
        raise HTTPException(
            status_code=502,
            detail=f"对象存储不可用（上传失败）：{exc.__class__.__name__}: {exc}",
        ) from exc
    jc.minio_object_key = key

    ctx = TransitionContext(source="inbound") if jc.status == "new" else None
    try:
        hr_tools.apply_transition(session, jc, StateEvent.RECEIVE_RESUME, ctx)
    except InvalidTransition:
        # 当前状态不在「可收」的起点上（例如已进终态）：只落简历，不改状态
        jc.resume_downloaded_at = jc.resume_downloaded_at or datetime.now()
        session.add(jc)

    session.add(models.Interaction(
        job_candidate_id=jc.id,
        direction="in",
        msg_type="attachment",
        content=f"附件简历 {item.filename}（{len(raw)} 字节）",
    ))
    session.commit()
    return {"object_key": key, "size": len(raw), "status": jc.status}


@router.post("/api/ingest/daily-sweep", dependencies=[Depends(_token_header)])
def daily_sweep(session: SessionDep) -> dict[str, int]:
    """日常轮的服务端收尾：**72h 无人回应关闭 + 延迟补评分**。

    ★ 72h 关闭照猎聘的 sweep：求过简历（`resume_requested_at`）超 72h 还没收到
      → `no_response` → `closed`。锚点由 `apply_transition` 的 REQUEST_RESUME 打。

    ★ 延迟补评分：`new` 且（summary/description 非空）且还没分 → 逐个 `screen_candidate`。
      单次上限 20 条防 LLM 跑太久；漏下的下一轮再补（幂等，只评没分过的）。
    """
    # — 72h 无人回应 → 关闭 —
    cutoff = datetime.now() - timedelta(hours=72)
    stale = list(session.scalars(
        select(models.JobCandidate)
        .where(models.JobCandidate.status == "awaiting_resume")
        .where(models.JobCandidate.resume_requested_at.is_not(None))
        .where(models.JobCandidate.resume_requested_at < cutoff)
    ))
    closed = 0
    for jc in stale:
        try:
            hr_tools.apply_transition(session, jc, StateEvent.NO_RESPONSE)
            hr_tools.apply_transition(session, jc, StateEvent.CLOSE)
            closed += 1
        except InvalidTransition:
            pass

    # — 延迟补评分 —
    scored = 0
    pending = list(session.scalars(
        select(models.JobCandidate)
        .join(models.Candidate, models.Candidate.id == models.JobCandidate.candidate_id)
        .where(models.JobCandidate.status == "new")
        .where(models.JobCandidate.match_score.is_(None))
        .where((models.Candidate.summary != "") | models.Candidate.description.is_not(None))
        .limit(20)
    ))
    for jc in pending:
        if hr_tools.screen_candidate(session, jc) is not None:
            scored += 1
    return {"closed": closed, "scored": scored}
