"""72h 关闭巡检（B 方案自 orchestrator 迁出，唯一保留的编排纯函数）。

close_stale_awaiting：awaiting_resume 且 resume_requested_at 严格早于
now-72h → no_response→closed；恰好 72h 不关；零追发。纯逻辑，
提交由调用方（hr-tools 工具 / 测试）负责。
"""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import JobCandidate
from app.state_machine import StateEvent, transition
from hr_workbuddy import CandidateStatus

RESUME_TIMEOUT = timedelta(hours=72)


def close_stale_awaiting(
    session: Session, account_id: int, now: datetime | None = None
) -> int:
    """关闭过期的 awaiting_resume（严格 > 72h），返回关闭数。

    account_id 必填：多账号下必须只扫当前生效账号的 jc，否则会把别的号的
    候选人一起关掉。
    """
    now = now or datetime.now()
    cutoff = now - RESUME_TIMEOUT
    stale = session.execute(
        select(JobCandidate).where(
            JobCandidate.account_id == account_id,
            JobCandidate.status == CandidateStatus.AWAITING_RESUME.value,
            JobCandidate.resume_requested_at < cutoff,
        )
    ).scalars().all()
    for jc in stale:
        jc.status = transition(jc, StateEvent.NO_RESPONSE).status
        jc.status = transition(jc, StateEvent.CLOSE).status
    return len(stale)
