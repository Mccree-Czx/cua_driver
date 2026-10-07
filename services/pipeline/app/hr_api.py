"""HR 面 API（M3 最小可用 + M4 可观测最小；2026-10-07）：/api/hr/*——本机使用无鉴权。

- 只读：overview / candidates / 两级详情（预签名 ≤15min）/ daily / manual-queue / alerts
- 动作：review 复核推翻（OVERRIDE + ReviewOverride 流水）、rerun 手动重跑（任务派发）、
  manual-queue ack 注记、PATCH jobs（阈值回流最小形态）
- 依赖复用：get_session / get_store / get_queue / get_login_state；任务派发走
  TaskQueue.enqueue（派发登记 + arq 入队，与内部面同源）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

import redis as redis_lib
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.deps import get_login_state, get_queue, get_session, get_store
from app.state_machine import InvalidTransition, StateEvent, TransitionContext, transition
from hr_workbuddy import AtomicTask, AtomicTaskType, CandidateStatus

router = APIRouter(prefix="/api/hr", tags=["hr"])

PRESIGN_MINUTES = 15  # spec §6：预签名 URL ≤15 分钟
DAILY_MAX_DAYS = 90
MANUAL_QUEUE_MAX = 200
REVIEW_FROM = {CandidateStatus.RESUME_RECEIVED.value, CandidateStatus.HR_REVIEWED.value}


def _today_start() -> datetime:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def _presign(store: Any, key: str | None) -> str | None:
    """对象键 → 预签名 URL（≤15min）；键缺失或对象异常返回 None（不阻塞详情）。"""
    if not key:
        return None
    try:
        return store.presigned_get_url(
            store.bucket, key, expires=timedelta(minutes=PRESIGN_MINUTES)
        )
    except Exception:  # noqa: BLE001  # 预签名失败不阻塞详情
        return None


# —— 只读端点 ——


@router.get("/overview")
def hr_overview(
    job_id: int | None = None, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """漏斗概览：状态计数 / 评分分布 / 今日触达与回传。"""
    status_stmt = select(models.JobCandidate.status, func.count()).group_by(
        models.JobCandidate.status
    )
    score_stmt = select(models.JobCandidate.match_score)
    if job_id is not None:
        status_stmt = status_stmt.where(models.JobCandidate.job_id == job_id)
        score_stmt = score_stmt.where(models.JobCandidate.job_id == job_id)
    status_counts = {s: n for s, n in session.execute(status_stmt).all()}
    scores = [s for (s,) in session.execute(score_stmt).all() if s is not None]
    buckets = {"<40": 0, "40-59": 0, "60-69": 0, ">=70": 0, "未评分": 0}
    for score in scores:
        if score < 40:
            buckets["<40"] += 1
        elif score < 60:
            buckets["40-59"] += 1
        elif score < 70:
            buckets["60-69"] += 1
        else:
            buckets[">=70"] += 1
    buckets["未评分"] = sum(status_counts.values()) - len(scores)
    start = _today_start()
    touches_stmt = (
        select(func.count())
        .select_from(models.Interaction)
        .join(models.JobCandidate, models.JobCandidate.id == models.Interaction.job_candidate_id)
        .where(models.Interaction.direction == "out", models.Interaction.sent_at >= start)
    )
    received_stmt = (
        select(func.count())
        .select_from(models.Interaction)
        .join(models.JobCandidate, models.JobCandidate.id == models.Interaction.job_candidate_id)
        .where(
            models.Interaction.direction == "in",
            models.Interaction.msg_type == "attachment",
            models.Interaction.sent_at >= start,
        )
    )
    if job_id is not None:
        touches_stmt = touches_stmt.where(models.JobCandidate.job_id == job_id)
        received_stmt = received_stmt.where(models.JobCandidate.job_id == job_id)
    touches = session.scalar(touches_stmt)
    received = session.scalar(received_stmt)
    # manual：task_logs 无 job 关联，保持全局口径（告警参考）
    manual = session.scalar(
        select(func.count())
        .select_from(models.TaskLog)
        .where(models.TaskLog.outcome == "failed_needs_manual", models.TaskLog.created_at >= start)
    )
    job = session.get(models.Job, job_id) if job_id is not None else None
    return {
        "job": {"id": job.id, "title": job.title, "llm_threshold": job.llm_threshold} if job else None,
        "status_counts": status_counts,
        "score_buckets": buckets,
        "today": {"touches_out": touches or 0, "received": received or 0, "manual": manual or 0},
    }


@router.get("/candidates")
def hr_candidates(
    job_id: int | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """候选人列表（筛选 + 分页；新到在前）。"""
    stmt = (
        select(models.JobCandidate, models.Candidate)
        .join(models.Candidate, models.Candidate.id == models.JobCandidate.candidate_id)
        .order_by(models.JobCandidate.id.desc())
    )
    count_stmt = select(func.count()).select_from(models.JobCandidate).join(
        models.Candidate, models.Candidate.id == models.JobCandidate.candidate_id
    )
    if job_id is not None:
        stmt = stmt.where(models.JobCandidate.job_id == job_id)
        count_stmt = count_stmt.where(models.JobCandidate.job_id == job_id)
    if status:
        stmt = stmt.where(models.JobCandidate.status == status)
        count_stmt = count_stmt.where(models.JobCandidate.status == status)
    if q:
        like = f"%{q}%"
        condition = models.Candidate.name.like(like) | models.Candidate.liepin_user_id.like(like)
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    total = session.scalar(count_stmt) or 0
    rows = session.execute(stmt.limit(limit).offset(offset)).all()
    items = [
        {
            "jc_id": jc.id,
            "job_id": jc.job_id,
            "name": candidate.name,
            "liepin_user_id": candidate.liepin_user_id,
            "source": candidate.source,
            "status": jc.status,
            "match_score": jc.match_score,
            "judge_reason": jc.judge_reason,
            "has_snapshot": bool(candidate.snapshot_object_key),
            "has_pdf": bool(jc.minio_object_key),
            "last_touch_at": jc.last_touch_at.isoformat() if jc.last_touch_at else None,
        }
        for jc, candidate in rows
    ]
    return {"total": total, "items": items}


@router.get("/candidates/{jc_id}")
def hr_candidate_detail(
    jc_id: int,
    session: Session = Depends(get_session),
    store: Any = Depends(get_store),
) -> dict[str, Any]:
    """两级详情：初筛（截图预签名 + 7 字段 + 评分理由）+ 二筛（PDF 预签名）+ 互动时间线。"""
    jc = session.get(models.JobCandidate, jc_id)
    if jc is None:
        raise HTTPException(status_code=404, detail=f"job_candidate {jc_id} 不存在")
    candidate = session.get(models.Candidate, jc.candidate_id)
    job = session.get(models.Job, jc.job_id)
    interactions = (
        session.execute(
            select(models.Interaction)
            .where(models.Interaction.job_candidate_id == jc.id)
            .order_by(models.Interaction.id)
        )
        .scalars()
        .all()
    )
    return {
        "job": {"id": job.id, "title": job.title} if job else None,
        "jc": {
            "id": jc.id,
            "status": jc.status,
            "match_score": jc.match_score,
            "judge_reason": jc.judge_reason,
            "resume_requested_at": jc.resume_requested_at.isoformat() if jc.resume_requested_at else None,
            "resume_downloaded_at": jc.resume_downloaded_at.isoformat() if jc.resume_downloaded_at else None,
            "last_touch_at": jc.last_touch_at.isoformat() if jc.last_touch_at else None,
        },
        "candidate": {
            "name": candidate.name if candidate else None,
            "liepin_user_id": candidate.liepin_user_id if candidate else None,
            "online_resume_minimal": candidate.online_resume_minimal if candidate else None,
        },
        "snapshot_url": _presign(store, candidate.snapshot_object_key if candidate else None),
        "resume_url": _presign(store, jc.minio_object_key),
        "interactions": [
            {
                "direction": row.direction,
                "msg_type": row.msg_type,
                "content": row.content,
                "sent_at": row.sent_at.isoformat() if row.sent_at else None,
            }
            for row in interactions
        ],
    }


@router.get("/daily")
def hr_daily(
    days: int = Query(14, ge=1, le=DAILY_MAX_DAYS),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """日报：逐日漏斗（新增/触达分型/回传/转化率）+ token 成本 + 转人工数。"""
    since = _today_start() - timedelta(days=days - 1)
    new_jc = {
        str(d): n
        for d, n in session.execute(
            select(func.date(models.JobCandidate.created_at), func.count())
            .where(models.JobCandidate.created_at >= since)
            .group_by(func.date(models.JobCandidate.created_at))
        ).all()
    }
    out_rows = session.execute(
        select(func.date(models.Interaction.sent_at), models.Interaction.msg_type, func.count())
        .where(models.Interaction.direction == "out", models.Interaction.sent_at >= since)
        .group_by(func.date(models.Interaction.sent_at), models.Interaction.msg_type)
    ).all()
    received = {
        str(d): n
        for d, n in session.execute(
            select(func.date(models.Interaction.sent_at), func.count())
            .where(
                models.Interaction.direction == "in",
                models.Interaction.msg_type == "attachment",
                models.Interaction.sent_at >= since,
            )
            .group_by(func.date(models.Interaction.sent_at))
        ).all()
    }
    costs = {
        str(d): (int(t or 0), float(c or 0.0))
        for d, t, c in session.execute(
            select(
                func.date(models.TaskLog.created_at),
                func.sum(models.TaskLog.tokens),
                func.sum(models.TaskLog.cost),
            )
            .where(models.TaskLog.created_at >= since)
            .group_by(func.date(models.TaskLog.created_at))
        ).all()
    }
    manual = {
        str(d): n
        for d, n in session.execute(
            select(func.date(models.TaskLog.created_at), func.count())
            .where(
                models.TaskLog.outcome == "failed_needs_manual",
                models.TaskLog.created_at >= since,
            )
            .group_by(func.date(models.TaskLog.created_at))
        ).all()
    }
    day_map: dict[str, dict[str, int]] = {}
    for d, msg_type, n in out_rows:
        day = day_map.setdefault(str(d), {"direct_request": 0, "greet_request": 0, "reply": 0})
        if msg_type in day:
            day[msg_type] = n
    all_days = sorted(set(new_jc) | set(received) | set(costs) | set(day_map) | set(manual))
    totals = {"new_jc": 0, "requests": 0, "received": 0, "tokens": 0, "cost": 0.0, "manual": 0}
    days_out = []
    for d in all_days:
        day = day_map.get(d, {"direct_request": 0, "greet_request": 0, "reply": 0})
        requests = day["direct_request"] + day["greet_request"]
        recv = received.get(d, 0)
        tokens, cost = costs.get(d, (0, 0.0))
        days_out.append(
            {
                "date": d,
                "new_jc": new_jc.get(d, 0),
                "direct_request": day["direct_request"],
                "greet_request": day["greet_request"],
                "reply": day["reply"],
                "received": recv,
                "conversion": round(recv / requests * 100, 1) if requests else None,
                "tokens": tokens,
                "cost": round(cost, 6),
                "manual": manual.get(d, 0),
            }
        )
        totals["new_jc"] += new_jc.get(d, 0)
        totals["requests"] += requests
        totals["received"] += recv
        totals["tokens"] += tokens
        totals["cost"] = round(totals["cost"] + cost, 6)
        totals["manual"] += manual.get(d, 0)
    totals["conversion"] = (
        round(totals["received"] / totals["requests"] * 100, 1) if totals["requests"] else None
    )
    return {"days": days_out, "totals": totals}


# —— M4：人工队列 / 告警 ——


@router.get("/manual-queue")
def hr_manual_queue(
    limit: int = Query(50, ge=1, le=MANUAL_QUEUE_MAX),
    session: Session = Depends(get_session),
    queue: Any = Depends(get_queue),
) -> dict[str, Any]:
    """转人工队列：failed_needs_manual 落账 + 派发登记反查上下文（72h 内）。"""
    total = session.scalar(
        select(func.count())
        .select_from(models.TaskLog)
        .where(models.TaskLog.outcome == "failed_needs_manual")
    )
    rows = (
        session.execute(
            select(models.TaskLog)
            .where(models.TaskLog.outcome == "failed_needs_manual")
            .order_by(models.TaskLog.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    items = []
    for log in rows:
        dispatched = None
        try:
            dispatched = queue.get_dispatched(UUID(log.task_id))
        except Exception:  # noqa: BLE001  # 登记过期/非本机派发：按缺失处理
            dispatched = None
        name = status = None
        if dispatched is not None and dispatched.job_candidate_id is not None:
            jc = session.get(models.JobCandidate, dispatched.job_candidate_id)
            if jc is not None:
                status = jc.status
                candidate = session.get(models.Candidate, jc.candidate_id)
                name = candidate.name if candidate else None
        items.append(
            {
                "task_id": log.task_id,
                "task_type": dispatched.type.value if dispatched else None,
                "job_candidate_id": dispatched.job_candidate_id if dispatched else None,
                "candidate_name": name,
                "jc_status": status,
                "attempt": log.attempt,
                "note": log.note,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
        )
    return {"total": total or 0, "items": items}


class AckRequest(BaseModel):
    note: str = "人工已处理"


@router.post("/manual-queue/{task_id}/ack")
def hr_manual_ack(
    task_id: str, body: AckRequest, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """人工队列忽略注记（追加 TaskLog.note，保留原注记）。"""
    log = session.execute(
        select(models.TaskLog)
        .where(models.TaskLog.task_id == task_id, models.TaskLog.outcome == "failed_needs_manual")
        .order_by(models.TaskLog.id.desc())
    ).scalars().first()
    if log is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 无待处理落账")
    suffix = f"{body.note}(acked)"
    log.note = f"{log.note}｜{suffix}"[:255] if log.note else suffix[:255]
    session.commit()
    return {"ok": True}


@router.get("/alerts")
def hr_alerts(
    session: Session = Depends(get_session),
    login_state: Any = Depends(get_login_state),
) -> dict[str, Any]:
    """告警可观测（最小）：登录态 / 全局限流标志 / 今日配额用量 / 今日转人工。"""
    try:
        login = login_state.get()
    except Exception:  # noqa: BLE001  # 登录态键缺失按未知处理
        login = None
    risk_paused = False
    try:
        client = redis_lib.Redis.from_url(get_settings().redis_url, decode_responses=True)
        try:
            risk_paused = client.get("cua:risk:paused") is not None
        finally:
            client.close()
    except Exception:  # noqa: BLE001
        risk_paused = False
    start = _today_start()
    touches = session.scalar(
        select(func.count())
        .select_from(models.Interaction)
        .where(models.Interaction.direction == "out", models.Interaction.sent_at >= start)
    )
    manual = session.scalar(
        select(func.count())
        .select_from(models.TaskLog)
        .where(models.TaskLog.outcome == "failed_needs_manual", models.TaskLog.created_at >= start)
    )
    return {
        "login": login,
        "risk_paused": risk_paused,
        "quota_used_today": touches or 0,
        "manual_today": manual or 0,
    }


# —— 动作端点 ——


class ReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    note: str = ""
    operator: str = "hr"


@router.post("/candidates/{jc_id}/review")
def hr_review(
    jc_id: int, body: ReviewRequest, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """复核推翻：approve→hr_reviewed / reject→closed（OVERRIDE + 流水落账）。"""
    jc = session.get(models.JobCandidate, jc_id)
    if jc is None:
        raise HTTPException(status_code=404, detail=f"job_candidate {jc_id} 不存在")
    if jc.status not in REVIEW_FROM:
        raise HTTPException(
            status_code=409, detail=f"当前状态 {jc.status} 不支持复核（仅 {sorted(REVIEW_FROM)}）"
        )
    target = CandidateStatus.HR_REVIEWED if body.decision == "approve" else CandidateStatus.CLOSED
    old_status = jc.status
    try:
        jc.status = transition(
            jc, StateEvent.OVERRIDE, TransitionContext(target_status=target)
        ).status
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.add(
        models.ReviewOverride(
            job_candidate_id=jc.id,
            old_status=old_status,
            new_status=jc.status,
            operator=body.operator or "hr",
            reason=body.note or f"人工复核：{body.decision}",
        )
    )
    session.commit()
    return {"ok": True, "status": jc.status}


RERUN_TASK_MAP = {
    CandidateStatus.NEW.value: AtomicTaskType.READ_RESUME,
    CandidateStatus.SCREENED_PASS.value: AtomicTaskType.CHECK_ATTACHMENT,
    CandidateStatus.RESUME_REQUESTED.value: AtomicTaskType.CHECK_ATTACHMENT,
    CandidateStatus.AWAITING_RESUME.value: AtomicTaskType.CHECK_ATTACHMENT,
}


@router.post("/candidates/{jc_id}/rerun")
def hr_rerun(
    jc_id: int,
    session: Session = Depends(get_session),
    queue: Any = Depends(get_queue),
) -> dict[str, Any]:
    """手动重跑：按状态派发对应任务（new→读简历；请求/等待→附件探测）。"""
    jc = session.get(models.JobCandidate, jc_id)
    if jc is None:
        raise HTTPException(status_code=404, detail=f"job_candidate {jc_id} 不存在")
    candidate = session.get(models.Candidate, jc.candidate_id)
    task_type = RERUN_TASK_MAP.get(jc.status)
    if task_type is None or candidate is None:
        raise HTTPException(status_code=409, detail=f"状态 {jc.status} 不支持手动重跑")
    task = AtomicTask(
        task_id=uuid4(),
        type=task_type,
        job_id=jc.job_id,
        job_candidate_id=jc.id,
        candidate_liepin_id=candidate.liepin_user_id,
        context={},
    )
    queue.enqueue(task)
    return {"ok": True, "task_id": str(task.task_id), "type": task_type.value}


class JobPatch(BaseModel):
    llm_threshold: int | None = None


@router.patch("/jobs/{job_id}")
def hr_patch_job(
    job_id: int, body: JobPatch, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """阈值回流（最小形态）：调整 LLM 阈值（0-100）。"""
    job = session.get(models.Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"岗位 {job_id} 不存在")
    if body.llm_threshold is not None:
        if not 0 <= body.llm_threshold <= 100:
            raise HTTPException(status_code=422, detail="llm_threshold 须在 0-100")
        job.llm_threshold = body.llm_threshold
    session.commit()
    return {"ok": True, "id": job.id, "llm_threshold": job.llm_threshold}
