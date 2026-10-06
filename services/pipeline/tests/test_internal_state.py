"""R9/R10：LIST_UNREAD / CHECK_LOGIN 结果处理器 + 登录态/配额/awaiting 内部端点
+ 通用 TaskLog 落账。

真实 MySQL（测试库）+ 真实 MinIO；queue / screening / login_state 经
dependency_overrides 注入内存替身（与 test_api_task_results.py 同模式）。
"""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models
from app.deps import get_login_state, get_queue, get_screening
from app.main import app
from fakes import FakeScreening, FakeTaskQueue
from hr_workbuddy import AtomicTask, AtomicTaskType, CandidateStatus

GREET_TEMPLATE = "您好 {name}，看到您在看{title}岗位，方便发一份简历吗？"


class FakeLoginState:
    """登录态内存替身（生产对应 RedisLoginState：pipeline:state:login 键）。"""

    def __init__(self) -> None:
        self.state: dict | None = None

    def get(self) -> dict | None:
        return self.state

    def set(self, is_login: bool, task_id: str, checked_at: str) -> None:
        self.state = {"is_login": is_login, "task_id": task_id, "checked_at": checked_at}


@pytest.fixture()
def fake_queue():
    return FakeTaskQueue()


@pytest.fixture()
def fake_screening():
    return FakeScreening()


@pytest.fixture()
def fake_login_state():
    return FakeLoginState()


@pytest.fixture()
def client(fake_queue, fake_screening, fake_login_state):
    app.dependency_overrides[get_queue] = lambda: fake_queue
    app.dependency_overrides[get_screening] = lambda: fake_screening
    app.dependency_overrides[get_login_state] = lambda: fake_login_state
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _create_job(client) -> int:
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
    return resp.json()["id"]


def _reload(session):
    """API 调用后：提交本会话挂起变更 + 清空身份映射（MySQL RR 快照要求）。"""
    session.commit()
    session.expire_all()


def _record_and_post(client, fake_queue, task: AtomicTask, evidence: dict) -> TestClient:
    fake_queue.record(task)
    resp = client.post(
        f"/internal/tasks/{task.task_id}/result",
        json={
            "task_id": str(task.task_id),
            "outcome": "success",
            "evidence": evidence,
            "error": None,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp


# —— LIST_UNREAD 结果处理器（R9）——


def test_list_unread_creates_candidates_jcs_and_read_tasks(client, fake_queue, session):
    """每个未读会话：幂等建 Candidate（inbound）+ job_candidate（new）→ READ_RESUME。"""
    job_id = _create_job(client)
    suffix = uuid4().hex[:8]
    lp_a, lp_b = f"LP-{suffix}-A", f"LP-{suffix}-B"
    task = AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.LIST_UNREAD,
        job_id=job_id,
        job_candidate_id=None,
        candidate_liepin_id=None,
        context={},
    )
    _record_and_post(
        client,
        fake_queue,
        task,
        {"unread_ids": [lp_a, lp_b], "brain_tokens": 10, "cost_est": 1},
    )

    _reload(session)
    candidates = session.execute(
        select(models.Candidate)
        .where(models.Candidate.liepin_user_id.in_([lp_a, lp_b]))
        .order_by(models.Candidate.id)
    ).scalars().all()
    assert [c.liepin_user_id for c in candidates] == [lp_a, lp_b]
    assert all(c.source == "inbound" for c in candidates)
    assert all(c.name == c.liepin_user_id for c in candidates)  # 姓名未知 → 占位

    jcs = session.execute(
        select(models.JobCandidate)
        .where(models.JobCandidate.candidate_id.in_([c.id for c in candidates]))
        .order_by(models.JobCandidate.id)
    ).scalars().all()
    assert len(jcs) == 2
    assert all(jc.job_id == job_id for jc in jcs)
    assert all(jc.status == CandidateStatus.NEW.value for jc in jcs)

    assert len(fake_queue.enqueued) == 2
    read_tasks = fake_queue.enqueued
    assert all(t.type is AtomicTaskType.READ_RESUME for t in read_tasks)
    assert {t.candidate_liepin_id for t in read_tasks} == {lp_a, lp_b}
    assert all(t.job_id == job_id for t in read_tasks)
    # jc 已建档 → READ_RESUME 带 job_candidate_id（溯源 + 可参与在途去重）
    by_liepin = {t.candidate_liepin_id: t for t in read_tasks}
    by_jc = {c.liepin_user_id: jc.id for c, jc in zip(candidates, jcs)}
    assert by_liepin[lp_a].job_candidate_id == by_jc[lp_a]
    assert by_liepin[lp_b].job_candidate_id == by_jc[lp_b]


def test_list_unread_duplicate_result_idempotent(client, fake_queue, session):
    """重复 LIST_UNREAD 结果（同 liepin_user_id）：不重复建档、不重复入队 READ_RESUME。"""
    job_id = _create_job(client)
    suffix = uuid4().hex[:8]
    lp_a, lp_c = f"LP-{suffix}-A", f"LP-{suffix}-C"
    task = AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.LIST_UNREAD,
        job_id=job_id,
        job_candidate_id=None,
        candidate_liepin_id=None,
        context={},
    )
    _record_and_post(client, fake_queue, task, {"unread_ids": [lp_a]})
    assert len(fake_queue.enqueued) == 1

    # 第二轮结果仍含同一未读 id（在途 READ_RESUME 尚未完成）→ 不重复入队
    task2 = AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.LIST_UNREAD,
        job_id=job_id,
        job_candidate_id=None,
        candidate_liepin_id=None,
        context={},
    )
    _record_and_post(client, fake_queue, task2, {"unread_ids": [lp_a, lp_c]})

    _reload(session)
    candidates = session.execute(
        select(models.Candidate)
        .where(models.Candidate.liepin_user_id.in_([lp_a, lp_c]))
        .order_by(models.Candidate.id)
    ).scalars().all()
    assert [c.liepin_user_id for c in candidates] == [lp_a, lp_c]  # 幂等建行
    jcs = session.execute(
        select(models.JobCandidate)
        .where(models.JobCandidate.candidate_id.in_([c.id for c in candidates]))
    ).scalars().all()
    assert len(jcs) == 2
    # lp_a 已有 jc（在途 READ_RESUME）→ 只新增 lp_c 的 READ_RESUME
    assert len(fake_queue.enqueued) == 2
    assert [t.candidate_liepin_id for t in fake_queue.enqueued] == [lp_a, lp_c]


def test_list_unread_invalid_evidence_422(client, fake_queue):
    job_id = _create_job(client)
    task = AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.LIST_UNREAD,
        job_id=job_id,
        job_candidate_id=None,
        candidate_liepin_id=None,
        context={},
    )
    fake_queue.record(task)
    resp = client.post(
        f"/internal/tasks/{task.task_id}/result",
        json={
            "task_id": str(task.task_id),
            "outcome": "success",
            "evidence": {"unread_ids": "not-a-list"},
            "error": None,
        },
    )
    assert resp.status_code == 422


# —— CHECK_LOGIN 结果处理器 + GET /internal/state/login（R9）——


def test_check_login_result_records_state(client, fake_queue, fake_login_state):
    """logged_in=False → 登录态落 store，GET /internal/state/login 可取；再查为 True 覆盖。"""
    task = AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.CHECK_LOGIN,
        job_id=0,  # 哨兵：无岗位
        job_candidate_id=None,
        candidate_liepin_id=None,
        context={},
    )
    _record_and_post(client, fake_queue, task, {"logged_in": False, "brain_tokens": 2})

    resp = client.get("/internal/state/login")
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_login"] is False
    assert body["task_id"] == str(task.task_id)
    assert body["checked_at"] is not None

    task2 = AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.CHECK_LOGIN,
        job_id=0,
        job_candidate_id=None,
        candidate_liepin_id=None,
        context={},
    )
    _record_and_post(client, fake_queue, task2, {"logged_in": True})
    body2 = client.get("/internal/state/login").json()
    assert body2["is_login"] is True
    assert body2["task_id"] == str(task2.task_id)


def test_login_state_before_any_check(client):
    """从未检查过 → 200 且三字段为 null（scheduler 视为未知，不动暂停）。"""
    resp = client.get("/internal/state/login")
    assert resp.status_code == 200
    assert resp.json() == {"is_login": None, "checked_at": None, "task_id": None}


def test_check_login_invalid_evidence_422(client, fake_queue):
    task = AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.CHECK_LOGIN,
        job_id=0,
        job_candidate_id=None,
        candidate_liepin_id=None,
        context={},
    )
    fake_queue.record(task)
    resp = client.post(
        f"/internal/tasks/{task.task_id}/result",
        json={
            "task_id": str(task.task_id),
            "outcome": "success",
            "evidence": {"logged_in": "yes"},
            "error": None,
        },
    )
    assert resp.status_code == 422


# —— R10：通用 TaskLog 落账 ——


def test_result_logs_generic_tasklog_row(client, fake_queue, session):
    """每次 result 回调落一行 TaskLog（task_id/outcome/attempt/tokens/cost/duration）。"""
    suffix = uuid4().hex[:12]
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
        resume_requested_at=datetime.now(),
    )
    session.add(jc)
    _reload(session)

    check_task = AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.CHECK_ATTACHMENT,
        job_id=job.id,
        job_candidate_id=jc.id,
        candidate_liepin_id=candidate.liepin_user_id,
        context={},
    )
    _record_and_post(
        client,
        fake_queue,
        check_task,
        {"has_attachment": False, "brain_tokens": 7, "cost_est": 3, "duration_s": 1.5},
    )

    _reload(session)
    logs = session.execute(
        select(models.TaskLog).where(models.TaskLog.task_id == str(check_task.task_id))
    ).scalars().all()
    assert len(logs) == 1  # 恰一行
    log = logs[0]
    assert log.outcome == "success"
    assert log.attempt == 0
    assert log.tokens == 7
    assert log.cost == 3
    assert log.duration == 1.5


# —— 内部端点：配额 / awaiting ——


def test_quota_today_counts_only_today_out(client, session):
    """GET /internal/quota/today：当日 out 计数 = 基线上 + 本次插入的 2（昨日 out、当日 in 不计）。"""
    suffix = uuid4().hex[:12]
    job = models.Job(title=f"产品经理-{suffix}", jd_text="负责产品规划与迭代")
    candidate = models.Candidate(
        liepin_user_id=f"LP{suffix}", name="张伟", online_resume_minimal={}, source="inbound"
    )
    session.add_all([job, candidate])
    session.flush()
    jc = models.JobCandidate(job_id=job.id, candidate_id=candidate.id)
    session.add(jc)
    session.flush()
    for _ in range(2):
        session.add(
            models.Interaction(
                job_candidate_id=jc.id, direction="out", msg_type="greet_request",
                content="您好", sent_at=datetime.now(),
            )
        )
    session.add(
        models.Interaction(
            job_candidate_id=jc.id, direction="out", msg_type="greet_request",
            content="昨日消息", sent_at=datetime.now() - timedelta(days=1),
        )
    )
    session.add(
        models.Interaction(
            job_candidate_id=jc.id, direction="in", msg_type="attachment",
            content="今日附件", sent_at=datetime.now(),
        )
    )
    _reload(session)
    expected = session.execute(
        select(models.Interaction).where(
            models.Interaction.direction == "out",
            models.Interaction.sent_at >= datetime.now().replace(hour=0, minute=0, second=0, microsecond=0),
        )
    ).scalars().all()

    resp = client.get("/internal/quota/today")
    assert resp.status_code == 200
    body = resp.json()
    # 端点数出的当日 out == 库中全部当日 out（本次插入 2 条在内）；
    # 若误计昨日 out / 当日 in，计数会比 expected 多
    assert body["count"] == len(expected)
    assert body["date"] == datetime.now().date().isoformat()


def test_awaiting_lists_only_awaiting_resume(client, session):
    """GET /internal/state/awaiting：只返回 awaiting_resume 的 jc（id/job_id/liepin）；
    同一候选人的 new 状态 jc 不在列表。"""
    suffix = uuid4().hex[:12]
    liepin = f"LP{suffix}"
    job = models.Job(title=f"产品经理-{suffix}", jd_text="负责产品规划与迭代")
    candidate = models.Candidate(
        liepin_user_id=liepin, name="张伟", online_resume_minimal={}, source="inbound"
    )
    session.add_all([job, candidate])
    session.flush()
    awaiting = models.JobCandidate(
        job_id=job.id,
        candidate_id=candidate.id,
        status=CandidateStatus.AWAITING_RESUME.value,
        resume_requested_at=datetime.now(),
    )
    other_candidate = models.Candidate(
        liepin_user_id=f"LP-OTHER-{suffix}", name="李四", online_resume_minimal={}, source="inbound"
    )
    session.add(other_candidate)
    session.flush()
    other = models.JobCandidate(job_id=job.id, candidate_id=other_candidate.id)
    session.add_all([awaiting, other])
    _reload(session)

    resp = client.get("/internal/state/awaiting")
    assert resp.status_code == 200
    rows = resp.json()
    mine = [r for r in rows if r["candidate_liepin_id"] == liepin]
    assert len(mine) == 1  # 本候选人的 awaiting jc 在列
    row = mine[0]
    assert row["id"] == awaiting.id
    assert row["job_id"] == job.id
    assert all(r["candidate_liepin_id"] != f"LP-OTHER-{suffix}" for r in rows)  # new 状态不在列
