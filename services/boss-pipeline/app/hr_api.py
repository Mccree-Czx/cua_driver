"""HR 面接口（`/api/hr/*`）。

端点清单**照猎聘的 `services/pipeline/app/hr_api.py`**，只删掉账号那一路
（BOSS 没有密码、没有多账号）。

★ 这里**没有鉴权**——统一身份（飞书登录 + admin/user）是四期的事。
  现在跑在本机/内网上，别把它暴露到公网。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hr_workbuddy import CandidateStatus

from app import hr_tools, keys, models, storage
from app.deps import get_session
from app.state_machine import InvalidTransition, StateEvent, TransitionContext

router = APIRouter(prefix="/api/hr", tags=["hr"])
SessionDep = Annotated[Session, Depends(get_session)]


# ——— 驾驶舱 ———

@router.get("/overview")
def overview(session: SessionDep) -> dict[str, Any]:
    def n(model: Any, *where: Any) -> int:
        stmt = select(func.count()).select_from(model)
        for clause in where:
            stmt = stmt.where(clause)
        return session.scalar(stmt) or 0

    jc_total = n(models.JobCandidate)
    return {
        "jobs": {
            "total": n(models.Job),
            # ★ 「可用」不是「开放中」——还要 JD 读全、系统启用过。见 hr_tools.job_list_active。
            "usable": len(hr_tools.job_list_active(session)),
            "开放中": n(models.Job, models.Job.state == "开放中"),
            "jd_ready": n(models.Job, models.Job.jd_complete.is_(True)),
        },
        "candidates": n(models.Candidate),
        "jc": {
            "total": jc_total,
            "greeted": n(models.JobCandidate, models.JobCandidate.status == "greeted"),
            "resume_received": n(models.JobCandidate, models.JobCandidate.status == "resume_received"),
            "closed": n(models.JobCandidate, models.JobCandidate.status == "closed"),
        },
        #: 按状态逐个数。★ 照猎聘的 `status_counts`——它驾驶舱的「状态分布」栏
        #: 就是拿这个渲染成一排胶囊。列**全部 11 个态**（哪怕是 0），
        #: 这样前端不用自己拼、也不会因为某个态没人而少一格。
        "status_counts": {
            s.value: n(models.JobCandidate, models.JobCandidate.status == s.value)
            for s in CandidateStatus
        },
        "conversations": {
            "total": n(models.Conversation),
            "unread": n(models.Conversation, models.Conversation.unread > 0),
        },
        "messages": {
            "total": n(models.Message),
            "in": n(models.Message, models.Message.direction == "in"),
            "out": n(models.Message, models.Message.direction == "out"),
        },
        # 打过招呼的账本条数（服务端这份）。采数端也有一份，见 models.GreetLog。
        "greeted": n(models.GreetLog),
        "today": {"quota_used": hr_tools.quota_used(), "quota_cap": hr_tools.counts(session)["quota_cap"]},
    }


@router.get("/alerts")
def alerts(session: SessionDep) -> dict[str, Any]:
    """顶栏那几条胶囊。照猎聘的 `/alerts`，把它的登录/配额换成 BOSS 的等价物。

    ★ BOSS 多了一条猎聘没有的：**`login_gate_in_flight`**——
      「手机正停在滑块/短信/人脸页等人」。它必须显眼，否则人根本不知道
      系统是在等自己，只会觉得「怎么不动了」。
    """
    paused = hr_tools.risk_check()
    try:
        in_flight = bool(hr_tools.redis_client().exists(keys.login_gate_in_flight()))
    except Exception:
        in_flight = False
    manual = session.scalar(
        select(func.count()).select_from(models.TaskLog)
        .where(models.TaskLog.outcome == "failed_needs_manual")
    ) or 0
    return {
        "risk_paused": paused,
        "risk_reason": hr_tools.risk_reason() if paused else "",
        "login_gate_in_flight": in_flight,
        "quota_used_today": hr_tools.quota_used(),
        "quota_cap": hr_tools.counts(session)["quota_cap"],
        "manual_today": manual,
    }


# ——— 候选人（jc 维度）———

def _jc_out(jc: models.JobCandidate) -> dict[str, Any]:
    cand = jc.candidate
    return {
        "id": jc.id,
        "status": jc.status,
        "judge_reason": jc.judge_reason,
        "match_score": jc.match_score,
        "job_id": jc.job_id,
        "job_title": jc.job.title if jc.job else "",
        "candidate_id": jc.candidate_id,
        "name": cand.name if cand else "",
        "boss_key": cand.boss_key if cand else "",
        "job_and_company": cand.job_and_company if cand else "",
        "work_year": cand.work_year if cand else "",
        "degree": cand.degree if cand else "",
        "age": cand.age if cand else "",
        "summary": cand.summary if cand else "",
        "expect_job_city": cand.expect_job_city if cand else "",
        "expect_salary": cand.expect_salary if cand else "",
        "tags": (cand.tags or []) if cand else [],
        "last_touch_at": jc.last_touch_at.isoformat() if jc.last_touch_at else None,
        "resume_requested_at": jc.resume_requested_at.isoformat() if jc.resume_requested_at else None,
        "resume_downloaded_at": jc.resume_downloaded_at.isoformat() if jc.resume_downloaded_at else None,
        #: 附件简历的 MinIO 对象键（空 = 还没收到）。控制台据此判断要不要给「简历」链接。
        "minio_object_key": jc.minio_object_key,
    }


@router.get("/candidates")
def list_candidates(
    session: SessionDep,
    status: str | None = None,
    job_id: int | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    stmt = select(models.JobCandidate)
    if status:
        stmt = stmt.where(models.JobCandidate.status == status)
    if job_id:
        stmt = stmt.where(models.JobCandidate.job_id == job_id)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(
        stmt.order_by(models.JobCandidate.id.desc()).offset(offset).limit(limit)
    ).all()
    items = [_jc_out(r) for r in rows]
    if q:
        items = [i for i in items if q in i["name"] or q in i["job_and_company"]]
    return {"total": total, "items": items}


@router.get("/candidates/{jc_id}")
def get_candidate(jc_id: int, session: SessionDep) -> dict[str, Any]:
    jc = session.get(models.JobCandidate, jc_id)
    if jc is None:
        raise HTTPException(404, "没有这条候选人记录")
    out = _jc_out(jc)
    out["description"] = jc.candidate.description if jc.candidate else None
    out["company_history"] = (jc.candidate.company_history or []) if jc.candidate else []
    # 附件简历的预签名链接（只有收到简历才有；15 分钟有效）
    out["resume_url"] = storage.presigned_get_url(jc.minio_object_key)
    # 系统动作流水（不是聊天记录——聊天在 /conversations 那边）
    out["interactions"] = [
        {"direction": i.direction, "msg_type": i.msg_type, "content": i.content,
         "sent_at": i.sent_at.isoformat()}
        for i in sorted(jc.interactions, key=lambda i: i.id)
    ]
    out["overrides"] = [
        {"old_status": o.old_status, "new_status": o.new_status,
         "operator": o.operator, "reason": o.reason, "created_at": o.created_at.isoformat()}
        for o in sorted(
            session.scalars(
                select(models.ReviewOverride)
                .where(models.ReviewOverride.job_candidate_id == jc.id)
            ), key=lambda o: o.id,
        )
    ]
    out["conversations"] = _candidate_conversations(session, jc.candidate_id)
    return out


def _candidate_conversations(session: Session, candidate_id: int | None) -> list[dict[str, Any]]:
    """这个人的**会话**（双方实际说了什么）。

    ★ 与上面的 `interactions`（**系统动作流水**——我们对候选人做了什么）
      **分开呈现**，这正是当初不合并 `messages` / `interactions` 两张表的理由。
      猎聘只有一个「互动时间线」（是前者）；BOSS 两个都有，就该两个都给出来。

    会话是按**人**存的、jc 是按**职位×人**，所以这里可能返回多条
    （同一个人在多个职位下聊过）。
    """
    if not candidate_id:
        return []
    rows = session.scalars(
        select(models.Conversation).where(models.Conversation.candidate_id == candidate_id)
    ).all()
    return [
        {
            "id": c.id, "job_title": c.job_title, "status": c.status,
            "boss_time": c.boss_time, "unread": c.unread,
            # ★ 按 boss_y 排：BOSS 不给消息时间戳，屏幕顺序是唯一可得的先后关系。
            "messages": [
                {"direction": m.direction, "text": m.text}
                for m in sorted(c.messages, key=lambda m: (m.boss_y, m.id))
            ],
        }
        for c in rows
    ]


class ReviewIn(BaseModel):
    decision: str            # approve | reject
    note: str = ""


@router.post("/candidates/{jc_id}/review")
def review(jc_id: int, body: ReviewIn, session: SessionDep) -> dict[str, Any]:
    """人工复核。

    ★ BOSS 上没有打分链路，所以「通过/推翻」在这里的语义是**人工判定的结果**，
      不是对机器打分结果的复核。落 `review_overrides` 流水（照猎聘）。
    """
    jc = session.get(models.JobCandidate, jc_id)
    if jc is None:
        raise HTTPException(404, "没有这条候选人记录")
    if body.decision not in ("approve", "reject"):
        raise HTTPException(422, "decision 只能是 approve / reject")

    old = jc.status
    target = "hr_reviewed" if body.decision == "approve" else "closed"
    if old == target:
        raise HTTPException(409, f"已经是 {target} 了")

    try:
        hr_tools.apply_transition(
            session, jc, StateEvent.OVERRIDE,
            TransitionContext(target_status=CandidateStatus(target)),
        )
    except InvalidTransition as exc:
        raise HTTPException(409, str(exc)) from exc

    session.add(models.ReviewOverride(
        job_candidate_id=jc.id, old_status=old, new_status=target,
        operator="hr", reason=body.note,
    ))
    session.commit()
    return {"id": jc.id, "status": jc.status}


# ——— 会话（BOSS 特有，猎聘没有独立页）———

def _conv_out(c: models.Conversation) -> dict[str, Any]:
    return {
        "id": c.id, "candidate_id": c.candidate_id, "boss_name": c.boss_name,
        "job_title": c.job_title, "last_msg": c.last_msg, "status": c.status,
        "boss_time": c.boss_time, "unread": c.unread,
    }


@router.get("/conversations")
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
    return {"total": total, "items": [_conv_out(r) for r in rows]}


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: int, session: SessionDep) -> dict[str, Any]:
    row = session.get(models.Conversation, conversation_id)
    if row is None:
        raise HTTPException(404, "没有这个会话")
    out = _conv_out(row)
    # ★ 按 boss_y 排：BOSS 不给消息时间戳，屏幕顺序是唯一可得的先后关系。
    out["messages"] = [
        {"id": m.id, "direction": m.direction, "text": m.text,
         "first_seen_at": m.first_seen_at.isoformat()}
        for m in sorted(row.messages, key=lambda m: (m.boss_y, m.id))
    ]
    return out


# ——— 人工队列 ———

@router.get("/manual-queue")
def manual_queue(session: SessionDep, limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    """需要人处理的事。

    ★ **BOSS 上这个队列有真内容**（猎聘那边其实半成品）：
      掉登录（要人过滑块/短信/人脸）、采集失败、轮次超时。
    """
    rows = session.scalars(
        select(models.TaskLog)
        .where(models.TaskLog.outcome == "failed_needs_manual")
        .order_by(models.TaskLog.id.desc())
        .limit(limit)
    ).all()
    return {
        "total": len(rows),
        "items": [
            {"id": r.id, "task_id": r.task_id, "round_name": r.round_name,
             "outcome": r.outcome, "note": r.note, "created_at": r.created_at.isoformat()}
            for r in rows
        ],
    }


class AckIn(BaseModel):
    note: str = ""


@router.post("/manual-queue/{task_id}/ack")
def ack_manual(task_id: str, body: AckIn, session: SessionDep) -> dict[str, Any]:
    """确认已人工处理。**不删行**——把 outcome 改成 acked 并追加备注（留痕）。"""
    row = session.scalar(select(models.TaskLog).where(models.TaskLog.task_id == task_id))
    if row is None:
        raise HTTPException(404, "没有这条任务")
    row.note = f"{row.note}｜已人工确认(acked): {body.note}".strip("｜")
    row.outcome = "acked"
    session.commit()
    return {"task_id": task_id, "outcome": row.outcome}


# ——— 运行日志 ———

@router.get("/tasklogs")
def tasklogs(session: SessionDep, limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    rows = session.scalars(
        select(models.TaskLog).order_by(models.TaskLog.id.desc()).limit(limit)
    ).all()
    return {
        "total": len(rows),
        "items": [
            {"id": r.id, "task_id": r.task_id, "round_name": r.round_name,
             "outcome": r.outcome, "attempt": r.attempt, "tokens": r.tokens,
             "cost": r.cost, "duration": r.duration, "note": r.note,
             "created_at": r.created_at.isoformat()}
            for r in rows
        ],
    }


# ——— 日报 ———

@router.get("/daily")
def daily(session: SessionDep, days: int = Query(14, ge=1, le=90)) -> dict[str, Any]:
    """近 N 天。

    ## ★ 列名照猎聘，数据是 BOSS 能算出来的；算不出来的给 `null`

    前端按猎聘的表头渲染（日期/新增/直索要/打招呼/收到简历/转化率/转人工），
    `null` 显示成「—」。**不硬造数字**——一个编出来的 0 比一个「—」有害得多。

    | 列 | BOSS 能不能算 | 依据 |
    |---|---|---|
    | 新增 | ✅ | `job_candidate.created_at` |
    | 打招呼 | ✅ | `greet_log.greeted_at` |
    | 转人工 | ✅ | `task_log.failed_needs_manual` |
    | 直索要 | ❌ 恒 `null` | BOSS 没有「直索要」这一步（它的「求简历」是聊天页上的动作，也没采到时间） |
    | 收到简历 | ❌ 恒 `null` | BOSS 没有「收到简历」这个事件，消息里也判断不出来 |
    | 转化率 | ❌ 恒 `null` | 依赖上面那个数 |

    另外**多给两个 BOSS 自己的数**（`messages_in` / `messages_out`）：
    它们不占表头，放在表格 footer 里显示——这是 BOSS 最实在的每日指标。
    """

    def count(model: Any, *where: Any, start: datetime, end: datetime, field: Any) -> int:
        stmt = select(func.count()).select_from(model).where(field >= start).where(field < end)
        for clause in where:
            stmt = stmt.where(clause)
        return session.scalar(stmt) or 0

    today = date.today()
    out = []
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        out.append({
            "date": day.isoformat(),
            "new_jc": count(models.JobCandidate, start=start, end=end,
                            field=models.JobCandidate.created_at),
            "greet_request": count(models.GreetLog, start=start, end=end,
                                   field=models.GreetLog.greeted_at),
            "manual": count(models.TaskLog,
                            models.TaskLog.outcome == "failed_needs_manual",
                            start=start, end=end, field=models.TaskLog.created_at),
            # —— BOSS 上算不出来的三列 ——
            "direct_request": None,
            "received": None,
            "conversion": None,
            # —— 表头之外的补充（footer 里显示）——
            "messages_in": count(models.Message, models.Message.direction == "in",
                                 start=start, end=end, field=models.Message.first_seen_at),
            "messages_out": count(models.Message, models.Message.direction == "out",
                                  start=start, end=end, field=models.Message.first_seen_at),
        })

    def total(key: str) -> int:
        return sum(d[key] or 0 for d in out)

    return {
        "days": out,
        "totals": {
            "new_jc": total("new_jc"), "greet_request": total("greet_request"),
            "manual": total("manual"), "messages_in": total("messages_in"),
            "messages_out": total("messages_out"),
            # 依赖 null 列的合计也是 null，不假装是 0
            "received": None, "conversion": None,
        },
    }


# ——— 评分偏好 ———

@router.get("/scoring")
def list_scoring(session: SessionDep, usable_only: bool = False) -> dict[str, Any]:
    """每个职位的评分偏好。

    ★ **打分链路还没做**，所以这些设置暂时不影响任何行为。它存在是因为
      猎聘的「评分偏好」就长在职位上（驾驶舱的职位卡里），BOSS 跟着对齐；
      真接上打分时它就是输入。

    `usable_only=true` 只给**可用职位**（启用过 + BOSS 在招 + JD 读全）。

    ★ 判定走 `hr_tools.job_list_active()`，**不在调用方重写一遍**——
      「可用」的定义只该有一份（联锁焊在代码里，两边各判一次迟早分叉）。
    """
    rows = hr_tools.job_list_active(session) if usable_only else list(
        session.scalars(select(models.Job).order_by(models.Job.title)).all()
    )
    return {"items": [
        {"job_id": j.id, "title": j.title, "state": j.state, "status": j.status,
         "jd_complete": j.jd_complete, "jd_text": j.jd_text, "scoring_prefs": j.scoring_prefs or {}}
        for j in rows
    ]}


class ScoringIn(BaseModel):
    scoring_prefs: dict[str, Any] = Field(default_factory=dict)


@router.patch("/scoring/{job_id}")
def patch_scoring(job_id: int, body: ScoringIn, session: SessionDep) -> dict[str, Any]:
    job = session.get(models.Job, job_id)
    if job is None:
        raise HTTPException(404, "没有这个职位")
    # 直接整块覆盖——形状由前端定（boosters / veto / requirements / min_stars）
    job.scoring_prefs = body.scoring_prefs
    session.commit()
    return {"job_id": job_id, "scoring_prefs": job.scoring_prefs}


# ——— 职位（PATCH 只改系统侧的两样东西）———

class JobPatchIn(BaseModel):
    status: str | None = None       # active | draft
    jd_text: str | None = None


@router.patch("/jobs/{job_id}")
def patch_job(job_id: int, body: JobPatchIn, session: SessionDep) -> dict[str, Any]:
    """改职位的**系统侧**属性。

    ★ 刻意只能改这两样：
      - `status`：启用/停用（联锁用）
      - `jd_text`：人工补录 JD（补完要显式把 `jd_complete` 打开）

      **BOSS 那边的职位名/城市/招聘类型是发布后不可改的**（页面原文），
      所以这里根本不提供那些字段——省得有人以为改了能同步回去。
    """
    job = session.get(models.Job, job_id)
    if job is None:
        raise HTTPException(404, "没有这个职位")
    if body.status is not None:
        if body.status not in (models.JOB_STATUS_ACTIVE, models.JOB_STATUS_DRAFT):
            raise HTTPException(422, f"status 只能是 {models.JOB_STATUS_ACTIVE} / {models.JOB_STATUS_DRAFT}")
        job.status = body.status
    if body.jd_text is not None:
        job.jd_text = body.jd_text
        # 人工补录的 JD 一律当作「读全了」——它就是人写进去的全文
        job.jd_complete = bool(body.jd_text.strip())
    session.commit()
    return {"id": job.id, "status": job.status, "jd_complete": job.jd_complete}
