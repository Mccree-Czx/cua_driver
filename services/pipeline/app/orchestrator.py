"""编排器：D1 任务结果处理 + 状态推进 + 后继任务产出（spec §3 路径一 inbound 主链）。

纯逻辑、副作用经参数注入（session/screening/store/login_state/now），HTTP 薄壳在
task_results.py / api.py。M1 范围：read_resume / send_message / check_attachment /
list_unread / check_login（T10 补）结果与 snapshot / resume artifact、72h 关闭（R3）、
延期重判；outbound 路径属 M2，本模块不实现。

入队时机（评审 Important 修复）：编排器不直接入队——后继任务以返回值交给
HTTP 层，在 session.commit() 成功之后才 enqueue。入队前置会破坏一人一消息
不变式：commit 失败回滚后 worker 仍会执行 SEND_MESSAGE，而库中无任何记录，
重读时 one-message 检查形同虚设。入队后置 + 幂等 handler 是 at-least-once
安全的（commit 失败 = 无副作用；enqueue 失败 = 消息未发，可重试）。

结果回调路由：回调体（TaskResult）不含任务类型，pipeline 经
queue.get_dispatched(task_id) 取回派发时登记的 AtomicTask（类型 / job_id /
job_candidate_id / candidate_liepin_id）——见 app/task_queue.py。

worker（T9）evidence 契约（本模块为消费方，逐字段约定）：
- read_resume 成功：{"resume": MinimalResume 7 字段, "screenshot_keys": [...],
  "brain_tokens": n, "cost_est": n}
- send_message 成功：{"sent_at": str, "brain_tokens": n, "cost_est": n}
- check_attachment 成功：{"has_attachment": bool, ...}
- list_unread 成功：{"unread_ids": [liepin_user_id, ...]}（T10 对齐点）
- check_login 成功：{"logged_in": bool}（T10 对齐点）
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.login_state import LoginStateStore
from app.messaging import OneMessagePerCandidateError, ensure_no_out_message, render_message
from app.models import Candidate, Interaction, Job, JobCandidate, TaskLog
from app.state_machine import StateEvent, TransitionContext, transition
from app.storage import ObjectStore, resume_object_key, snapshot_object_key
from app.task_queue import TaskQueue
from hr_workbuddy import (
    AtomicTask,
    AtomicTaskType,
    CandidateStatus,
    MinimalResume,
    ScreenRequest,
    ScreeningResult,
    TaskResult,
)

RESUME_TIMEOUT = timedelta(hours=72)  # spec §3：发送后 72h 无附件 → no_response→closed
GREET_VARIANT = "greet_request"  # 打招呼+索要简历合并模板变体（决策 3；outbound）
DIRECT_REQUEST_VARIANT = "direct_request"  # inbound 直索要变体（2026-10-06 策略：主动咨询者免前置评分）
DEFERRED_REASON = "deferred: LLM unavailable"  # 降级判定理由（与 screening 服务一致）
DEFERRED_MARKER = "deferred"  # 延期重判扫描判据：judge_reason 含此子串

SNAPSHOT_CONTENT_TYPE = "image/png"
RESUME_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "zip": "application/zip",
}


class Screener(Protocol):
    """screening 判定依赖：真实 ScreeningClient 与测试 FakeScreening 同形状。"""

    def screen(self, request: ScreenRequest) -> ScreeningResult: ...


class UnknownTaskError(Exception):
    """task_id 无派发登记（非本 pipeline/scheduler 派发，或登记已过期）。"""


class MissingEntityError(Exception):
    """回调引用的 job / job_candidate / candidate 行不存在。"""


class InvalidEvidenceError(Exception):
    """evidence 不满足消费方契约（缺字段 / 类型非法）。"""


# —— 后继任务产出（纯函数）——


def next_tasks(
    jc: JobCandidate,
    *,
    job: Job | None = None,
    candidate: Candidate | None = None,
    has_attachment: bool = False,
) -> list[AtomicTask]:
    """按当前状态产出后继任务（M1 路径一）：

    - resume_requested（READ_RESUME 通过后）→ SEND_MESSAGE（渲染话术：inbound
      直索要 direct_request / outbound 打招呼索要 greet_request；context 带
      渲染文本 + candidate_liepin_id）
    - awaiting_resume 且 has_attachment → DOWNLOAD_ATTACHMENT
    - 其余 → []（SEND_MESSAGE 成功后等巡检；artifact 后待 M3 复核）
    """
    status = CandidateStatus(jc.status)
    if status is CandidateStatus.RESUME_REQUESTED:
        if job is None or candidate is None:
            raise ValueError("resume_requested → SEND_MESSAGE 需要 job/candidate 渲染话术")
        # 2026-10-06 策略：主动咨询者（inbound）直索要；推荐人（outbound）打招呼索要
        variant = (
            DIRECT_REQUEST_VARIANT if candidate.source == "inbound" else GREET_VARIANT
        )
        text = render_message(job, candidate, variant)
        return [
            AtomicTask(
                task_id=uuid4(),
                type=AtomicTaskType.SEND_MESSAGE,
                job_id=job.id,
                job_candidate_id=jc.id,
                candidate_liepin_id=candidate.liepin_user_id,
                context={"text": text, "candidate_liepin_id": candidate.liepin_user_id},
            )
        ]
    if status is CandidateStatus.AWAITING_RESUME and has_attachment:
        return [
            AtomicTask(
                task_id=uuid4(),
                type=AtomicTaskType.DOWNLOAD_ATTACHMENT,
                job_id=jc.job_id,
                job_candidate_id=jc.id,
                candidate_liepin_id=candidate.liepin_user_id if candidate else None,
                context={},
            )
        ]
    return []


def resume_artifact_key(
    job_id: int,
    liepin_user_id: str,
    name: str,
    job_title: str,
    date: date,
    ext: str,
) -> str:
    """resume_object_key 变体：扩展名随附件（Review Focus 1：docx/zip 等不拒绝）。"""
    base = resume_object_key(job_id, liepin_user_id, name, job_title, date)
    return f"{base[: -len('.pdf')]}.{ext.lower().lstrip('.')}"


# —— 内部辅助 ——


def _now(now: datetime | None) -> datetime:
    return now or datetime.now()


def _advance(jc: JobCandidate, event: StateEvent, ctx: TransitionContext | None = None) -> None:
    """把 detached 迁移结果的 status 写回已挂载实例（T4：持久化由编排层负责）。"""
    jc.status = transition(jc, event, ctx).status


def _get_or_create_candidate(
    session: Session, liepin_user_id: str, *, name: str | None = None
) -> Candidate:
    candidate = session.execute(
        select(Candidate).where(Candidate.liepin_user_id == liepin_user_id)
    ).scalar_one_or_none()
    if candidate is None:
        candidate = Candidate(
            liepin_user_id=liepin_user_id,
            name=name or liepin_user_id,
            online_resume_minimal={},
            source="inbound",
        )
        session.add(candidate)
        session.flush()
    return candidate


def _get_or_create_job_candidate(
    session: Session, job_id: int, candidate: Candidate
) -> tuple[JobCandidate, bool]:
    """幂等取 (job, candidate) 关系行；返回 (jc, created)。"""
    jc = session.execute(
        select(JobCandidate).where(
            JobCandidate.job_id == job_id, JobCandidate.candidate_id == candidate.id
        )
    ).scalar_one_or_none()
    if jc is None:
        jc = JobCandidate(job_id=job_id, candidate_id=candidate.id)
        jc.candidate = candidate  # 状态机路径消歧读 jc.candidate.source
        session.add(jc)
        session.flush()
        return jc, True
    return jc, False


def _apply_screening_result(
    jc: JobCandidate,
    job: Job,
    candidate: Candidate,
    sres: ScreeningResult,
) -> list[AtomicTask]:
    """spec §3 步骤 3-5 判定分支，返回待入队的后继任务（由 HTTP 层在 commit 后入队）：

    - degraded → 只存证据不推进：status 保持 new、judge_reason=deferred、不发消息
    - rejected_hard / rejected_llm → 对应状态 + judge_reason 落库，零触达
    - screened_pass → new→screened_pass→resume_requested，渲染话术产出 SEND_MESSAGE
    """
    if sres.degraded:
        jc.judge_reason = sres.judge_reason or DEFERRED_REASON
        return []
    if sres.status is CandidateStatus.REJECTED_HARD:
        _advance(jc, StateEvent.REJECT_HARD)
        jc.judge_reason = sres.judge_reason
    elif sres.status is CandidateStatus.REJECTED_LLM:
        _advance(jc, StateEvent.REJECT_LLM)
        jc.judge_reason = sres.judge_reason
        jc.match_score = sres.score
    elif sres.status is CandidateStatus.SCREENED_PASS:
        _advance(jc, StateEvent.SCREEN_PASS)
        _advance(jc, StateEvent.REQUEST_RESUME, TransitionContext(source="inbound"))
        jc.match_score = sres.score
        jc.judge_reason = sres.judge_reason
        return next_tasks(jc, job=job, candidate=candidate)
    return []


# —— 任务结果处理（按派发类型路由）——


def _handle_read_resume_result(
    session: Session,
    dispatched: AtomicTask,
    result: TaskResult,
    *,
    screening: Screener,
    now: datetime,
    login_state: LoginStateStore | None = None,
) -> list[AtomicTask]:
    if result.outcome != "success":
        return []  # 失败由 worker 重试 ≤3 / 转人工（M4）；pipeline 不动状态
    minimal = _minimal_from_evidence(result.evidence)
    job = session.get(Job, dispatched.job_id)
    if job is None:
        raise MissingEntityError(f"岗位 {dispatched.job_id} 不存在")
    candidate = _get_or_create_candidate(session, minimal.liepin_user_id, name=minimal.name)
    jc, _ = _get_or_create_job_candidate(session, job.id, candidate)
    if CandidateStatus(jc.status) is not CandidateStatus.NEW:
        return []  # Review Focus 5 幂等：jc 已有判定（status 非 new）→ 忽略重复结果

    # 最小字段落库（每次有效读取刷新快照）
    candidate.name = minimal.name
    candidate.online_resume_minimal = minimal.model_dump()

    sres = screening.screen(
        ScreenRequest(
            job_id=job.id,
            resume=minimal,
            jd_text=job.jd_text,
            hard_rules=job.hard_rules,
            threshold=job.llm_threshold,
            # 2026-10-06 策略：inbound 直索要（仅硬规则；LLM 评分后移至简历收到后）
            llm_scoring=(candidate.source != "inbound"),
        )
    )
    return _apply_screening_result(jc, job, candidate, sres)


def _handle_send_message_result(
    session: Session,
    dispatched: AtomicTask,
    result: TaskResult,
    *,
    screening: Screener,
    now: datetime,
    login_state: LoginStateStore | None = None,
) -> list[AtomicTask]:
    if result.outcome != "success":
        return []  # 失败：worker 重试 ≤3；转人工为 M4，本任务不落账
    if dispatched.job_candidate_id is None:
        raise InvalidEvidenceError("SEND_MESSAGE 任务缺 job_candidate_id")
    jc = session.get(JobCandidate, dispatched.job_candidate_id)
    if jc is None:
        raise MissingEntityError(f"job_candidate {dispatched.job_candidate_id} 不存在")
    if jc.status == CandidateStatus.AWAITING_RESUME.value:
        return []  # 重复回调重放：已推进过，幂等忽略
    if jc.status != CandidateStatus.RESUME_REQUESTED.value:
        return []  # 防御：非预期状态不动（pipeline 不会对非 resume_requested 派发发送）

    try:
        ensure_no_out_message(session, jc)
    except OneMessagePerCandidateError:
        # 一人一消息（决策 3）：转 failed_needs_manual 落 TaskLog，不推进、不落第二条消息
        # attempt 取 evidence 折算值（与通用落账的去重键 (task_id, attempt) 对齐，
        # 避免重试轮次的自落账与通用落账错位成两行）
        session.add(
            TaskLog(
                task_id=str(result.task_id),
                outcome="failed_needs_manual",
                attempt=int(result.evidence.get("attempt") or 0),
                tokens=int(result.evidence.get("brain_tokens") or 0),
                cost=float(result.evidence.get("cost_est") or 0),
            )
        )
        return []
    _advance(jc, StateEvent.AWAIT_RESUME)
    jc.resume_requested_at = now  # 72h 关闭锚点
    jc.last_touch_at = now
    candidate = session.get(Candidate, jc.candidate_id)
    if candidate is None:
        raise MissingEntityError(f"job_candidate {jc.id} 关联的 candidate 行不存在")
    session.add(
        Interaction(
            job_candidate_id=jc.id,
            direction="out",
            # 2026-10-06 策略：inbound 直索要（direct_request）/ outbound 打招呼索要
            msg_type=(
                DIRECT_REQUEST_VARIANT if candidate.source == "inbound" else GREET_VARIANT
            ),
            content=dispatched.context.get("text"),
            sent_at=now,
        )
    )
    return []


def _handle_check_attachment_result(
    session: Session,
    dispatched: AtomicTask,
    result: TaskResult,
    *,
    screening: Screener,
    now: datetime,
    login_state: LoginStateStore | None = None,
) -> list[AtomicTask]:
    if result.outcome != "success":
        return []
    if dispatched.job_candidate_id is None:
        raise InvalidEvidenceError("CHECK_ATTACHMENT 任务缺 job_candidate_id")
    jc = session.get(JobCandidate, dispatched.job_candidate_id)
    if jc is None:
        raise MissingEntityError(f"job_candidate {dispatched.job_candidate_id} 不存在")
    has_attachment = result.evidence.get("has_attachment")
    if not isinstance(has_attachment, bool):
        raise InvalidEvidenceError("CHECK_ATTACHMENT 成功结果 evidence 需 has_attachment: bool")
    if not has_attachment:
        return []  # 无附件 → 不动（等下一轮巡检）
    candidate = session.get(Candidate, jc.candidate_id)
    return next_tasks(jc, candidate=candidate, has_attachment=True)


def _handle_list_unread_result(
    session: Session,
    dispatched: AtomicTask,
    result: TaskResult,
    *,
    screening: Screener,
    now: datetime,
    login_state: LoginStateStore | None = None,
) -> list[AtomicTask]:
    """R9：对每个未读会话幂等建 Candidate（inbound）+ job_candidate（new）→ READ_RESUME。

    jc 已存在（此前轮次已建、READ_RESUME 在途或已读）→ 不重复入队——建档幂等 +
    在途自然去重；重复结果重放安全（at-least-once）。
    """
    if result.outcome != "success":
        return []  # 失败由 worker 重试 ≤3；pipeline 不动状态
    unread = result.evidence.get("unread_ids")
    if not isinstance(unread, list) or not all(isinstance(i, str) for i in unread):
        raise InvalidEvidenceError("LIST_UNREAD 成功结果 evidence 需 unread_ids: list[str]")
    pending: list[AtomicTask] = []
    for liepin_id in unread:
        candidate = _get_or_create_candidate(session, liepin_id)  # 姓名未知 → liepin_id 占位
        jc, created = _get_or_create_job_candidate(session, dispatched.job_id, candidate)
        if not created:
            continue  # 已有 jc：READ_RESUME 在途或已读 → 不重复入队
        pending.append(
            AtomicTask(
                task_id=uuid4(),
                type=AtomicTaskType.READ_RESUME,
                job_id=dispatched.job_id,
                job_candidate_id=jc.id,  # 建档后即知 jc（溯源 + 在途去重索引）
                candidate_liepin_id=liepin_id,
                context={},
            )
        )
    return pending


def _handle_check_login_result(
    session: Session,
    dispatched: AtomicTask,
    result: TaskResult,
    *,
    screening: Screener,
    now: datetime,
    login_state: LoginStateStore | None = None,
) -> list[AtomicTask]:
    """R9：CHECK_LOGIN 结果 → 登录态落 Redis（GET /internal/state/login 供 scheduler
    查询）；账目由端点层通用 TaskLog（R10）落。无状态推进、无后继任务。"""
    if result.outcome != "success":
        return []  # 失败由 worker 重试；登录态不更新（未知比错判安全）
    logged_in = result.evidence.get("logged_in")
    if not isinstance(logged_in, bool):
        raise InvalidEvidenceError("CHECK_LOGIN 成功结果 evidence 需 logged_in: bool")
    if login_state is not None:
        login_state.set(logged_in, str(result.task_id), now.isoformat())
    return []


_RESULT_HANDLERS: dict[AtomicTaskType, Callable[..., list[AtomicTask]]] = {
    AtomicTaskType.READ_RESUME: _handle_read_resume_result,
    AtomicTaskType.SEND_MESSAGE: _handle_send_message_result,
    AtomicTaskType.CHECK_ATTACHMENT: _handle_check_attachment_result,
    AtomicTaskType.LIST_UNREAD: _handle_list_unread_result,
    AtomicTaskType.CHECK_LOGIN: _handle_check_login_result,
}


def handle_task_result(
    session: Session,
    result: TaskResult,
    *,
    queue: TaskQueue,
    screening: Screener,
    login_state: LoginStateStore | None = None,
    now: datetime | None = None,
) -> list[AtomicTask]:
    """D1 结果回调入口：按派发登记的任务类型路由。

    返回待入队的后继任务；不直接入队——HTTP 层在 session.commit() 成功后
    才 enqueue（评审 Important：入队前置 + commit 失败 = 无记录的真实消息）。
    本函数不落库，提交由 HTTP 层负责。
    """
    dispatched = queue.get_dispatched(result.task_id)
    if dispatched is None:
        raise UnknownTaskError(
            f"任务 {result.task_id} 无派发登记（非本 pipeline/scheduler 派发或登记已过期）"
        )
    handler = _RESULT_HANDLERS.get(dispatched.type)
    if handler is None:
        return []  # download_attachment 等：结果本身无推进动作（artifact 驱动）
    return handler(
        session,
        dispatched,
        result,
        screening=screening,
        now=_now(now),
        login_state=login_state,
    )


# —— artifact 回调（multipart）——


def handle_artifact(
    session: Session,
    task_id: UUID,
    kind: str,
    filename: str,
    data: bytes,
    *,
    queue: TaskQueue,
    store: ObjectStore,
    screening: Screener,
    now: datetime | None = None,
) -> str | None:
    """D1 artifact 回调入口：kind=snapshot 归档在线简历截图；kind=resume 归档回传附件。"""
    dispatched = queue.get_dispatched(task_id)
    if dispatched is None:
        raise UnknownTaskError(
            f"任务 {task_id} 无派发登记（非本 pipeline/scheduler 派发或登记已过期）"
        )
    if kind == "snapshot":
        return _handle_snapshot_artifact(session, dispatched, data, store=store, now=_now(now))
    if kind == "resume":
        return _handle_resume_artifact(
            session, dispatched, filename, data, store=store, screening=screening, now=_now(now)
        )
    raise InvalidEvidenceError(f"未知 artifact kind：{kind!r}（须 snapshot|resume）")


def _handle_snapshot_artifact(
    session: Session,
    dispatched: AtomicTask,
    data: bytes,
    *,
    store: ObjectStore,
    now: datetime,
) -> str:
    if dispatched.type is not AtomicTaskType.READ_RESUME:
        raise InvalidEvidenceError(
            f"snapshot artifact 只属于 read_resume 任务（收到 {dispatched.type.value}）"
        )
    liepin_id = dispatched.candidate_liepin_id
    if not liepin_id:
        raise InvalidEvidenceError("read_resume 任务缺 candidate_liepin_id，无法归档截图")
    key = snapshot_object_key(liepin_id, now.date(), now.strftime("%H%M%S"))
    store.put_object(store.bucket, key, data, SNAPSHOT_CONTENT_TYPE)
    # result 与 artifact 两个回调无序到达：候选人可能尚未建档，先建占位行
    # （name 以 liepin_user_id 占位，result 到达后覆盖为真实姓名）
    candidate = _get_or_create_candidate(session, liepin_id)
    candidate.snapshot_object_key = key  # 重复截图保留历史，引用指向最新
    return key


def _handle_resume_artifact(
    session: Session,
    dispatched: AtomicTask,
    filename: str,
    data: bytes,
    *,
    store: ObjectStore,
    screening: Screener,
    now: datetime,
) -> str:
    if dispatched.type is not AtomicTaskType.DOWNLOAD_ATTACHMENT:
        raise InvalidEvidenceError(
            f"resume artifact 只属于 download_attachment 任务（收到 {dispatched.type.value}）"
        )
    if dispatched.job_candidate_id is None:
        raise InvalidEvidenceError("download_attachment 任务缺 job_candidate_id")
    jc = session.get(JobCandidate, dispatched.job_candidate_id)
    if jc is None:
        raise MissingEntityError(f"job_candidate {dispatched.job_candidate_id} 不存在")
    candidate = session.get(Candidate, jc.candidate_id)
    job = session.get(Job, jc.job_id)
    if candidate is None or job is None:
        raise MissingEntityError("job_candidate 关联的 candidate/job 行不存在")

    # 真实模式门禁 ②：无论 jc 状态，附件字节一律按 resumes/ 键规则落 MinIO——
    # 72h 关闭与在途 CHECK_ATTACHMENT→DOWNLOAD_ATTACHMENT 链存在竞态窗口，
    # 迟到附件是真实简历唯一副本（worker 不留存字节），绝不静默丢弃。
    ext = Path(filename).suffix.lstrip(".").lower() or "pdf"
    if ext == "pdf":
        key = store.put_resume(
            job.id, candidate.liepin_user_id, candidate.name, job.title, now.date(), data
        )
    else:
        # Review Focus 1：非 PDF 附件不拒绝，按原始扩展名归档
        key = resume_artifact_key(
            job.id, candidate.liepin_user_id, candidate.name, job.title, now.date(), ext
        )
        store.put_object(
            store.bucket,
            key,
            data,
            RESUME_CONTENT_TYPES.get(ext, "application/octet-stream"),
        )
    if jc.minio_object_key is None:
        jc.minio_object_key = key  # 空则填上；已有（先前收到的简历）保留先到者

    if jc.status != CandidateStatus.AWAITING_RESUME.value:
        # 迟到附件（jc 已 closed/no_response/resume_received 等）：只存不推进。
        # 状态推进交由人工/复核流程（M4）；落 TaskLog 注记留痕。
        session.add(
            TaskLog(
                task_id=str(dispatched.task_id),
                outcome="failed_needs_manual",
                attempt=dispatched.attempt,
                note="迟到附件，只存不推进",
            )
        )
        return key

    _advance(jc, StateEvent.RECEIVE_RESUME)
    jc.resume_downloaded_at = now
    session.add(
        Interaction(
            job_candidate_id=jc.id,
            direction="in",
            msg_type="attachment",
            content=key,
            sent_at=now,
        )
    )
    # 2026-10-06 策略：inbound 直索要流——收到简历后补 LLM 评分（前置评分已免）。
    # 仅落 match_score/judge_reason 供 HR/M3；不改状态、不阻塞入库。
    if candidate.source == "inbound" and jc.match_score is None:
        _score_received_resume(screening, jc, job, candidate)
    return key


def _score_received_resume(
    screening: Screener, jc: JobCandidate, job: Job, candidate: Candidate
) -> None:
    """收到简历后补评分（inbound 直索要流）：仅落 match_score/judge_reason。

    失败（LLM/pipeline 异常/无在线简历快照）记标记不重试——不阻塞入库，交 M3
    人工关注（标记不含 deferred 子串，不被延期重判扫描误拾）。
    """
    if not candidate.online_resume_minimal:
        jc.judge_reason = "收到后补评分跳过：无在线简历快照（M3 人工复核）"
        return
    try:
        sres = screening.screen(
            ScreenRequest(
                job_id=job.id,
                resume=MinimalResume.model_validate(candidate.online_resume_minimal),
                jd_text=job.jd_text,
                hard_rules=job.hard_rules,
                threshold=job.llm_threshold,
                llm_scoring=True,
            )
        )
        jc.match_score = sres.score
        jc.judge_reason = sres.judge_reason
    except Exception:  # noqa: BLE001 - 补评分失败不阻塞入库（M3 人工关注）
        jc.judge_reason = "收到后补评分失败（LLM/pipeline 异常；M3 人工复核）"


def _minimal_from_evidence(evidence: dict) -> MinimalResume:
    raw = evidence.get("resume")
    if not isinstance(raw, dict):
        raise InvalidEvidenceError("READ_RESUME 成功结果 evidence 需 resume 最小字段集")
    return MinimalResume.model_validate(raw)


# —— 巡检（R3 / 延期重判）——


def close_stale_awaiting(session: Session, now: datetime | None = None) -> int:
    """R3 72h 关闭：awaiting_resume 且 resume_requested_at 严格早于 now-72h
    → no_response→closed。恰好 72h 不关（Review Focus 3）；零追发（决策 3）。"""
    now = _now(now)
    cutoff = now - RESUME_TIMEOUT
    stale = session.execute(
        select(JobCandidate).where(
            JobCandidate.status == CandidateStatus.AWAITING_RESUME.value,
            JobCandidate.resume_requested_at < cutoff,
        )
    ).scalars().all()
    for jc in stale:
        _advance(jc, StateEvent.NO_RESPONSE)
        _advance(jc, StateEvent.CLOSE)
    return len(stale)


def reenqueue_deferred(
    session: Session,
    *,
    screening: Screener,
) -> tuple[int, list[AtomicTask]]:
    """延期重判：status=new 且 judge_reason 含 deferred → 用已存 online_resume_minimal
    重调 screening，结果按判定分支推进。不产生新的 read CUA 任务（不重读在线简历）；
    通过者产出的 SEND_MESSAGE 返回给 HTTP 层，在 commit 成功后入队。"""
    pending: list[AtomicTask] = []
    deferred = session.execute(
        select(JobCandidate).where(
            JobCandidate.status == CandidateStatus.NEW.value,
            JobCandidate.judge_reason.like(f"%{DEFERRED_MARKER}%"),
        )
    ).scalars().all()
    for jc in deferred:
        candidate = session.get(Candidate, jc.candidate_id)
        job = session.get(Job, jc.job_id)
        if candidate is None or job is None:
            raise MissingEntityError(f"job_candidate {jc.id} 关联的 candidate/job 行不存在")
        minimal = MinimalResume.model_validate(candidate.online_resume_minimal)
        sres = screening.screen(
            ScreenRequest(
                job_id=job.id,
                resume=minimal,
                jd_text=job.jd_text,
                hard_rules=job.hard_rules,
                threshold=job.llm_threshold,
            )
        )
        pending.extend(_apply_screening_result(jc, job, candidate, sres))
    return len(deferred), pending
