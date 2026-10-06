"""模型往返 + 约束测试（真实 MySQL：测试库 hr_workbuddy_test，走 Alembic 迁移建表）。

覆盖任务要求的核心行为：
- Job.llm_threshold 默认 70、JSON 列往返
- 重复 (job_id, candidate_id) → IntegrityError（UNIQUE 约束真实打库）
- status 存 CandidateStatus 字符串值（Python 枚举、VARCHAR(32)）
- 原生 ENUM 列（source/direction/msg_type）持久化往返
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app import models
from hr_workbuddy import CandidateStatus


def _unique(prefix: str) -> str:
    """每次运行唯一的标识，保证测试可重复执行不撞唯一约束。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _make_job_and_candidate(session) -> tuple[models.Job, models.Candidate]:
    suffix = _unique("t")
    job = models.Job(title=f"产品经理-{suffix}", jd_text="负责产品规划与迭代")
    candidate = models.Candidate(
        liepin_user_id=f"LP-{suffix}",
        name="张伟",
        online_resume_minimal={},
        source="inbound",
    )
    session.add_all([job, candidate])
    session.flush()
    return job, candidate


def test_job_defaults_and_json_roundtrip(session):
    """llm_threshold 默认 70；hard_rules/template_msgs JSON 往返；created_at 落库。"""
    job = models.Job(
        title=_unique("产品经理"),
        jd_text="负责产品规划",
        hard_rules={"min_education": "本科", "min_years": 3},
        template_msgs={"greet_request": "您好 {name}，看到您在看{title}岗位"},
    )
    assert job.llm_threshold == 70  # flush 前即取 Python 侧默认
    assert job.status == "active"

    session.add(job)
    session.commit()

    loaded = session.execute(select(models.Job).where(models.Job.id == job.id)).scalar_one()
    assert loaded.llm_threshold == 70
    assert loaded.hard_rules == {"min_education": "本科", "min_years": 3}
    assert loaded.template_msgs == {"greet_request": "您好 {name}，看到您在看{title}岗位"}
    assert loaded.created_at is not None


def test_candidate_roundtrip_and_unique_liepin_user_id(session):
    """liepin_user_id 全局唯一（真实打库 IntegrityError）；JSON 快照与 source 往返。"""
    liepin_id = _unique("LP")
    snapshot = {
        "name": "张伟",
        "liepin_user_id": liepin_id,
        "education": "本科",
        "years_of_experience": "3年",
        "city": "北京",
        "salary": "20-30K",
        "experience_summary": "三年产品经验",
    }
    candidate = models.Candidate(
        liepin_user_id=liepin_id,
        name="张伟",
        online_resume_minimal=snapshot,
        snapshot_object_key=f"snapshots/{liepin_id}/20261005_120000.png",
        source="inbound",
    )
    session.add(candidate)
    session.commit()

    loaded = session.execute(
        select(models.Candidate).where(models.Candidate.liepin_user_id == liepin_id)
    ).scalar_one()
    assert loaded.online_resume_minimal == snapshot
    assert loaded.snapshot_object_key == f"snapshots/{liepin_id}/20261005_120000.png"
    assert loaded.source == "inbound"

    dup = models.Candidate(
        liepin_user_id=liepin_id, name="重复", online_resume_minimal={}, source="inbound"
    )
    session.add(dup)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_job_candidate_unique_pair_raises_integrity_error(session):
    """UNIQUE(job_id, candidate_id)：同对第二行 flush 即 IntegrityError。"""
    job, candidate = _make_job_and_candidate(session)

    session.add(models.JobCandidate(job_id=job.id, candidate_id=candidate.id))
    session.flush()
    session.add(models.JobCandidate(job_id=job.id, candidate_id=candidate.id))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_job_candidate_status_stores_candidate_status_string(session):
    """status 为 VARCHAR(32)：库中存 CandidateStatus 字符串值；默认 new。"""
    job, candidate = _make_job_and_candidate(session)

    default_jc = models.JobCandidate(job_id=job.id, candidate_id=candidate.id)
    assert default_jc.status == CandidateStatus.NEW.value

    other = models.Candidate(
        liepin_user_id=_unique("LP"), name="李娜", online_resume_minimal={}, source="inbound"
    )
    session.add(other)
    session.flush()
    jc = models.JobCandidate(
        job_id=job.id,
        candidate_id=other.id,
        status=CandidateStatus.SCREENED_PASS.value,
        match_score=82,
        judge_reason="硬规则通过，LLM 评分 82 > 70",
    )
    session.add_all([default_jc, jc])
    session.commit()

    raw = session.execute(
        text("SELECT status FROM job_candidate WHERE id = :id"), {"id": jc.id}
    ).scalar_one()
    assert raw == "screened_pass"
    assert CandidateStatus(raw) is CandidateStatus.SCREENED_PASS

    loaded = session.get(models.JobCandidate, jc.id)
    assert loaded.match_score == 82
    assert loaded.judge_reason == "硬规则通过，LLM 评分 82 > 70"
    assert loaded.resume_requested_at is None  # 72h 锚点字段存在，初始为空


def test_interaction_native_enum_roundtrip(session):
    """direction/msg_type 为 MySQL 原生 ENUM，out/greet_request 可持久化往返。"""
    job, candidate = _make_job_and_candidate(session)
    jc = models.JobCandidate(job_id=job.id, candidate_id=candidate.id)
    session.add(jc)
    session.flush()

    interaction = models.Interaction(
        job_candidate_id=jc.id,
        direction="out",
        msg_type="greet_request",
        content="您好，看到您在关注产品经理岗位，方便发一份简历吗？",
        sent_at=datetime(2026, 10, 5, 10, 30, 0),
    )
    session.add(interaction)
    session.commit()

    loaded = session.execute(
        select(models.Interaction).where(models.Interaction.id == interaction.id)
    ).scalar_one()
    assert loaded.direction == "out"
    assert loaded.msg_type == "greet_request"
    assert loaded.content == "您好，看到您在关注产品经理岗位，方便发一份简历吗？"
    assert loaded.sent_at == datetime(2026, 10, 5, 10, 30, 0)


def test_review_override_roundtrip(session):
    """人工复核流水：old/new status 用 CandidateStatus 字符串值。"""
    job, candidate = _make_job_and_candidate(session)
    jc = models.JobCandidate(
        job_id=job.id,
        candidate_id=candidate.id,
        status=CandidateStatus.SCREENED_PASS.value,
    )
    session.add(jc)
    session.flush()

    override = models.ReviewOverride(
        job_candidate_id=jc.id,
        old_status=CandidateStatus.SCREENED_PASS.value,
        new_status=CandidateStatus.REJECTED_LLM.value,
        operator="hr-zhang",
        reason="复核认为经验与岗位不符",
    )
    session.add(override)
    session.commit()

    loaded = session.execute(
        select(models.ReviewOverride).where(models.ReviewOverride.id == override.id)
    ).scalar_one()
    assert loaded.old_status == "screened_pass"
    assert loaded.new_status == "rejected_llm"
    assert loaded.operator == "hr-zhang"
    assert loaded.reason == "复核认为经验与岗位不符"


def test_task_log_roundtrip(session):
    """TaskLog token 成本落账（spec §5）：task_id 可回查、数值字段往返。"""
    task_id = str(uuid.uuid4())
    log = models.TaskLog(
        task_id=task_id, outcome="success", attempt=1, tokens=1234, cost=56, duration=1.25
    )
    session.add(log)
    session.commit()

    loaded = session.execute(
        select(models.TaskLog).where(models.TaskLog.task_id == task_id)
    ).scalar_one()
    assert loaded.outcome == "success"
    assert loaded.attempt == 1
    assert loaded.tokens == 1234
    assert loaded.cost == 56
    assert loaded.duration == 1.25
    assert loaded.created_at is not None
