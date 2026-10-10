"""72h 关闭（R3）：close_stale_awaiting 边界。真实 MySQL。

- +72h1s → awaiting_resume 关为 closed，out 消息总数保持 1（零追发）
- 恰好 72h 不关【Review Focus 3】（严格 >，`resume_requested_at < now-72h`）
- 窗口内与终态行不受影响

函数级用例传显式 now（时钟注入），边界语义精确可测。
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app import models
from app.sweep import RESUME_TIMEOUT, close_stale_awaiting
from hr_workbuddy import CandidateStatus

FIXED_NOW = datetime(2026, 10, 5, 12, 0, 0)


def _make_awaiting(session, resume_requested_at: datetime) -> models.JobCandidate:
    """落一条 awaiting_resume 的 jc + 1 行历史 out/greet_request（一人一消息基线）。"""
    suffix = uuid.uuid4().hex[:12]
    job = models.Job(title=f"产品经理-{suffix}", jd_text="负责产品规划与迭代")
    candidate = models.Candidate(
        liepin_user_id=f"LP{suffix}", name="张伟", online_resume_minimal={}, source="inbound"
    )
    session.add_all([job, candidate])
    session.flush()
    jc = models.JobCandidate(
        job_id=job.id,
        candidate_id=candidate.id,
        status=CandidateStatus.AWAITING_RESUME.value,
        resume_requested_at=resume_requested_at,
    )
    session.add(jc)
    session.flush()
    session.add(
        models.Interaction(
            job_candidate_id=jc.id,
            direction="out",
            msg_type="greet_request",
            content="您好，方便发一份简历吗？",
            sent_at=resume_requested_at,
        )
    )
    return jc


def _out_count(session, jc_id: int) -> int:
    return session.execute(
        select(func.count())
        .select_from(models.Interaction)
        .where(
            models.Interaction.job_candidate_id == jc_id,
            models.Interaction.direction == "out",
        )
    ).scalar_one()


def test_close_stale_awaiting_after_72h_closes(session):
    """+72h1s → no_response→closed；out 消息总数仍 1（零追发，决策 3）。"""
    jc = _make_awaiting(session, FIXED_NOW - RESUME_TIMEOUT - timedelta(seconds=1))

    closed = close_stale_awaiting(session, now=FIXED_NOW)
    assert closed == 1

    # 编排层不提交（提交归 HTTP 层）：变更在同一会话内可见；expire_all 会从库
    # 重载未提交改动，此处只经 identity map 断言
    assert session.get(models.JobCandidate, jc.id).status == CandidateStatus.CLOSED.value
    assert _out_count(session, jc.id) == 1


def test_boundary_exact_72h_still_waits(session):
    """Review Focus 3：恰好 72h（严格 < 边界）不关闭，继续等待。"""
    jc = _make_awaiting(session, FIXED_NOW - RESUME_TIMEOUT)

    closed = close_stale_awaiting(session, now=FIXED_NOW)
    assert closed == 0
    assert session.get(models.JobCandidate, jc.id).status == CandidateStatus.AWAITING_RESUME.value


def test_close_only_touches_stale_awaiting_rows(session):
    """窗口内 awaiting、已 no_response 行不受影响；只关严格过期的 awaiting。"""
    fresh = _make_awaiting(session, FIXED_NOW - timedelta(hours=1))
    stale = _make_awaiting(session, FIXED_NOW - RESUME_TIMEOUT - timedelta(minutes=1))

    closed = close_stale_awaiting(session, now=FIXED_NOW)
    assert closed == 1

    assert session.get(models.JobCandidate, fresh.id).status == CandidateStatus.AWAITING_RESUME.value
    assert session.get(models.JobCandidate, stale.id).status == CandidateStatus.CLOSED.value
    assert _out_count(session, stale.id) == 1  # 关闭零追发
