"""岗位管理端点（M1 最小）：POST /api/jobs 建岗位、GET /api/jobs 列岗位。

字段与 models.Job 对齐；漏斗视图 / 候选人详情 / 复核接口属 M3（D7 API 面）。
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_active_account, get_session
from app.models import JOB_STATUS_ACTIVE, Account, Job

router = APIRouter(prefix="/api", tags=["jobs"])


class JobCreate(BaseModel):
    title: str
    jd_text: str
    hard_rules: dict[str, Any] = Field(default_factory=dict)
    template_msgs: dict[str, Any] = Field(default_factory=dict)
    # 已废弃（2026-10-15）：筛选不读它，见 models.Job.llm_threshold 上的说明。
    # 默认值曾是 40，与 DB 的 70 不一致；统一到 70。
    llm_threshold: int = 70
    scoring_prefs: dict[str, Any] = Field(default_factory=dict)
    status: str = JOB_STATUS_ACTIVE


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    jd_text: str
    # 扫职位时从卡片上读回来的（0011）。存量职位为 null。
    location: str | None = None
    education: str | None = None
    experience: str | None = None
    salary: str | None = None
    hard_rules: dict[str, Any]
    template_msgs: dict[str, Any]
    llm_threshold: int
    scoring_prefs: dict[str, Any]
    status: str
    created_at: datetime


@router.post("/jobs", response_model=JobOut, status_code=201)
def create_job(
    payload: JobCreate,
    session: Session = Depends(get_session),
    acct: Account = Depends(get_active_account),
):
    """建岗位。归属当前生效账号——职位是从那个号的 App 里读到的。"""
    job = Job(account_id=acct.id, **payload.model_dump())
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    session: Session = Depends(get_session),
    acct: Account = Depends(get_active_account),
):
    """列当前账号的岗位。"""
    return (
        session.execute(
            select(Job).where(Job.account_id == acct.id).order_by(Job.id)
        )
        .scalars()
        .all()
    )
