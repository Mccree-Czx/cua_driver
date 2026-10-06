"""pipeline 内部回调端点（D1/R3/R9/R10）：任务结果、artifact、巡检、登录态、配额、awaiting。

调用方：cua-agent worker（tasks/{id}/result|artifact）、scheduler（sweeps /
state/login / quota/today / state/awaiting 端点）。属内部接口，非 M3 API 面。
HTTP 薄壳：编排逻辑在 orchestrator，提交/回滚在此层。

R10：result 回调按 (task_id, attempt) 去重落 TaskLog（task_id/outcome/attempt/
evidence 的 brain_tokens/cost_est/duration_s）——arq 重试每次真实调用视觉
API，逐 attempt 落账不丢重试账目；handler 已自落账（一人一消息 violation
的 failed_needs_manual）则跳过，避免同 (task_id, attempt) 两行。
"""

from datetime import datetime, time
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.deps import get_login_state, get_queue, get_screening, get_session, get_store
from app.login_state import LoginStateStore
from app.models import CandidateStatus, Interaction, JobCandidate, TaskLog
from app.orchestrator import (
    InvalidEvidenceError,
    MissingEntityError,
    UnknownTaskError,
    close_stale_awaiting,
    handle_artifact,
    handle_task_result,
    reenqueue_deferred,
)
from hr_workbuddy import TaskResult

router = APIRouter(prefix="/internal", tags=["internal"])


def _http_error(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def _log_task_result(session: Session, queue, result: TaskResult) -> None:
    """R10 通用落账：按 (task_id, attempt) 去重、每次 result 一行 TaskLog。

    arq 重试每次真实调用视觉 API，但 payload 不重写——worker 把折算后的
    attempt（task.attempt + job_try - 1）与耗时 duration_s 放进 evidence，
    pipeline 据此逐 attempt 落账（不丢重试账目）；同 (task_id, attempt)
    重复回调（at-least-once 重放）仍一行。evidence 无 attempt 时默认 0
    （旧 payload / 测试直投），与一人一消息违规 handler 的自落账一致。
    """
    evidence = result.evidence
    attempt = int(evidence.get("attempt") or 0)
    existing = session.execute(
        select(TaskLog).where(
            TaskLog.task_id == str(result.task_id), TaskLog.attempt == attempt
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    dispatched = queue.get_dispatched(result.task_id)
    if dispatched is None:
        return  # handle_task_result 已通过（必有登记），纯防御
    session.add(
        TaskLog(
            task_id=str(result.task_id),
            outcome=result.outcome,
            attempt=attempt,
            tokens=int(evidence.get("brain_tokens") or 0),
            cost=float(evidence.get("cost_est") or 0),
            duration=float(evidence.get("duration_s") or 0),
        )
    )


@router.post("/tasks/{task_id}/result")
def post_task_result(
    task_id: UUID,
    result: TaskResult,
    session: Session = Depends(get_session),
    queue=Depends(get_queue),
    screening=Depends(get_screening),
    login_state: LoginStateStore = Depends(get_login_state),
):
    """D1：cua-agent 任务结果回调。pipeline 推进状态机、落账（R10）并在提交成功后入队后继任务。"""
    if result.task_id != task_id:
        raise _http_error(422, "TaskResult.task_id 与路径 task_id 不一致")
    try:
        pending = handle_task_result(
            session, result, queue=queue, screening=screening, login_state=login_state
        )
    except UnknownTaskError as exc:
        raise _http_error(404, str(exc)) from exc
    except InvalidEvidenceError as exc:
        raise _http_error(422, str(exc)) from exc
    except MissingEntityError as exc:
        raise _http_error(404, str(exc)) from exc
    _log_task_result(session, queue, result)
    session.commit()
    # 入队严格在 commit 之后：commit 失败回滚时队列零残留（一人一消息不变式）
    for task in pending:
        queue.enqueue(task)
    return {"ok": True}


@router.post("/tasks/{task_id}/artifact")
def post_task_artifact(
    task_id: UUID,
    file: UploadFile,
    kind: str = Form(...),
    filename: str = Form(...),
    form_task_id: UUID = Form(..., alias="task_id"),
    session: Session = Depends(get_session),
    queue=Depends(get_queue),
    store=Depends(get_store),
    screening=Depends(get_screening),
):
    """D1：cua-agent artifact 回调（multipart）。kind=snapshot 归档截图、kind=resume 归档附件。"""
    if form_task_id != task_id:
        raise _http_error(422, "表单 task_id 与路径 task_id 不一致")
    try:
        key = handle_artifact(
            session,
            task_id,
            kind,
            filename,
            file.file.read(),
            queue=queue,
            store=store,
            screening=screening,
        )
    except UnknownTaskError as exc:
        raise _http_error(404, str(exc)) from exc
    except InvalidEvidenceError as exc:
        raise _http_error(422, str(exc)) from exc
    except MissingEntityError as exc:
        raise _http_error(404, str(exc)) from exc
    session.commit()
    return {"ok": True, "object_key": key}


@router.post("/sweeps/stale-awaiting")
def post_stale_awaiting_sweep(session: Session = Depends(get_session)):
    """R3：72h 巡检。awaiting_resume 超 72h → no_response→closed，零追发。"""
    closed = close_stale_awaiting(session)
    session.commit()
    return {"closed": closed}


@router.post("/sweeps/deferred")
def post_deferred_sweep(
    session: Session = Depends(get_session),
    queue=Depends(get_queue),
    screening=Depends(get_screening),
):
    """延期重判巡检：对 deferred 的 new 候选人用已存最小字段重调 screening。"""
    try:
        judged, pending = reenqueue_deferred(session, screening=screening)
    except InvalidEvidenceError as exc:
        raise _http_error(422, str(exc)) from exc
    except MissingEntityError as exc:
        raise _http_error(404, str(exc)) from exc
    session.commit()
    for task in pending:
        queue.enqueue(task)
    return {"judged": judged}


# —— scheduler 数据面（R9 / 配额 / awaiting）——


@router.get("/state/login")
def get_login_state(login_state: LoginStateStore = Depends(get_login_state)):
    """R9：最近一次 CHECK_LOGIN 结果（scheduler login_health_round 轮询）。
    从未检查 → 三字段 null（scheduler 视为未知，不动暂停）。"""
    state = login_state.get()
    if state is None:
        return {"is_login": None, "checked_at": None, "task_id": None}
    return state


@router.get("/quota/today")
def get_quota_today(session: Session = Depends(get_session)):
    """当日 out 消息计数（scheduler daily_quota_reconcile 对账 vs 日上限）。"""
    start_of_today = datetime.combine(datetime.now().date(), time.min)
    count = session.execute(
        select(func.count())
        .select_from(Interaction)
        .where(Interaction.direction == "out", Interaction.sent_at >= start_of_today)
    ).scalar_one()
    return {"count": count, "date": datetime.now().date().isoformat()}


@router.get("/state/awaiting")
def get_awaiting(session: Session = Depends(get_session)):
    """awaiting_resume 的 jc 列表（scheduler awaiting_resume_sweep 入队 CHECK_ATTACHMENT）。"""
    rows = session.execute(
        select(JobCandidate).where(
            JobCandidate.status == CandidateStatus.AWAITING_RESUME.value
        )
    ).scalars().all()
    return [
        {
            "id": jc.id,
            "job_id": jc.job_id,
            "candidate_liepin_id": jc.candidate.liepin_user_id,
        }
        for jc in rows
    ]
