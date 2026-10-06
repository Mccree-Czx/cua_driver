"""岗位管理端点（M1 最小）：POST /api/jobs 建岗位、GET /api/jobs 列岗位。

字段与 models.Job 对齐；漏斗视图 / 候选人详情 / 复核接口属 M3（D7 API 面）。
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_session
from app.models import JOB_STATUS_ACTIVE, Job

router = APIRouter(prefix="/api", tags=["jobs"])


class JobCreate(BaseModel):
    title: str
    jd_text: str
    hard_rules: dict[str, Any] = Field(default_factory=dict)
    template_msgs: dict[str, Any] = Field(default_factory=dict)
    llm_threshold: int = 70
    status: str = JOB_STATUS_ACTIVE


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    jd_text: str
    hard_rules: dict[str, Any]
    template_msgs: dict[str, Any]
    llm_threshold: int
    status: str
    created_at: datetime


@router.post("/jobs", response_model=JobOut, status_code=201)
def create_job(payload: JobCreate, session: Session = Depends(get_session)):
    job = Job(**payload.model_dump())
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(session: Session = Depends(get_session)):
    return session.execute(select(Job).order_by(Job.id)).scalars().all()
