"""D1 回调端点 + READ_RESUME→SEND_MESSAGE 编排链（spec §3 路径一）。

真实 MySQL（测试库）+ 真实 MinIO（127.0.0.1:9000）；screening 经
dependency_overrides 注入 FakeScreening——不触真实 LLM / 真实 screening HTTP。

覆盖任务要求 + Review Focus：
- inbound 直索要（2026-10-06 策略）：仅硬规则 → new→screened_pass→resume_requested
  →awaiting_resume 完整链、SEND_MESSAGE 用 direct_request 文案、interactions
  恰 1 行 out/direct_request（outbound 两层判定与收到后补评分见专用用例）
- 附件 .docx → 键保留 .docx【Review Focus 1】（pdf 走 put_resume 同测）
- 同 liepin_user_id 重复 read_resume 幂等【Review Focus 5】
- degraded → 不推进不发消息；rejected → 零触达
- CHECK_ATTACHMENT 有附件 → DOWNLOAD_ATTACHMENT；无附件 → 不动
- 一人一消息：二次 SEND_MESSAGE 成功 → TaskLog failed_needs_manual 不推进
- 延期重判 reenqueue_deferred
- 管理端点 POST/GET /api/jobs
"""

import re
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models
from app.db import SessionLocal
from app.deps import get_queue, get_screening, get_session
from app.main import app
from app.storage import ObjectStore
from fakes import DEGRADED_RESULT, FakeScreening, FakeTaskQueue
from hr_workbuddy import (
    AtomicTask,
    AtomicTaskType,
    CandidateStatus,
    ScreeningResult,
)

BUCKET = "hr-workbuddy"  # infra 契约（minio-init 创建）
GREET_TEMPLATE = "您好 {name}，看到您在看{title}岗位，方便发一份简历吗？"


def _resume(liepin_id: str) -> dict:
    """MinimalResume 恰 7 字段（决策 10）。"""
    return {
        "name": "张伟",
        "liepin_user_id": liepin_id,
        "education": "本科",
        "years_of_experience": "3年",
        "city": "北京",
        "salary": "20-30K",
        "experience_summary": "三年互联网产品经验",
    }


def _pass_result(score: int = 82) -> ScreeningResult:
    return ScreeningResult(
        hard_pass=True,
        hard_reasons=[],
        score=score,
        judge_reason=f"LLM 评分 {score} 通过",
        status=CandidateStatus.SCREENED_PASS,
        degraded=False,
    )


def _make_read_task(job_id: int, liepin_id: str) -> AtomicTask:
    """READ_RESUME 派发任务：候选人不详，job_candidate_id 为空。"""
    return AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.READ_RESUME,
        job_id=job_id,
        job_candidate_id=None,
        candidate_liepin_id=liepin_id,
        context={},
    )


def _post_read_result(client, task: AtomicTask, liepin_id: str):
    return client.post(
        f"/internal/tasks/{task.task_id}/result",
        json={
            "task_id": str(task.task_id),
            "outcome": "success",
            "evidence": {
                "resume": _resume(liepin_id),
                "screenshot_keys": [],
                "brain_tokens": 10,
                "cost_est": 1,
            },
            "error": None,
        },
    )


def _post_send_result(client, task: AtomicTask):
    return client.post(
        f"/internal/tasks/{task.task_id}/result",
        json={
            "task_id": str(task.task_id),
            "outcome": "success",
            "evidence": {"sent_at": "2026-10-05T10:00:00", "brain_tokens": 5, "cost_est": 0},
            "error": None,
        },
    )


def _post_check_attachment_result(client, task: AtomicTask, *, has_attachment: bool):
    """CHECK_ATTACHMENT 成功结果：evidence 仅 has_attachment（契 约）。"""
    return client.post(
        f"/internal/tasks/{task.task_id}/result",
        json={
            "task_id": str(task.task_id),
            "outcome": "success",
            "evidence": {"has_attachment": has_attachment, "attempt": 0, "duration_s": 0.5},
            "error": None,
        },
    )


def _reload(session):
    """API 调用后：提交本会话挂起变更 + 清空身份映射，使后续读取见到
    API 已提交数据（MySQL RR 快照 + expire_on_commit=False 双重要求）。"""
    session.commit()
    session.expire_all()


def _create_job(client) -> tuple[int, str]:
    """经管理端点建岗位（顺带覆盖 POST /api/jobs），返回 (job_id, title)。"""
    title = f"产品经理-{uuid4().hex[:8]}"
    resp = client.post(
        "/api/jobs",
        json={
            "title": title,
            "jd_text": "负责产品规划与迭代",
            "hard_rules": {"min_education": "本科", "min_years": 3},
            "template_msgs": {"greet_request": GREET_TEMPLATE},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["llm_threshold"] == 70  # 默认阈值（spec 决策 R6）
    assert body["status"] == "active"
    return body["id"], title


def _make_awaiting_jc(session) -> tuple[models.Job, models.Candidate, models.JobCandidate]:
    """直接落库：awaiting_resume 状态的 jc（附件/巡检测试的起点，跳过前端链）。"""
    suffix = uuid4().hex[:12]
    job = models.Job(
        title=f"产品经理-{suffix}",
        jd_text="负责产品规划与迭代",
        template_msgs={"greet_request": GREET_TEMPLATE},
    )
    candidate = models.Candidate(
        liepin_user_id=f"LP{suffix}",
        name="张伟",
        online_resume_minimal=_resume(f"LP{suffix}"),
        source="inbound",
    )
    session.add_all([job, candidate])
    session.flush()
    jc = models.JobCandidate(
        job_id=job.id,
        candidate_id=candidate.id,
        status=CandidateStatus.AWAITING_RESUME.value,
        resume_requested_at=datetime.now(),
    )
    session.add(jc)
    _reload(session)
    return job, candidate, jc


# —— fixtures ——


@pytest.fixture()
def fake_queue():
    return FakeTaskQueue()


@pytest.fixture()
def fake_screening():
    return FakeScreening()


@pytest.fixture()
def client(fake_queue, fake_screening):
    """TestClient + 依赖替身：FakeTaskQueue/FakeScreening；MySQL 与 MinIO 保持真实。"""
    app.dependency_overrides[get_queue] = lambda: fake_queue
    app.dependency_overrides[get_screening] = lambda: fake_screening
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def store():
    s = ObjectStore()
    s.ensure_bucket(BUCKET)
    return s


# —— READ_RESUME → SEND_MESSAGE 主链 ——


def test_read_resume_pass_full_chain(client, fake_queue, fake_screening, session, store):
    """inbound 分流（2026-10-06）：读→CHECK（无附件）→硬规则→直索要→触达→等待。
    
    READ_RESUME 结果 → 建候选人 + 最小字段 + 派发 CHECK_ATTACHMENT（不调 screening）；
    CHECK 无附件 → 此刻才走硬规则（llm_scoring=False）→ resume_requested + SEND（direct_request）；
    SEND_MESSAGE 成功 → awaiting_resume + interactions 恰 1 行 out/direct_request；
    snapshot artifact → MinIO snapshots/ 键 + candidates.snapshot_object_key。
    """
    job_id, title = _create_job(client)
    liepin = f"LP{uuid4().hex[:12]}"
    fake_screening.set(liepin, _pass_result(82))
    
    read_task = _make_read_task(job_id, liepin)
    fake_queue.record(read_task)
    
    resp = _post_read_result(client, read_task, liepin)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    assert fake_screening.requests == []  # 读后不调 screening（先探附件，2026-10-06 分流）
    
    session.commit()  # 新事务读 API 已提交数据（MySQL RR 快照）
    candidate = session.execute(
        select(models.Candidate).where(models.Candidate.liepin_user_id == liepin)
    ).scalar_one()
    assert candidate.source == "inbound"
    assert candidate.name == "张伟"
    assert candidate.online_resume_minimal == _resume(liepin)  # 恰 7 字段
    
    jc = session.execute(
        select(models.JobCandidate).where(
            models.JobCandidate.job_id == job_id,
            models.JobCandidate.candidate_id == candidate.id,
        )
    ).scalar_one()
    assert jc.status == CandidateStatus.NEW.value  # 读后未判定（等 CHECK 结果）
    
    # —— CHECK_ATTACHMENT 已派发（分流第一步）——
    assert len(fake_queue.enqueued) == 1
    check_task = fake_queue.enqueued[0]
    assert check_task.type is AtomicTaskType.CHECK_ATTACHMENT
    assert check_task.job_candidate_id == jc.id
    assert check_task.candidate_liepin_id == liepin
    
    # —— CHECK 结果：无附件 → 此刻才走硬规则 → 直索要 ——
    resp = _post_check_attachment_result(client, check_task, has_attachment=False)
    assert resp.status_code == 200, resp.text
    _reload(session)
    assert len(fake_screening.requests) == 1
    req = fake_screening.requests[0]
    assert req.job_id == job_id
    assert req.threshold == 70
    assert req.jd_text == "负责产品规划与迭代"
    assert req.hard_rules == {"min_education": "本科", "min_years": 3}
    assert req.resume.liepin_user_id == liepin
    assert req.llm_scoring is False  # inbound 直索要：仅硬规则（2026-10-06 策略）
    
    jc = session.get(models.JobCandidate, jc.id)
    assert jc.status == CandidateStatus.RESUME_REQUESTED.value  # new→screened_pass→resume_requested
    assert jc.match_score is None  # 前置评分已免（收到简历后补评）
    assert jc.judge_reason == "硬规则通过（评分后移至简历收到后）"
    
    # —— SEND_MESSAGE 入队：direct_request 变体 + 渲染文本 ——
    send_tasks = [t for t in fake_queue.enqueued if t.type is AtomicTaskType.SEND_MESSAGE]
    assert len(send_tasks) == 1
    (send_task,) = send_tasks
    assert send_task.job_candidate_id == jc.id
    assert send_task.candidate_liepin_id == liepin
    assert send_task.context["variant"] == "direct_request"
    assert send_task.context["text"] == f"您好 张伟，方便发一份简历吗？"  # direct_request 默认模板（零岗位名）
    
    # —— SEND_MESSAGE 成功 → awaiting_resume + 72h 锚点 + out/direct_request ——
    resp2 = _post_send_result(client, send_task)
    assert resp2.status_code == 200, resp2.text
    
    _reload(session)
    jc2 = session.get(models.JobCandidate, jc.id)
    assert jc2.status == CandidateStatus.AWAITING_RESUME.value
    assert jc2.resume_requested_at is not None
    assert jc2.last_touch_at is not None
    
    interactions = session.execute(
        select(models.Interaction).where(
            models.Interaction.job_candidate_id == jc.id
        )
    ).scalars().all()
    assert len(interactions) == 1  # 恰 1 行
    assert interactions[0].direction == "out"
    assert interactions[0].msg_type == "direct_request"  # inbound 直索要（2026-10-06 策略）
    assert interactions[0].content == f"您好 张伟，方便发一份简历吗？"

    # —— snapshot artifact → snapshots/{liepin}/YYYYMMDD_HHMMSS.png + 落库 + 真实 MinIO ——
    png = b"\x89PNG\r\n\x1a\n" + uuid4().bytes
    resp3 = client.post(
        f"/internal/tasks/{read_task.task_id}/artifact",
        files={"file": ("snapshot.png", png, "image/png")},
        data={
            "task_id": str(read_task.task_id),
            "kind": "snapshot",
            "filename": "snapshot.png",
        },
    )
    assert resp3.status_code == 200, resp3.text

    _reload(session)
    candidate2 = session.get(models.Candidate, candidate.id)
    assert candidate2.snapshot_object_key is not None
    assert re.fullmatch(
        rf"snapshots/{re.escape(liepin)}/\d{{8}}_\d{{6}}\.png",
        candidate2.snapshot_object_key,
    ), candidate2.snapshot_object_key
    try:
        got = store.client.get_object(BUCKET, candidate2.snapshot_object_key)
        assert got.read() == png  # 真实 MinIO 字节一致
        got.close()
        got.release_conn()
    finally:
        store.client.remove_object(BUCKET, candidate2.snapshot_object_key)


def test_degraded_stays_new_no_message(client, fake_queue, fake_screening, session):
    """degraded=True → 存快照+最小字段、status 保持 new、judge_reason=deferred、不发消息。

    2026-10-06 策略后 degraded 仅存于 outbound（llm_scoring=True）路径——
    inbound 直索要不调 LLM；本用例改用 recommended 候选人保持语义。
    """
    job_id, _ = _create_job(client)
    liepin = f"LP{uuid4().hex[:12]}"
    session.add(models.Candidate(liepin_user_id=liepin, name="张伟", source="recommended"))
    session.commit()
    fake_screening.set(liepin, DEGRADED_RESULT)

    read_task = _make_read_task(job_id, liepin)
    fake_queue.record(read_task)
    resp = _post_read_result(client, read_task, liepin)
    assert resp.status_code == 200

    _reload(session)
    candidate = session.execute(
        select(models.Candidate).where(models.Candidate.liepin_user_id == liepin)
    ).scalar_one()
    assert candidate.online_resume_minimal == _resume(liepin)
    jc = session.execute(
        select(models.JobCandidate).where(
            models.JobCandidate.job_id == job_id,
            models.JobCandidate.candidate_id == candidate.id,
        )
    ).scalar_one()
    assert jc.status == CandidateStatus.NEW.value  # 不推进
    assert jc.judge_reason == "deferred: LLM unavailable"
    assert jc.match_score is None
    assert fake_queue.enqueued == []  # 不发消息
    assert session.execute(
        select(models.Interaction).where(models.Interaction.job_candidate_id == jc.id)
    ).scalars().all() == []


@pytest.mark.parametrize(
    ("result", "expected_status", "expected_score"),
    [
        (
            ScreeningResult(
                hard_pass=False,
                hard_reasons=["学历不足"],
                score=None,
                judge_reason="硬规则不通过: 学历不足",
                status=CandidateStatus.REJECTED_HARD,
                degraded=False,
            ),
            CandidateStatus.REJECTED_HARD.value,
            None,
        ),
        (
            ScreeningResult(
                hard_pass=True,
                hard_reasons=[],
                score=55,
                judge_reason="LLM 评分 55 未达阈值",
                status=CandidateStatus.REJECTED_LLM,
                degraded=False,
            ),
            CandidateStatus.REJECTED_LLM.value,
            55,
        ),
    ],
    ids=["rejected_hard", "rejected_llm"],
)
def test_rejected_zero_contact(
    client, fake_queue, fake_screening, session, result, expected_status, expected_score
):
    """rejected_hard / rejected_llm → 对应状态 + judge_reason 落库，零触达。

    2026-10-06 策略后 rejected_llm 仅存于 outbound（llm_scoring=True）；两种
    拒绝语义均以 recommended 候选人验证（inbound 直索要不产生 rejected_llm）。
    """
    job_id, _ = _create_job(client)
    liepin = f"LP{uuid4().hex[:12]}"
    session.add(models.Candidate(liepin_user_id=liepin, name="张伟", source="recommended"))
    session.commit()
    fake_screening.set(liepin, result)

    read_task = _make_read_task(job_id, liepin)
    fake_queue.record(read_task)
    resp = _post_read_result(client, read_task, liepin)
    assert resp.status_code == 200

    _reload(session)
    candidate = session.execute(
        select(models.Candidate).where(models.Candidate.liepin_user_id == liepin)
    ).scalar_one()
    jc = session.execute(
        select(models.JobCandidate).where(
            models.JobCandidate.job_id == job_id,
            models.JobCandidate.candidate_id == candidate.id,
        )
    ).scalar_one()
    assert jc.status == expected_status
    assert jc.judge_reason == result.judge_reason
    assert jc.match_score == expected_score
    assert fake_screening.requests[0].llm_scoring is True  # outbound 两层判定
    assert fake_queue.enqueued == []  # 零触达：无 SEND_MESSAGE
    assert session.execute(
        select(models.Interaction).where(models.Interaction.job_candidate_id == jc.id)
    ).scalars().all() == []


def test_duplicate_read_result_idempotent(client, fake_queue, fake_screening, session):
    """Review Focus 5：同 liepin_user_id 重复 read_resume 结果不重复建档/不重复推进。

    inbound 已改为 读→CHECK→硬规则 序列（2026-10-06），幂等语义以 outbound
    （recommended，读后即判定）验证，保持原断言形状。
    """
    job_id, _ = _create_job(client)
    liepin = f"LP{uuid4().hex[:12]}"
    session.add(models.Candidate(liepin_user_id=liepin, name="张伟", source="recommended"))
    session.commit()
    fake_screening.set(liepin, _pass_result(82))

    read_task = _make_read_task(job_id, liepin)
    fake_queue.record(read_task)
    assert _post_read_result(client, read_task, liepin).status_code == 200
    assert len(fake_queue.enqueued) == 1

    # 第二次读取（新任务、同 liepin_user_id）→ 忽略重复结果
    read_task2 = _make_read_task(job_id, liepin)
    fake_queue.record(read_task2)
    resp2 = _post_read_result(client, read_task2, liepin)
    assert resp2.status_code == 200

    _reload(session)
    candidates = session.execute(
        select(models.Candidate).where(models.Candidate.liepin_user_id == liepin)
    ).scalars().all()
    assert len(candidates) == 1  # 不重复建档
    jc = session.execute(
        select(models.JobCandidate).where(
            models.JobCandidate.job_id == job_id,
            models.JobCandidate.candidate_id == candidates[0].id,
        )
    ).scalar_one()
    assert jc.status == CandidateStatus.RESUME_REQUESTED.value  # 不重复推进/重判
    assert len(fake_screening.requests) == 1  # 判定只调过一次
    assert len(fake_queue.enqueued) == 1  # 仍只有 1 条 SEND_MESSAGE
    assert session.execute(
        select(models.Interaction).where(models.Interaction.job_candidate_id == jc.id)
    ).scalars().all() == []


# —— artifact：附件归档（Review Focus 1）——


def test_attachment_non_pdf_docx_key_keeps_extension(client, fake_queue, session, store):
    """Review Focus 1：.docx 附件按原始扩展名归档，键保留 .docx。"""
    job, candidate, jc = _make_awaiting_jc(session)
    dl_task = AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.DOWNLOAD_ATTACHMENT,
        job_id=job.id,
        job_candidate_id=jc.id,
        candidate_liepin_id=candidate.liepin_user_id,
        context={},
    )
    fake_queue.record(dl_task)

    data = b"PK\x03\x04 fake docx bytes"
    resp = client.post(
        f"/internal/tasks/{dl_task.task_id}/artifact",
        files={"file": ("简历.docx", data, "application/octet-stream")},
        data={
            "task_id": str(dl_task.task_id),
            "kind": "resume",
            "filename": "简历.docx",
        },
    )
    assert resp.status_code == 200, resp.text

    _reload(session)
    jc2 = session.get(models.JobCandidate, jc.id)
    assert jc2.status == CandidateStatus.RESUME_RECEIVED.value
    assert jc2.resume_downloaded_at is not None
    assert jc2.minio_object_key.endswith(".docx")
    assert re.fullmatch(
        rf"resumes/{job.id}/{re.escape(candidate.liepin_user_id)}/张伟_{re.escape(job.title)}_\d{{8}}\.docx",
        jc2.minio_object_key,
    ), jc2.minio_object_key

    interaction = session.execute(
        select(models.Interaction).where(models.Interaction.job_candidate_id == jc.id)
    ).scalar_one()
    assert interaction.direction == "in"
    assert interaction.msg_type == "attachment"
    assert interaction.content == jc2.minio_object_key

    try:
        stat = store.client.stat_object(BUCKET, jc2.minio_object_key)
        assert stat.content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        got = store.client.get_object(BUCKET, jc2.minio_object_key)
        assert got.read() == data
        got.close()
        got.release_conn()
    finally:
        store.client.remove_object(BUCKET, jc2.minio_object_key)


def test_attachment_pdf_uses_put_resume(client, fake_queue, session, store):
    """pdf 附件走 put_resume：§4 规范键 resumes/{job}/{liepin}/{姓名}_{岗位}_{YYYYMMDD}.pdf。"""
    job, candidate, jc = _make_awaiting_jc(session)
    dl_task = AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.DOWNLOAD_ATTACHMENT,
        job_id=job.id,
        job_candidate_id=jc.id,
        candidate_liepin_id=candidate.liepin_user_id,
        context={},
    )
    fake_queue.record(dl_task)

    data = b"%PDF-1.4 fake resume"
    resp = client.post(
        f"/internal/tasks/{dl_task.task_id}/artifact",
        files={"file": ("简历.pdf", data, "application/pdf")},
        data={"task_id": str(dl_task.task_id), "kind": "resume", "filename": "简历.pdf"},
    )
    assert resp.status_code == 200, resp.text

    _reload(session)
    jc2 = session.get(models.JobCandidate, jc.id)
    assert jc2.status == CandidateStatus.RESUME_RECEIVED.value
    assert re.fullmatch(
        rf"resumes/{job.id}/{re.escape(candidate.liepin_user_id)}/张伟_{re.escape(job.title)}_\d{{8}}\.pdf",
        jc2.minio_object_key,
    ), jc2.minio_object_key
    try:
        got = store.client.get_object(BUCKET, jc2.minio_object_key)
        assert got.read() == data
        got.close()
        got.release_conn()
    finally:
        store.client.remove_object(BUCKET, jc2.minio_object_key)


def test_late_resume_artifact_stored_without_state_change(client, fake_queue, session, store):
    """真实模式门禁 ②：jc 已 closed 时到达的 resume artifact → 只存不推进。

    72h 关闭与在途下载链的竞态窗口：附件字节是真实简历唯一副本（worker 不
    留存），绝不静默丢弃——MinIO 有对象、minio_object_key 填上、状态仍
    closed（不迁移）、TaskLog 落一行注记「迟到附件，只存不推进」。
    """
    job, candidate, jc = _make_awaiting_jc(session)
    jc.status = CandidateStatus.CLOSED.value
    _reload(session)

    dl_task = AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.DOWNLOAD_ATTACHMENT,
        job_id=job.id,
        job_candidate_id=jc.id,
        candidate_liepin_id=candidate.liepin_user_id,
        context={},
    )
    fake_queue.record(dl_task)

    data = b"%PDF-1.4 late resume"
    resp = client.post(
        f"/internal/tasks/{dl_task.task_id}/artifact",
        files={"file": ("简历.pdf", data, "application/pdf")},
        data={"task_id": str(dl_task.task_id), "kind": "resume", "filename": "简历.pdf"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["object_key"]

    _reload(session)
    jc2 = session.get(models.JobCandidate, jc.id)
    assert jc2.status == CandidateStatus.CLOSED.value  # 不做状态迁移
    assert jc2.resume_downloaded_at is None
    assert jc2.minio_object_key == body["object_key"]  # 空则填上
    assert re.fullmatch(
        rf"resumes/{job.id}/{re.escape(candidate.liepin_user_id)}/张伟_{re.escape(job.title)}_\d{{8}}\.pdf",
        jc2.minio_object_key,
    ), jc2.minio_object_key
    assert session.execute(
        select(models.Interaction).where(models.Interaction.job_candidate_id == jc.id)
    ).scalars().all() == []  # 不落 in/attachment 行（仅 awaiting_resume 正常路径有）

    log = session.execute(
        select(models.TaskLog).where(models.TaskLog.task_id == str(dl_task.task_id))
    ).scalar_one()
    assert log.outcome == "failed_needs_manual"
    assert log.note == "迟到附件，只存不推进"

    try:
        got = store.client.get_object(BUCKET, jc2.minio_object_key)
        assert got.read() == data  # 真实 MinIO 字节一致
        got.close()
        got.release_conn()
    finally:
        store.client.remove_object(BUCKET, jc2.minio_object_key)


# —— CHECK_ATTACHMENT ——


def test_check_attachment_with_attachment_enqueues_download(client, fake_queue, session):
    """CHECK_ATTACHMENT 有附件 → 入队 DOWNLOAD_ATTACHMENT；无附件 → 不动。"""
    job, candidate, jc = _make_awaiting_jc(session)
    check_task = AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.CHECK_ATTACHMENT,
        job_id=job.id,
        job_candidate_id=jc.id,
        candidate_liepin_id=candidate.liepin_user_id,
        context={},
    )
    fake_queue.record(check_task)
    resp = client.post(
        f"/internal/tasks/{check_task.task_id}/result",
        json={
            "task_id": str(check_task.task_id),
            "outcome": "success",
            "evidence": {"has_attachment": True, "brain_tokens": 3, "cost_est": 0},
            "error": None,
        },
    )
    assert resp.status_code == 200
    assert len(fake_queue.enqueued) == 1
    download = fake_queue.enqueued[0]
    assert download.type is AtomicTaskType.DOWNLOAD_ATTACHMENT
    assert download.job_candidate_id == jc.id
    assert download.candidate_liepin_id == candidate.liepin_user_id
    _reload(session)
    assert session.get(models.JobCandidate, jc.id).status == CandidateStatus.AWAITING_RESUME.value


def test_check_attachment_without_attachment_noop(client, fake_queue, session):
    job, candidate, jc = _make_awaiting_jc(session)
    check_task = AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.CHECK_ATTACHMENT,
        job_id=job.id,
        job_candidate_id=jc.id,
        candidate_liepin_id=candidate.liepin_user_id,
        context={},
    )
    fake_queue.record(check_task)
    resp = client.post(
        f"/internal/tasks/{check_task.task_id}/result",
        json={
            "task_id": str(check_task.task_id),
            "outcome": "success",
            "evidence": {"has_attachment": False},
            "error": None,
        },
    )
    assert resp.status_code == 200
    assert fake_queue.enqueued == []
    _reload(session)
    assert session.get(models.JobCandidate, jc.id).status == CandidateStatus.AWAITING_RESUME.value


# —— 一人一消息（决策 3）——


def test_second_send_message_logged_failed_needs_manual(client, fake_queue, session):
    """jc 已有 out 消息时二次 SEND_MESSAGE 成功 → TaskLog failed_needs_manual，不推进。"""
    job, candidate, jc = _make_awaiting_jc(session)
    jc.status = CandidateStatus.RESUME_REQUESTED.value  # 回拨到发送前状态
    session.add(
        models.Interaction(
            job_candidate_id=jc.id,
            direction="out",
            msg_type="greet_request",
            content="您好（历史消息）",
            sent_at=datetime.now(),
        )
    )
    _reload(session)

    send_task = AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.SEND_MESSAGE,
        job_id=job.id,
        job_candidate_id=jc.id,
        candidate_liepin_id=candidate.liepin_user_id,
        context={"text": "您好（重复发送）", "candidate_liepin_id": candidate.liepin_user_id},
    )
    fake_queue.record(send_task)
    resp = _post_send_result(client, send_task)
    assert resp.status_code == 200

    _reload(session)
    assert session.get(models.JobCandidate, jc.id).status == CandidateStatus.RESUME_REQUESTED.value
    interactions = session.execute(
        select(models.Interaction).where(models.Interaction.job_candidate_id == jc.id)
    ).scalars().all()
    assert len(interactions) == 1  # 不落第二条 out 消息

    log = session.execute(
        select(models.TaskLog).where(models.TaskLog.task_id == str(send_task.task_id))
    ).scalar_one()
    assert log.outcome == "failed_needs_manual"
    assert log.tokens == 5  # evidence.brain_tokens
    assert log.cost == 0


# —— TaskLog 账目（R10）：按 (task_id, attempt) 落账 / duration / cost 浮点 ——


def _post_check_result(client, task: AtomicTask, evidence: dict) -> None:
    resp = client.post(
        f"/internal/tasks/{task.task_id}/result",
        json={
            "task_id": str(task.task_id),
            "outcome": "success",
            "evidence": {"has_attachment": False, **evidence},
            "error": None,
        },
    )
    assert resp.status_code == 200, resp.text


def _make_check_task(job, candidate, jc) -> AtomicTask:
    return AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.CHECK_ATTACHMENT,
        job_id=job.id,
        job_candidate_id=jc.id,
        candidate_liepin_id=candidate.liepin_user_id,
        context={},
    )


def test_task_log_per_attempt_rows_and_dedup(client, fake_queue, session):
    """同 task_id 不同 attempt 两次 result → 两行 TaskLog；同 (task_id, attempt) 重复 → 仍两行。

    arq 重试每次真实调用视觉 API、payload 不重写——账目按 attempt 逐次落，
    不丢重试账目；at-least-once 重复回调按 (task_id, attempt) 去重。
    """
    job, candidate, jc = _make_awaiting_jc(session)
    check_task = _make_check_task(job, candidate, jc)
    fake_queue.record(check_task)

    _post_check_result(client, check_task, {"attempt": 0})
    _post_check_result(client, check_task, {"attempt": 1})
    _post_check_result(client, check_task, {"attempt": 0})  # 重复：去重

    _reload(session)
    rows = session.execute(
        select(models.TaskLog).where(models.TaskLog.task_id == str(check_task.task_id))
    ).scalars().all()
    assert len(rows) == 2
    assert {r.attempt for r in rows} == {0, 1}


def test_task_log_attempt_defaults_to_zero_without_evidence(client, fake_queue, session):
    """evidence 无 attempt → 默认 0（旧 payload / 测试直投），与 handler 自落账对齐。"""
    job, candidate, jc = _make_awaiting_jc(session)
    check_task = _make_check_task(job, candidate, jc)
    fake_queue.record(check_task)

    _post_check_result(client, check_task, {})
    _post_check_result(client, check_task, {})  # 重复 (task_id, 0)：仍一行

    _reload(session)
    rows = session.execute(
        select(models.TaskLog).where(models.TaskLog.task_id == str(check_task.task_id))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].attempt == 0


def test_task_log_duration_from_evidence(client, fake_queue, session):
    """evidence 带 duration_s → TaskLog.duration 相等。"""
    job, candidate, jc = _make_awaiting_jc(session)
    check_task = _make_check_task(job, candidate, jc)
    fake_queue.record(check_task)

    _post_check_result(client, check_task, {"duration_s": 1.5})

    _reload(session)
    log = session.execute(
        select(models.TaskLog).where(models.TaskLog.task_id == str(check_task.task_id))
    ).scalar_one()
    assert log.duration == 1.5


def test_task_log_cost_float_not_truncated(client, fake_queue, session):
    """cost_est 小数（微元精度）落库不截断：Float 列往返相等。"""
    job, candidate, jc = _make_awaiting_jc(session)
    check_task = _make_check_task(job, candidate, jc)
    fake_queue.record(check_task)

    _post_check_result(client, check_task, {"cost_est": 0.000123})

    _reload(session)
    log = session.execute(
        select(models.TaskLog).where(models.TaskLog.task_id == str(check_task.task_id))
    ).scalar_one()
    assert log.cost == pytest.approx(0.000123)


# —— 延期重判 ——


def test_reenqueue_deferred_pass_rejudges_and_enqueues(client, fake_queue, fake_screening, session):
    """status=new 且 judge_reason 含 deferred → 用已存最小字段重判；通过照常入队 SEND_MESSAGE。"""
    job_id, _ = _create_job(client)
    liepin = f"LP{uuid4().hex[:12]}"
    candidate = models.Candidate(
        liepin_user_id=liepin,
        name="张伟",
        online_resume_minimal=_resume(liepin),
        source="inbound",
    )
    session.add(candidate)
    session.flush()
    jc = models.JobCandidate(
        job_id=job_id,
        candidate_id=candidate.id,
        status=CandidateStatus.NEW.value,
        judge_reason="deferred: LLM unavailable",
    )
    session.add(jc)
    _reload(session)

    fake_screening.set(liepin, _pass_result(82))
    resp = client.post("/internal/sweeps/deferred")
    assert resp.status_code == 200, resp.text
    assert resp.json()["judged"] >= 1

    _reload(session)
    jc2 = session.get(models.JobCandidate, jc.id)
    assert jc2.status == CandidateStatus.RESUME_REQUESTED.value
    assert jc2.match_score == 82
    assert jc2.judge_reason == "LLM 评分 82 通过"
    sent = [t for t in fake_queue.enqueued if t.job_candidate_id == jc.id]
    assert len(sent) == 1
    assert sent[0].type is AtomicTaskType.SEND_MESSAGE


def test_reenqueue_deferred_still_degraded_stays_new(client, fake_queue, fake_screening, session):
    """重判仍 degraded → status 保持 new，不产生 SEND_MESSAGE。"""
    job_id, _ = _create_job(client)
    liepin = f"LP{uuid4().hex[:12]}"
    candidate = models.Candidate(
        liepin_user_id=liepin,
        name="张伟",
        online_resume_minimal=_resume(liepin),
        source="inbound",
    )
    session.add(candidate)
    session.flush()
    jc = models.JobCandidate(
        job_id=job_id,
        candidate_id=candidate.id,
        status=CandidateStatus.NEW.value,
        judge_reason="deferred: LLM unavailable",
    )
    session.add(jc)
    _reload(session)

    fake_screening.set(liepin, DEGRADED_RESULT)
    resp = client.post("/internal/sweeps/deferred")
    assert resp.status_code == 200

    _reload(session)
    jc2 = session.get(models.JobCandidate, jc.id)
    assert jc2.status == CandidateStatus.NEW.value
    assert "deferred" in jc2.judge_reason
    assert [t for t in fake_queue.enqueued if t.job_candidate_id == jc.id] == []


# —— 回调契约边界 ——


class _CommitExplodingSession:
    """把 commit 变成必炸的会话包装：模拟 commit 失败 → 回滚（评审回归场景）。"""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def commit(self):
        self._inner.rollback()
        raise RuntimeError("simulated commit failure")


def _failing_commit_session():
    with SessionLocal() as s:
        yield _CommitExplodingSession(s)


def test_commit_failure_never_enqueues(client, fake_queue, fake_screening, session):
    """评审回归：commit 失败回滚 → 队列零 SEND_MESSAGE、库中无半截记录。

    入队必须发生在 commit 成功之后：入队前置会让 worker 在数据已回滚时仍向
    真实候选人发消息，且库中无 Interaction 行——一人一消息检查（决策 3）失效，
    之后重读该候选人会被再次放行发出第二条消息。
    """
    job_id, _ = _create_job(client)  # 正常会话建岗位
    liepin = f"LP{uuid4().hex[:12]}"
    fake_screening.set(liepin, _pass_result(82))

    read_task = _make_read_task(job_id, liepin)
    fake_queue.record(read_task)

    app.dependency_overrides[get_session] = _failing_commit_session
    try:
        with TestClient(app, raise_server_exceptions=False) as failing_client:
            resp = _post_read_result(failing_client, read_task, liepin)
        assert resp.status_code == 500
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert fake_queue.enqueued == []  # 回滚后队列零残留

    _reload(session)
    candidates = session.execute(
        select(models.Candidate).where(models.Candidate.liepin_user_id == liepin)
    ).scalars().all()
    assert candidates == []  # 事务整体回滚，无半截数据


def test_result_for_unknown_task_404(client):
    task_id = uuid4()
    resp = client.post(
        f"/internal/tasks/{task_id}/result",
        json={
            "task_id": str(task_id),
            "outcome": "success",
            "evidence": {},
            "error": None,
        },
    )
    assert resp.status_code == 404


def test_result_task_id_mismatch_422(client):
    task_id = uuid4()
    resp = client.post(
        f"/internal/tasks/{task_id}/result",
        json={"task_id": str(uuid4()), "outcome": "success", "evidence": {}, "error": None},
    )
    assert resp.status_code == 422


# —— 管理端点 ——


def test_jobs_create_and_list(client, session):
    title = f"产品经理-{uuid4().hex[:8]}"
    resp = client.post("/api/jobs", json={"title": title, "jd_text": "负责产品规划"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["llm_threshold"] == 70
    assert body["status"] == "active"
    assert body["hard_rules"] == {}
    assert body["template_msgs"] == {}
    assert body["created_at"] is not None

    listed = client.get("/api/jobs")
    assert listed.status_code == 200
    ids = {row["id"] for row in listed.json()}
    assert body["id"] in ids
    assert any(row["title"] == title for row in listed.json())


def test_artifact_snapshot_creates_candidate_if_missing(client, fake_queue, session, store):
    """artifact 先于 result 到达（D1 两回调无序）：snapshot 自建候选人与快照键。"""
    job_id, _ = _create_job(client)
    liepin = f"LP{uuid4().hex[:12]}"
    read_task = _make_read_task(job_id, liepin)
    fake_queue.record(read_task)

    png = b"\x89PNG\r\n\x1a\n" + uuid4().bytes
    resp = client.post(
        f"/internal/tasks/{read_task.task_id}/artifact",
        files={"file": ("snapshot.png", png, "image/png")},
        data={"task_id": str(read_task.task_id), "kind": "snapshot", "filename": "snapshot.png"},
    )
    assert resp.status_code == 200, resp.text

    _reload(session)
    candidate = session.execute(
        select(models.Candidate).where(models.Candidate.liepin_user_id == liepin)
    ).scalar_one()
    assert candidate.source == "inbound"
    assert candidate.snapshot_object_key is not None
    try:
        got = store.client.get_object(BUCKET, candidate.snapshot_object_key)
        assert got.read() == png
        got.close()
        got.release_conn()
    finally:
        store.client.remove_object(BUCKET, candidate.snapshot_object_key)


# —— 2026-10-06 策略：inbound 直索要 / outbound 打招呼索要 / 收到后补评分 ——


def test_outbound_uses_greet_variant_and_llm_scoring(
    client, fake_queue, fake_screening, session
):
    """outbound（recommended）：两层判定（llm_scoring=True）→ 打招呼索要 greet_request。"""
    job_id, title = _create_job(client)
    liepin = f"LP{uuid4().hex[:12]}"
    session.add(models.Candidate(liepin_user_id=liepin, name="张伟", source="recommended"))
    session.commit()
    fake_screening.set(liepin, _pass_result(82))

    read_task = _make_read_task(job_id, liepin)
    fake_queue.record(read_task)
    assert _post_read_result(client, read_task, liepin).status_code == 200
    assert fake_screening.requests[0].llm_scoring is True  # outbound 保持两层

    _reload(session)
    candidate = session.execute(
        select(models.Candidate).where(models.Candidate.liepin_user_id == liepin)
    ).scalar_one()
    assert candidate.source == "recommended"  # 预建行被复用
    jc = session.execute(
        select(models.JobCandidate).where(
            models.JobCandidate.job_id == job_id,
            models.JobCandidate.candidate_id == candidate.id,
        )
    ).scalar_one()
    assert jc.status == CandidateStatus.RESUME_REQUESTED.value
    assert jc.match_score == 82  # outbound 仍为前置评分
    assert jc.judge_reason == "LLM 评分 82 通过"
    (send_task,) = fake_queue.enqueued
    assert send_task.context["text"] == f"您好 张伟，看到您在看{title}岗位，方便发一份简历吗？"

    assert _post_send_result(client, send_task).status_code == 200
    _reload(session)
    interaction = session.execute(
        select(models.Interaction).where(models.Interaction.job_candidate_id == jc.id)
    ).scalar_one()
    assert interaction.msg_type == "greet_request"  # outbound 原通道


def test_inbound_post_receive_scoring_on_artifact(
    client, fake_queue, fake_screening, session, store
):
    """inbound 直索要：收到简历 artifact 后补评分（llm_scoring=True）落 match_score。"""
    job, candidate, jc = _make_awaiting_jc(session)  # source=inbound、minimal 已存
    fake_screening.set(candidate.liepin_user_id, _pass_result(82))

    dl_task = AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.DOWNLOAD_ATTACHMENT,
        job_id=job.id,
        job_candidate_id=jc.id,
        candidate_liepin_id=candidate.liepin_user_id,
        context={},
    )
    fake_queue.record(dl_task)
    resp = client.post(
        f"/internal/tasks/{dl_task.task_id}/artifact",
        files={"file": ("简历.pdf", b"%PDF-1.4 resume", "application/pdf")},
        data={"task_id": str(dl_task.task_id), "kind": "resume", "filename": "简历.pdf"},
    )
    assert resp.status_code == 200, resp.text

    _reload(session)
    jc2 = session.get(models.JobCandidate, jc.id)
    assert jc2.status == CandidateStatus.RESUME_RECEIVED.value
    assert jc2.match_score == 82  # 收到后补评分落账
    assert jc2.judge_reason == "LLM 评分 82 通过"
    assert fake_screening.requests[-1].llm_scoring is True  # 补评分调用带评分开关
    try:
        store.client.remove_object(BUCKET, jc2.minio_object_key)
    finally:
        pass


# —— 2026-10-06 分流：读→CHECK→（有简历：回执+入库 / 无：硬规则→直索要）——


def _drive_inbound_read_to_check(client, fake_queue, session, *, job_id, liepin):
    """读结果 → 返回 (read_task, candidate, jc, check_task)；断言分流第一步（不调 screening）。"""
    read_task = _make_read_task(job_id, liepin)
    fake_queue.record(read_task)
    assert _post_read_result(client, read_task, liepin).status_code == 200
    _reload(session)
    candidate = session.execute(
        select(models.Candidate).where(models.Candidate.liepin_user_id == liepin)
    ).scalar_one()
    jc = session.execute(
        select(models.JobCandidate).where(
            models.JobCandidate.job_id == job_id,
            models.JobCandidate.candidate_id == candidate.id,
        )
    ).scalar_one()
    checks = [t for t in fake_queue.enqueued if t.type is AtomicTaskType.CHECK_ATTACHMENT]
    assert len(checks) == 1
    return read_task, candidate, jc, checks[0]


def test_direct_intake_has_attachment_dispatches_ack_and_download(
    client, fake_queue, fake_screening, session
):
    """有简历分支：回执（resume_ack）+ DOWNLOAD 直入；零 screening；状态仍 new。"""
    job_id, title = _create_job(client)
    liepin = f"LP{uuid4().hex[:12]}"
    _, _, jc, check_task = _drive_inbound_read_to_check(
        client, fake_queue, session, job_id=job_id, liepin=liepin
    )

    resp = _post_check_attachment_result(client, check_task, has_attachment=True)
    assert resp.status_code == 200, resp.text
    _reload(session)

    rollout = [
        t
        for t in fake_queue.enqueued
        if t.type in (AtomicTaskType.SEND_MESSAGE, AtomicTaskType.DOWNLOAD_ATTACHMENT)
    ]
    assert [t.type for t in rollout] == [
        AtomicTaskType.SEND_MESSAGE,
        AtomicTaskType.DOWNLOAD_ATTACHMENT,
    ]
    ack, download = rollout
    assert ack.context["variant"] == "resume_ack"
    assert ack.context["text"] == f"您好 张伟，已收到您的简历，感谢关注！"
    assert download.job_candidate_id == jc.id
    assert fake_screening.requests == []  # 硬规则不拦收：直收路径零 screening
    assert session.get(models.JobCandidate, jc.id).status == CandidateStatus.NEW.value


def test_direct_intake_skips_ack_when_candidate_already_touched(
    client, fake_queue, fake_screening, session
):
    """候选人级一人一消息：该候选人已触达（跨岗位）→ 跳过回执，仍下载入库。"""
    job_id, _ = _create_job(client)
    liepin = f"LP{uuid4().hex[:12]}"
    _, _, jc, check_task = _drive_inbound_read_to_check(
        client, fake_queue, session, job_id=job_id, liepin=liepin
    )
    session.add(
        models.Interaction(
            job_candidate_id=jc.id,
            direction="out",
            msg_type="reply",
            content="历史触达（另一岗位）",
            sent_at=datetime.now(),
        )
    )
    session.commit()

    resp = _post_check_attachment_result(client, check_task, has_attachment=True)
    assert resp.status_code == 200, resp.text
    rollout = [
        t
        for t in fake_queue.enqueued
        if t.type in (AtomicTaskType.SEND_MESSAGE, AtomicTaskType.DOWNLOAD_ATTACHMENT)
    ]
    assert [t.type for t in rollout] == [AtomicTaskType.DOWNLOAD_ATTACHMENT]


def test_ack_result_records_reply_without_status_change(
    client, fake_queue, fake_screening, session
):
    """回执结果：interactions 落 out/reply + last_touch_at；生命周期状态不推进。"""
    job_id, _ = _create_job(client)
    liepin = f"LP{uuid4().hex[:12]}"
    _, _, jc, check_task = _drive_inbound_read_to_check(
        client, fake_queue, session, job_id=job_id, liepin=liepin
    )
    assert _post_check_attachment_result(client, check_task, has_attachment=True).status_code == 200
    (ack,) = [t for t in fake_queue.enqueued if t.type is AtomicTaskType.SEND_MESSAGE]

    assert _post_send_result(client, ack).status_code == 200
    _reload(session)
    jc2 = session.get(models.JobCandidate, jc.id)
    assert jc2.status == CandidateStatus.NEW.value  # 回执不推进状态
    assert jc2.last_touch_at is not None
    interactions = (
        session.execute(
            select(models.Interaction).where(models.Interaction.job_candidate_id == jc.id)
        )
        .scalars()
        .all()
    )
    assert [(r.direction, r.msg_type) for r in interactions] == [("out", "reply")]


def test_direct_intake_artifact_from_new_reaches_resume_received(
    client, fake_queue, fake_screening, session, store
):
    """直收入库：artifact 从 new → resume_received + 收到后补评分落账。"""
    job_id, _ = _create_job(client)
    liepin = f"LP{uuid4().hex[:12]}"
    fake_screening.set(liepin, _pass_result(82))
    _, _, jc, check_task = _drive_inbound_read_to_check(
        client, fake_queue, session, job_id=job_id, liepin=liepin
    )
    assert _post_check_attachment_result(client, check_task, has_attachment=True).status_code == 200
    (download,) = [
        t for t in fake_queue.enqueued if t.type is AtomicTaskType.DOWNLOAD_ATTACHMENT
    ]

    resp = client.post(
        f"/internal/tasks/{download.task_id}/artifact",
        files={"file": ("简历.pdf", b"%PDF-1.4 resume", "application/pdf")},
        data={"task_id": str(download.task_id), "kind": "resume", "filename": "简历.pdf"},
    )
    assert resp.status_code == 200, resp.text
    _reload(session)
    jc2 = session.get(models.JobCandidate, jc.id)
    assert jc2.status == CandidateStatus.RESUME_RECEIVED.value
    assert jc2.resume_downloaded_at is not None
    assert jc2.match_score == 82  # 收到后补评分
    assert fake_screening.requests[-1].llm_scoring is True
    interaction = (
        session.execute(
            select(models.Interaction).where(models.Interaction.job_candidate_id == jc.id)
        )
        .scalars()
        .one()
    )  # 回执未执行（未投递 send 结果）→ 仅 in/attachment
    assert (interaction.direction, interaction.msg_type) == ("in", "attachment")
    try:
        store.client.remove_object(BUCKET, jc2.minio_object_key)
    finally:
        pass
