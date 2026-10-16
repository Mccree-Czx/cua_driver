"""HR 面接口（`/api/hr/*`）。

端点清单照猎聘的 `hr_api.py`，只删掉账号那一路。这里重点钉住几件
**BOSS 与猎聘不一样**的事：

- 候选人按 **jc 维度**返回（BOSS 的牛人推荐按职位）
- `review` 的语义是**人工判定**，不是「复核机器打分」（BOSS 还没打分链路）
- `manual-queue` 在 BOSS 上**有真内容**（掉登录/采集失败）
- 改职位**只能改系统侧两样**（status / jd_text）——BOSS 的职位名/城市发布后不可改
"""

import pytest

from app import hr_tools, models


@pytest.fixture()
def seeded(client, session, fake_redis):
    """一个能用的职位 + 一个候选人 + 一条 jc。"""
    client.post("/api/ingest/jobs", json=[{
        "title": "高级大数据开发工程师", "state": "开放中",
        "details": "上海·四川北路 ｜ 本科 ｜ 3-5年 ｜ 18-25K·13薪 ｜ 全职",
        "jd_text": "负责大数据平台研发……", "jd_complete": True,
        "tags": ["PostgreSQL"],
    }])
    client.post("/api/ingest/candidates", json=[{
        "boss_key": "张雄瑜｜云南白药集团·数据开发", "name": "张雄瑜",
        "job_title": "高级大数据开发工程师",
        "job_and_company": "云南白药集团·数据开发",
        "work_year": "9年", "degree": "本科", "age": "32",
        "summary": "9年 ｜ 本科 ｜ 面议", "description": "多年大数据平台研发经验",
        "tags": ["数据建模"],
    }])
    job = session.query(models.Job).one()
    cand = session.query(models.Candidate).one()
    jc = hr_tools.jc_get_or_create(session, job.id, cand.id)
    session.commit()
    return job, cand, jc


# ——— 驾驶舱 ———

def test_overview_shape(client, seeded):
    ov = client.get("/api/hr/overview").json()
    assert ov["jobs"]["total"] == 1
    assert ov["jobs"]["usable"] == 1
    assert ov["candidates"] == 1
    assert ov["jc"]["total"] == 1
    assert ov["greeted"] == 0
    assert "quota_cap" in ov["today"]


def test_usable_is_stricter_than_open(client, session, seeded):
    """★ 「可用」比「开放中」严——JD 没读全就不算可用。"""
    job, _, _ = seeded
    job.jd_complete = False
    session.commit()
    ov = client.get("/api/hr/overview").json()
    assert ov["jobs"]["开放中"] == 1
    assert ov["jobs"]["usable"] == 0


# ——— 候选人（jc 维度）———

def test_candidates_are_jc_scoped(client, seeded):
    job, _, jc = seeded
    body = client.get("/api/hr/candidates").json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["id"] == jc.id
    assert row["job_title"] == job.title, "jc 维度必须带职位"
    assert row["status"] == "new"

    # 按职位过滤
    assert client.get(f"/api/hr/candidates?job_id={job.id}").json()["total"] == 1
    assert client.get("/api/hr/candidates?job_id=9999").json()["total"] == 0
    # 按状态过滤
    assert client.get("/api/hr/candidates?status=greeted").json()["total"] == 0


def test_candidate_detail_has_interactions_and_overrides(client, session, seeded):
    _, _, jc = seeded
    hr_tools.interaction_log(session, jc, "out", "greet", "您好")

    detail = client.get(f"/api/hr/candidates/{jc.id}").json()
    assert detail["description"] == "多年大数据平台研发经验"
    assert [i["msg_type"] for i in detail["interactions"]] == ["greet"]
    assert detail["overrides"] == []


def test_candidate_404(client):
    assert client.get("/api/hr/candidates/9999").status_code == 404


# ——— 人工复核 ———

def test_review_approve_then_reject(client, session, seeded):
    _, _, jc = seeded
    r = client.post(f"/api/hr/candidates/{jc.id}/review",
                    json={"decision": "approve", "note": "人工看过，合适"})
    assert r.status_code == 200 and r.json()["status"] == "hr_reviewed"

    # ★ 复核流水要落下来（照猎聘）
    rows = session.query(models.ReviewOverride).all()
    assert len(rows) == 1
    assert (rows[0].old_status, rows[0].new_status) == ("new", "hr_reviewed")
    assert rows[0].reason == "人工看过，合适"

    r = client.post(f"/api/hr/candidates/{jc.id}/review",
                    json={"decision": "reject", "note": "面试后推翻"})
    assert r.status_code == 200 and r.json()["status"] == "closed"
    session.expire_all()
    assert session.query(models.ReviewOverride).count() == 2


def test_review_rejects_bad_input(client, seeded):
    _, _, jc = seeded
    assert client.post(f"/api/hr/candidates/{jc.id}/review",
                       json={"decision": "maybe"}).status_code == 422
    assert client.post("/api/hr/candidates/9999/review",
                       json={"decision": "approve"}).status_code == 404


def test_review_cannot_leave_terminal(client, seeded):
    """★ 终态锁：closed 之后不能再改。"""
    _, _, jc = seeded
    client.post(f"/api/hr/candidates/{jc.id}/review", json={"decision": "reject"})
    r = client.post(f"/api/hr/candidates/{jc.id}/review", json={"decision": "approve"})
    assert r.status_code == 409


# ——— 会话 ———

def test_conversations_and_messages(client):
    client.post("/api/ingest/conversations", json=[{
        "boss_name": "张雄瑜", "job_title": "高级大数据开发工程师",
        "last_msg": "您好，有兴趣的话可以发一份简历看看。",
        "status": "[送达]", "boss_time": "16:36", "unread": 2,
        "messages": [
            {"direction": "out", "text": "您好，有兴趣的话可以发一份简历看看。", "boss_y": 1064},
            {"direction": "in", "text": "好的", "boss_y": 1300},
        ],
    }])
    body = client.get("/api/hr/conversations").json()
    assert body["total"] == 1

    cid = body["items"][0]["id"]
    msgs = client.get(f"/api/hr/conversations/{cid}").json()["messages"]
    assert [m["direction"] for m in msgs] == ["out", "in"], "应按 boss_y 排先后"

    assert client.get("/api/hr/conversations?unread_only=true").json()["total"] == 1
    assert client.get("/api/hr/conversations?q=不存在的人").json()["total"] == 0
    assert client.get("/api/hr/conversations/9999").status_code == 404


# ——— 人工队列 ————

def test_manual_queue_and_ack(client, session):
    """★ BOSS 上这个队列有真内容：掉登录、采集失败、轮次超时。"""
    hr_tools.tasklog_add(session, task_id="t-1", round_name="main_round",
                         outcome="failed_needs_manual", note="手机停在人脸认证页")
    hr_tools.tasklog_add(session, task_id="t-2", round_name="main_round",
                         outcome="success", note="")

    body = client.get("/api/hr/manual-queue").json()
    assert body["total"] == 1
    assert body["items"][0]["note"] == "手机停在人脸认证页"

    r = client.post("/api/hr/manual-queue/t-1/ack", json={"note": "已经过了人脸"})
    assert r.status_code == 200
    session.expire_all()
    assert client.get("/api/hr/manual-queue").json()["total"] == 0
    row = session.query(models.TaskLog).filter_by(task_id="t-1").one()
    assert row.outcome == "acked"
    assert "已经过了人脸" in row.note, "ack 要留痕，不是删行"

    assert client.post("/api/hr/manual-queue/不存在/ack", json={}).status_code == 404


def test_tasklogs(client, session):
    hr_tools.tasklog_add(session, task_id="t-9", round_name="daily_round",
                         outcome="success", tokens=1234, cost=0.02, duration=12.5)
    body = client.get("/api/hr/tasklogs").json()
    assert body["items"][0]["tokens"] == 1234
    assert body["items"][0]["round_name"] == "daily_round"


# ——— 告警 ———

def test_alerts_reports_risk_and_quota(client, seeded):
    body = client.get("/api/hr/alerts").json()
    assert body["risk_paused"] is False
    assert body["quota_used_today"] == 0
    assert body["quota_cap"] > 0
    assert body["login_gate_in_flight"] is False


def test_alerts_shows_risk_reason(client, seeded):
    hr_tools.risk_set("撞到易盾滑块")
    body = client.get("/api/hr/alerts").json()
    assert body["risk_paused"] is True
    assert "易盾滑块" in body["risk_reason"]


def test_alerts_shows_login_gate(client, seeded):
    """★ BOSS 特有：手机停在风控关卡前等人时必须显眼——否则人只会觉得「怎么不动了」。"""
    from app import keys
    hr_tools.redis_client().set(keys.login_gate_in_flight(), "1")
    assert client.get("/api/hr/alerts").json()["login_gate_in_flight"] is True


# ——— 日报 ———

def test_daily(client, seeded):
    body = client.get("/api/hr/daily?days=7").json()
    assert len(body["days"]) == 7
    assert set(body["totals"]) == {"messages_in", "messages_out", "greeted"}


# ——— 评分偏好（保留形状，暂无消费者）———

def test_scoring_list_and_patch(client, session, seeded):
    job, _, _ = seeded
    body = client.get("/api/hr/scoring").json()
    assert body["items"][0]["job_id"] == job.id
    assert body["items"][0]["scoring_prefs"] == {}

    r = client.patch(f"/api/hr/scoring/{job.id}",
                     json={"scoring_prefs": {"min_stars": 3, "veto": ["外包"]}})
    assert r.status_code == 200
    session.expire_all()
    assert client.get("/api/hr/scoring").json()["items"][0]["scoring_prefs"]["min_stars"] == 3

    assert client.patch("/api/hr/scoring/9999", json={"scoring_prefs": {}}).status_code == 404


# ——— 改职位（只能改系统侧两样）———

def test_patch_job_status(client, session, seeded):
    job, _, _ = seeded
    r = client.patch(f"/api/hr/jobs/{job.id}", json={"status": "draft"})
    assert r.status_code == 200 and r.json()["status"] == "draft"
    session.expire_all()
    assert hr_tools.job_list_active(session) == [], "停用之后不该还在可用列表里"


def test_patch_job_jd_marks_complete(client, session, seeded):
    """人工补录的 JD 一律当作「读全了」——它就是人写进去的全文。"""
    job, _, _ = seeded
    client.patch(f"/api/hr/jobs/{job.id}", json={"jd_text": "人工补的完整 JD"})
    session.expire_all()
    job = session.get(models.Job, job.id)
    assert job.jd_complete is True
    assert job.jd_text == "人工补的完整 JD"


def test_patch_job_rejects_unknown_status(client, seeded):
    job, _, _ = seeded
    assert client.patch(f"/api/hr/jobs/{job.id}",
                        json={"status": "乱写"}).status_code == 422
    assert client.patch("/api/hr/jobs/9999", json={"status": "draft"}).status_code == 404
