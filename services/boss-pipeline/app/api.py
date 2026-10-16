"""boss-pipeline 的 HTTP 接口。

分两半：

- **只读**（`/api/*`）：给前端看。
- **写入**（`/api/ingest/*`）：给 Mac 上的 `agent_boss` 采数端推送用，带令牌。

★ 这里**没有账号/角色体系**——那是「统一身份」（飞书登录 + admin/user）那条线的事，
  还没开始。现在跑在本机/内网上，别把它暴露到公网。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.deps import get_session, require_ingest_token

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


# ——— 只读 ———

@router.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/overview")
def overview(session: SessionDep) -> dict[str, Any]:
    """驾驶舱要的那几个数。"""
    def count(model: Any, *where: Any) -> int:
        stmt = select(func.count()).select_from(model)
        for clause in where:
            stmt = stmt.where(clause)
        return session.scalar(stmt) or 0

    return {
        "jobs": {
            "total": count(models.Job),
            "开放中": count(models.Job, models.Job.state == "开放中"),
            "待开放": count(models.Job, models.Job.state == "待开放"),
            # ★ 有 JD 且**确认读全**的才算可打分。半截 JD 打出来的分是错的。
            "jd_ready": count(models.Job, models.Job.jd_complete.is_(True)),
        },
        "candidates": count(models.Candidate),
        "conversations": {
            "total": count(models.Conversation),
            "unread": count(models.Conversation, models.Conversation.unread > 0),
        },
        "messages": {
            "total": count(models.Message),
            "in": count(models.Message, models.Message.direction == "in"),
            "out": count(models.Message, models.Message.direction == "out"),
        },
        "greeted": count(models.GreetLog),
    }


@router.get("/api/jobs")
def list_jobs(
    session: SessionDep,
    state: str | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    stmt = select(models.Job)
    if state:
        stmt = stmt.where(models.Job.state == state)
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
    return _job_out(job, with_jd=True)


@router.get("/api/candidates")
def list_candidates(
    session: SessionDep,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    stmt = select(models.Candidate)
    if q:
        stmt = stmt.where(
            models.Candidate.name.contains(q)
            | models.Candidate.job_and_company.contains(q)
        )
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(
        stmt.order_by(models.Candidate.last_seen_at.desc()).limit(limit).offset(offset)
    ).all()
    return {"total": total, "items": [_candidate_out(r) for r in rows]}


@router.get("/api/candidates/{candidate_id}")
def get_candidate(candidate_id: int, session: SessionDep) -> dict[str, Any]:
    row = session.get(models.Candidate, candidate_id)
    if row is None:
        raise HTTPException(404, "没有这个候选人")
    out = _candidate_out(row)
    out["conversations"] = [
        {"id": c.id, "job_title": c.job_title, "last_msg": c.last_msg,
         "status": c.status, "boss_time": c.boss_time}
        for c in row.conversations
    ]
    return out


@router.get("/api/conversations")
def list_conversations(
    session: SessionDep,
    q: str | None = None,
    unread_only: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    stmt = select(models.Conversation)
    if q:
        stmt = stmt.where(models.Conversation.boss_name.contains(q))
    if unread_only:
        stmt = stmt.where(models.Conversation.unread > 0)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(
        stmt.order_by(models.Conversation.last_synced_at.desc()).limit(limit).offset(offset)
    ).all()
    return {"total": total, "items": [_conversation_out(r) for r in rows]}


@router.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: int, session: SessionDep) -> dict[str, Any]:
    row = session.get(models.Conversation, conversation_id)
    if row is None:
        raise HTTPException(404, "没有这个会话")
    out = _conversation_out(row)
    out["messages"] = [
        {"id": m.id, "direction": m.direction, "text": m.text,
         "first_seen_at": m.first_seen_at.isoformat()}
        # ★ 按 boss_y 排：BOSS 不给消息时间戳，屏幕顺序是唯一可得的先后关系。
        for m in sorted(row.messages, key=lambda m: (m.boss_y, m.id))
    ]
    return out


def _job_out(job: models.Job, *, with_jd: bool) -> dict[str, Any]:
    out = {
        "id": job.id, "title": job.title, "state": job.state, "details": job.details,
        "jd_complete": job.jd_complete, "tags": job.tags or [],
        "jd_chars": len(job.jd_text or ""),
        "scanned_at": job.scanned_at.isoformat() if job.scanned_at else None,
    }
    if with_jd:
        out["jd_text"] = job.jd_text
    return out


def _candidate_out(row: models.Candidate) -> dict[str, Any]:
    return {
        "id": row.id, "name": row.name, "boss_key": row.boss_key,
        "job_and_company": row.job_and_company, "work_status": row.work_status,
        "work_year": row.work_year, "degree": row.degree, "age": row.age,
        "description": row.description, "summary": row.summary,
        "expect_job_city": row.expect_job_city, "expect_salary": row.expect_salary,
        "expect_industry": row.expect_industry,
        "tags": row.tags or [], "company_history": row.company_history or [],
        "active": row.active,
    }


def _conversation_out(row: models.Conversation) -> dict[str, Any]:
    return {
        "id": row.id, "candidate_id": row.candidate_id, "boss_name": row.boss_name,
        "job_title": row.job_title, "last_msg": row.last_msg, "status": row.status,
        "boss_time": row.boss_time, "unread": row.unread,
    }


# ——— 写入：采数端推上来的 ———

class JobIn(BaseModel):
    title: str
    state: str = ""
    details: str = ""
    jd_text: str | None = None
    jd_complete: bool = False
    tags: list[str] = Field(default_factory=list)


class CandidateIn(BaseModel):
    boss_key: str
    name: str
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


def _token_header(x_ingest_token: Annotated[str | None, Header()] = None) -> None:
    require_ingest_token(x_ingest_token)


@router.post("/api/ingest/jobs", dependencies=[Depends(_token_header)])
def ingest_jobs(items: list[JobIn], session: SessionDep) -> dict[str, int]:
    """采数端推职位。按 `title` upsert（BOSS 的职位名发布后不可修改）。"""
    created = updated = 0
    for item in items:
        row = session.scalar(select(models.Job).where(models.Job.title == item.title))
        if row is None:
            row = models.Job(title=item.title)
            session.add(row)
            created += 1
        else:
            updated += 1
        row.state = item.state
        row.details = item.details
        row.tags = item.tags
        # ★ JD 只在**这次读到、且读全了**的时候覆盖。
        #   否则一次失败的抓取会把好 JD 冲成半截。
        if item.jd_text and item.jd_complete:
            row.jd_text = item.jd_text
            row.jd_complete = True
    session.commit()
    return {"created": created, "updated": updated}


@router.post("/api/ingest/candidates", dependencies=[Depends(_token_header)])
def ingest_candidates(items: list[CandidateIn], session: SessionDep) -> dict[str, int]:
    """采数端推候选人。按 `boss_key` upsert。"""
    created = updated = 0
    for item in items:
        row = session.scalar(
            select(models.Candidate).where(models.Candidate.boss_key == item.boss_key)
        )
        if row is None:
            row = models.Candidate(boss_key=item.boss_key, name=item.name)
            session.add(row)
            created += 1
        else:
            updated += 1
        for field, value in item.model_dump(exclude={"boss_key"}).items():
            # 详情页才有的字段，列表页推上来是空的——别用空值盖掉已有的
            if value in (None, "", [], {}) and getattr(row, field, None):
                continue
            setattr(row, field, value)
    session.commit()
    return {"created": created, "updated": updated}


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

        existing = {(m.direction, m.text) for m in row.messages}
        for msg in item.messages:
            if (msg.direction, msg.text) in existing:
                msg_skipped += 1
                continue
            session.add(models.Message(
                conversation_id=row.id, direction=msg.direction,
                text=msg.text, boss_y=msg.boss_y,
            ))
            existing.add((msg.direction, msg.text))
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
