"""HR 面 API 验收测试（M3 最小可用 + M4 可观测）。

真实 MySQL/MinIO + FakeTaskQueue（与 test_api_task_results 同模式）：
- overview / candidates / 详情（预签名 + 时间线）
- review 复核推翻（OVERRIDE + 流水）/ rerun 手动重跑（任务映射与守卫）
- daily 日报 / manual-queue + ack / alerts / PATCH jobs（阈值回流）
"""

from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import models
from app.deps import get_login_state, get_queue
from app.main import app
from app.storage import ObjectStore
from fakes import FakeTaskQueue
from hr_workbuddy import AtomicTask, AtomicTaskType

BUCKET = "hr-workbuddy"


class FakeLoginState:
    def __init__(self, payload=None):
        self._payload = payload

    def get(self):
        return self._payload


@pytest.fixture()
def fake_queue():
    return FakeTaskQueue()


@pytest.fixture()
def client(fake_queue):
    app.dependency_overrides[get_queue] = lambda: fake_queue
    app.dependency_overrides[get_login_state] = lambda: FakeLoginState(
        {"is_login": True, "checked_at": "2026-10-07T09:00:00", "task_id": None}
    )
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


def _new_job(client) -> int:
    resp = client.post(
        "/api/jobs", json={"title": f"HR-{uuid4().hex[:6]}", "jd_text": "jd"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _seed_jc(
    session,
    job_id: int,
    *,
    status: str,
    score=None,
    name=None,
    snapshot_key=None,
    pdf_key=None,
) -> models.JobCandidate:
    candidate = models.Candidate(
        liepin_user_id=f"LP{uuid4().hex[:10]}",
        name=name or f"候选人{uuid4().hex[:4]}",
        source="inbound",
        online_resume_minimal={},
        snapshot_object_key=snapshot_key,
    )
    session.add(candidate)
    session.flush()
    jc = models.JobCandidate(job_id=job_id, candidate_id=candidate.id, status=status)
    jc.match_score = score
    jc.minio_object_key = pdf_key
    jc.judge_reason = "测试理由"
    session.add(jc)
    session.commit()
    return jc


def _add_interaction(session, jc_id: int, direction: str, msg_type: str, content="x"):
    row = models.Interaction(
        job_candidate_id=jc_id,
        direction=direction,
        msg_type=msg_type,
        content=content,
        sent_at=datetime.now(),
    )
    session.add(row)
    session.commit()
    return row


# —— 只读端点 ——


def test_overview_counts_buckets_and_today(client, session):
    job_id = _new_job(client)
    jc_a = _seed_jc(session, job_id, status="new")
    jc_b = _seed_jc(session, job_id, status="resume_received", score=55)
    _seed_jc(session, job_id, status="closed", score=82)
    _add_interaction(session, jc_a.id, "out", "direct_request")
    _add_interaction(session, jc_b.id, "in", "attachment")

    body = client.get(f"/api/hr/overview?job_id={job_id}").json()
    assert body["job"]["llm_threshold"] == 40
    assert body["status_counts"] == {"new": 1, "resume_received": 1, "closed": 1}
    assert body["score_buckets"] == {"1星": 0, "2星": 1, "3星": 0, "4星": 1, "5星": 0, "未评分": 1}
    assert body["today"]["touches_out"] == 1  # 岗位维度计数
    assert body["today"]["received"] == 1
    assert body["today"]["manual"] >= 0  # 全局口径（其他用例可能已产生转人工落账）


def test_candidates_filter_search_and_paginate(client, session):
    job_id = _new_job(client)
    _seed_jc(session, job_id, status="resume_received", name="张三丰")
    _seed_jc(session, job_id, status="resume_received", name="李四光")
    _seed_jc(session, job_id, status="closed", name="王五")

    body = client.get(f"/api/hr/candidates?job_id={job_id}&status=resume_received").json()
    assert body["total"] == 2
    assert {item["name"] for item in body["items"]} == {"张三丰", "李四光"}

    body = client.get(f"/api/hr/candidates?job_id={job_id}&q=张三").json()
    assert body["total"] == 1 and body["items"][0]["name"] == "张三丰"

    body = client.get(f"/api/hr/candidates?job_id={job_id}&limit=1&offset=1").json()
    assert body["total"] == 3 and len(body["items"]) == 1


def test_candidate_detail_presigned_urls_and_timeline(client, session):
    job_id = _new_job(client)
    jc = _seed_jc(
        session,
        job_id,
        status="resume_received",
        snapshot_key=f"snapshots/{uuid4().hex}/20261007_090000.png",
        pdf_key=f"resumes/{job_id}/{uuid4().hex}/a.pdf",
    )
    _add_interaction(session, jc.id, "out", "direct_request", "您好")
    _add_interaction(session, jc.id, "in", "attachment", "resumes/a.pdf")

    body = client.get(f"/api/hr/candidates/{jc.id}").json()
    assert body["snapshot_url"] and body["snapshot_url"].startswith("http")
    assert body["resume_url"] and body["resume_url"].startswith("http")
    assert [r["msg_type"] for r in body["interactions"]] == ["direct_request", "attachment"]
    assert body["candidate"]["online_resume_minimal"] == {}

    assert client.get("/api/hr/candidates/999999").status_code == 404


def test_daily_report_funnel_and_cost(client, session):
    job_id = _new_job(client)
    before = client.get("/api/hr/daily?days=1").json()["totals"]
    jc = _seed_jc(session, job_id, status="awaiting_resume")
    _add_interaction(session, jc.id, "out", "direct_request")
    _add_interaction(session, jc.id, "in", "attachment")
    session.add(
        models.TaskLog(
            task_id=str(uuid4()), outcome="success", attempt=0, tokens=100, cost=0.01
        )
    )
    session.add(
        models.TaskLog(
            task_id=str(uuid4()), outcome="failed_needs_manual", attempt=0, note="风控"
        )
    )
    session.commit()

    body = client.get("/api/hr/daily?days=1").json()
    assert len(body["days"]) == 1
    after = body["totals"]
    assert after["requests"] == before["requests"] + 1
    assert after["received"] == before["received"] + 1
    assert after["tokens"] >= before["tokens"] + 100
    assert after["manual"] == before["manual"] + 1
    day = body["days"][0]
    assert day["conversion"] is not None  # 有请求→有转化率口径


# —— 动作端点 ——


def test_review_approve_reject_and_guards(client, session):
    job_id = _new_job(client)
    jc = _seed_jc(session, job_id, status="resume_received")

    resp = client.post(
        f"/api/hr/candidates/{jc.id}/review",
        json={"decision": "approve", "note": "学历核验无误"},
    )
    assert resp.status_code == 200 and resp.json()["status"] == "hr_reviewed"
    session.commit()  # 结束 RR 快照，读取 API 侧新写入的流水
    rows = session.query(models.ReviewOverride).filter_by(job_candidate_id=jc.id).all()
    assert len(rows) == 1
    assert (rows[0].old_status, rows[0].new_status) == ("resume_received", "hr_reviewed")
    assert rows[0].reason == "学历核验无误"

    resp = client.post(
        f"/api/hr/candidates/{jc.id}/review", json={"decision": "reject", "note": "面试后推翻"}
    )
    assert resp.status_code == 200 and resp.json()["status"] == "closed"
    session.commit()  # 再次结束 RR 快照（第二次写入）
    assert session.query(models.ReviewOverride).filter_by(job_candidate_id=jc.id).count() == 2

    assert (
        client.post(
            f"/api/hr/candidates/{jc.id}/review", json={"decision": "approve"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/hr/candidates/999999/review", json={"decision": "approve"}
        ).status_code
        == 404
    )


def test_patch_job_threshold_reflow(client, session):
    job_id = _new_job(client)
    resp = client.patch(f"/api/hr/jobs/{job_id}", json={"llm_threshold": 85})
    assert resp.status_code == 200 and resp.json()["llm_threshold"] == 85
    assert client.patch(f"/api/hr/jobs/{job_id}", json={"llm_threshold": 120}).status_code == 422
    assert client.patch("/api/hr/jobs/999999", json={"llm_threshold": 80}).status_code == 404


# —— M4：人工队列 / 告警 ——


def test_manual_queue_and_ack(client, fake_queue, session):
    job_id = _new_job(client)
    jc = _seed_jc(session, job_id, status="awaiting_resume", name="待处理者")
    task = AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.CHECK_ATTACHMENT,
        job_id=job_id,
        job_candidate_id=jc.id,
        candidate_liepin_id="LPx",
        context={},
    )
    fake_queue.record(task)
    session.add(
        models.TaskLog(task_id=str(task.task_id), outcome="failed_needs_manual", note="风控拦截")
    )
    session.commit()

    body = client.get("/api/hr/manual-queue").json()
    assert body["total"] >= 1  # 全局队列：其他用例可能已产生落账
    item = next(i for i in body["items"] if i["task_id"] == str(task.task_id))
    assert item["task_type"] == "check_attachment"
    assert item["candidate_name"] == "待处理者" and item["jc_status"] == "awaiting_resume"

    resp = client.post(f"/api/hr/manual-queue/{task.task_id}/ack", json={"note": "已人工确认"})
    assert resp.status_code == 200
    session.commit()  # 结束 RR 快照，重读 note
    log = session.query(models.TaskLog).filter_by(task_id=str(task.task_id)).one()
    assert "已人工确认(acked)" in (log.note or "")
    assert client.post("/api/hr/manual-queue/xx/ack", json={}).status_code == 404


def test_alerts_minimal(client):
    body = client.get("/api/hr/alerts").json()
    assert body["login"] == {
        "is_login": True,
        "checked_at": "2026-10-07T09:00:00",
        "task_id": None,
    }
    assert body["risk_paused"] is False
    assert body["quota_used_today"] >= 0
    assert body["manual_today"] >= 0
