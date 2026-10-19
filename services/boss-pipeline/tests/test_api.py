"""boss-pipeline 接口验收。

重点钉住三件**会被真机数据毒到**的事：

1. **半截 JD 不能覆盖好 JD**——一次失败的抓取不能把已经读全的 JD 冲掉
2. **消息去重**——BOSS 不给消息 id，每次同步都会把同一批消息再推一遍
3. **空值不能盖掉已有值**——列表页推上来的候选人缺详情字段，不能把详情页采到的抹掉
"""


def _ingest_job(client, **over) -> None:
    body = {"title": "高级大数据开发工程师", "state": "开放中",
            "details": "上海·四川北路 ｜ 本科 ｜ 3-5年 ｜ 18-25K·13薪 ｜ 全职",
            "jd_text": "负责大数据平台研发……", "jd_complete": True,
            "tags": ["PostgreSQL", "实时数仓开发经验"]}
    body.update(over)
    assert client.post("/api/ingest/jobs", json=[body]).status_code == 200


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_ingest_job_then_read_and_count(client):
    _ingest_job(client)

    items = client.get("/api/jobs").json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "高级大数据开发工程师"
    assert items[0]["state"] == "开放中"
    assert items[0]["jd_chars"] > 0
    assert items[0]["tags"] == ["PostgreSQL", "实时数仓开发经验"]
    # 列表不带 JD 正文（它可能很长）
    assert "jd_text" not in items[0]
    # 详情带
    detail = client.get(f"/api/jobs/{items[0]['id']}").json()
    assert detail["jd_text"].startswith("负责大数据平台")

    ov = client.get("/api/hr/overview").json()
    assert ov["jobs"]["total"] == 1
    assert ov["jobs"]["开放中"] == 1
    assert ov["jobs"]["jd_ready"] == 1
    # 「可用」比「开放中」严：还要系统启用过 + JD 读全（见 hr_tools.job_list_active）
    assert "usable" in ov["jobs"]


def test_ingest_job_upserts_by_title(client):
    _ingest_job(client)
    _ingest_job(client, state="已关闭")           # 同一个职位名，状态变了
    items = client.get("/api/jobs").json()["items"]
    assert len(items) == 1, "同一个职位名应该 upsert，不是插一条新的"
    assert items[0]["state"] == "已关闭"


def test_open_job_without_state_label_is_normalized(client):
    """★ BOSS **对「开放中」的职位不打状态标签**，采数端读回来的是空串。

    不归一的话会出一条**静默的死链**：库里存 `""` → `hr_tools.job_list_active`
    筛 `state == "开放中"` 永远匹配不上 → `usable` 恒为 0 → 轮次闸门⑤永远跳过 →
    **整条自动化什么都不做，而且看起来一切正常**。

    2026-10-16 真机联调就是这么撞上的：明明有个在招职位，overview 里「开放中」是 0。
    """
    _ingest_job(client, state="", jd_complete=True)
    item = client.get("/api/jobs").json()["items"][0]
    assert item["state"] == "开放中", "空状态没有被归一"

    # 归一之后它才真的可用
    assert client.get("/api/hr/overview").json()["jobs"]["usable"] == 1


def test_half_read_jd_must_not_overwrite_a_good_one(client):
    """★ 一次抓失败的 JD（没读全）不能把已经读全的冲掉。"""
    _ingest_job(client)
    good = client.get("/api/jobs").json()["items"][0]

    _ingest_job(client, jd_text="只读到一半", jd_complete=False)

    after = client.get(f"/api/jobs/{good['id']}").json()
    assert after["jd_text"].startswith("负责大数据平台"), "半截 JD 把好 JD 冲掉了"
    assert after["jd_complete"] is True


def test_ingest_candidates_and_empty_values_do_not_clobber(client):
    """★ 列表页推上来的候选人**没有详情字段**——不能用空值盖掉详情页采到的。"""
    _ingest_job(client)          # 候选人现在是 jc 维度，得先有职位
    key = "杜延龙｜腾讯音乐娱乐科技（深圳）·数据架构师"
    client.post("/api/ingest/candidates", json=[{
        "boss_key": key, "name": "杜延龙", "job_title": "高级大数据开发工程师",
        "job_and_company": "腾讯音乐娱乐科技（深圳）·数据架构师",
        "work_year": "10年以上", "degree": "本科", "age": "37岁",
        "description": "多年大数据平台研发经验", "expect_salary": "面议",
        "tags": ["数据平台搭建"],
    }])

    # 列表页推上来的：没有详情字段
    client.post("/api/ingest/candidates", json=[
        {"boss_key": key, "name": "杜延龙", "job_title": "高级大数据开发工程师",
         "summary": "10年以上 ｜ 本科 ｜ 面议", "active": "刚刚活跃"}
    ])

    row = client.get("/api/hr/candidates").json()["items"][0]
    assert row["summary"] == "10年以上 ｜ 本科 ｜ 面议", "新的非空值应该写进去"
    assert row["work_year"] == "10年以上"

    # 详情字段（description 只在详情里给）——★ 这一条才是本测试的重点
    detail = client.get(f"/api/hr/candidates/{row['id']}").json()
    assert detail["description"] == "多年大数据平台研发经验", "空值把详情字段盖掉了"
    assert detail["company_history"] == [] or isinstance(detail["company_history"], list)


def test_ingest_conversations_dedupes_messages(client):
    """★ BOSS 不给消息 id，每次同步都会重推同一批消息——必须去重。"""
    conv = {
        "boss_name": "张雄瑜", "job_title": "高级大数据开发工程师",
        "last_msg": "您好，有兴趣的话可以发一份简历看看，我将转交业务部门。",
        "status": "[送达]", "boss_time": "16:36",
        "messages": [
            {"direction": "out", "text": "您好，有兴趣的话可以发一份简历看看，我将转交业务部门。", "boss_y": 1064},
            {"direction": "in", "text": "好的，我看看", "boss_y": 1300},
        ],
    }
    r = client.post("/api/ingest/conversations", json=[conv]).json()
    assert r == {"conversations_created": 1, "conversations_updated": 0,
                 "messages_created": 2, "messages_skipped": 0}

    # 再推一遍完全一样的内容（模拟下一次同步）
    r = client.post("/api/ingest/conversations", json=[conv]).json()
    assert r["messages_created"] == 0 and r["messages_skipped"] == 2, "重复推产生了重复消息"

    # 新消息只进新的那条
    conv["messages"].append({"direction": "in", "text": "我发简历给你", "boss_y": 1400})
    r = client.post("/api/ingest/conversations", json=[conv]).json()
    assert r["messages_created"] == 1 and r["messages_skipped"] == 2

    cid = client.get("/api/hr/conversations").json()["items"][0]["id"]
    msgs = client.get(f"/api/hr/conversations/{cid}").json()["messages"]
    assert [m["direction"] for m in msgs] == ["out", "in", "in"], "应按 boss_y 排出先后"
    assert msgs[0]["text"].startswith("您好，有兴趣")

    ov = client.get("/api/hr/overview").json()
    assert ov["messages"] == {"total": 3, "in": 2, "out": 1}


def test_greet_log_is_idempotent(client):
    key = {"boss_key": "张雄瑜｜云南白药集团·数据开发", "job": "云南白药集团·数据开发"}
    assert client.post("/api/ingest/greet-log", json=[key]).json() == {"added": 1}
    assert client.post("/api/ingest/greet-log", json=[key]).json() == {"added": 0}
    assert client.get("/api/hr/overview").json()["greeted"] == 1


def test_missing_ids_are_404_not_500(client):
    assert client.get("/api/jobs/9999").status_code == 404
    assert client.get("/api/candidates/9999").status_code == 404
    assert client.get("/api/conversations/9999").status_code == 404


def _candidate_and_job(client):
    """造一个 jc（职位 + 候选人），供附件测试用。"""
    _ingest_job(client)
    key = "侯怡旭｜北京擎天创世科技·数据开发"
    client.post("/api/ingest/candidates", json=[{
        "boss_key": key, "name": "侯怡旭", "job_title": "高级大数据开发工程师",
        "job_and_company": "北京擎天创世科技·数据开发",
        "work_year": "5年", "degree": "本科", "age": "28岁",
        "description": "数据开发", "expect_salary": "15-17K",
    }])
    return key


def test_ingest_attachment_stores_and_advances(client, monkeypatch):
    """★ 附件简历：存 MinIO → 写 object_key → 状态推进 resume_received → 记 interaction。"""
    monkeypatch.setattr("app.storage.put_object", lambda key, raw, **kw: key)
    monkeypatch.setattr("app.storage.presigned_get_url", lambda key, **kw: f"https://minio/{key}")
    _candidate_and_job(client)

    pdf = b"%PDF-1.4 fake resume bytes"
    import base64
    r = client.post("/api/ingest/attachment", json={
        "boss_name": "侯怡旭", "job_title": "高级大数据开发工程师",
        "filename": "侯怡旭-数据开发-5年.pdf",
        "content_b64": base64.b64encode(pdf).decode(),
    })
    assert r.status_code == 200
    body = r.json()
    assert body["size"] == len(pdf)
    assert body["status"] == "resume_received"
    assert body["object_key"].startswith("resumes/")

    # 落库：object_key / resume_downloaded_at / interaction(attachment)
    row = client.get("/api/hr/candidates").json()["items"][0]
    detail = client.get(f"/api/hr/candidates/{row['id']}").json()
    assert detail["status"] == "resume_received"
    assert detail["resume_downloaded_at"] is not None
    assert detail["minio_object_key"] == body["object_key"]

    # 幂等：再推一遍同一个，直接返回 skipped
    r2 = client.post("/api/ingest/attachment", json={
        "boss_name": "侯怡旭", "job_title": "高级大数据开发工程师",
        "filename": "侯怡旭-数据开发-5年.pdf",
        "content_b64": base64.b64encode(pdf).decode(),
    }).json()
    assert r2 == {"skipped": True, "object_key": body["object_key"]}


def test_ingest_attachment_rejects_non_pdf(client, monkeypatch):
    monkeypatch.setattr("app.storage.put_object", lambda key, raw, **kw: key)
    monkeypatch.setattr("app.storage.presigned_get_url", lambda key, **kw: None)
    _candidate_and_job(client)
    import base64
    r = client.post("/api/ingest/attachment", json={
        "boss_name": "侯怡旭", "job_title": "高级大数据开发工程师",
        "filename": "x.pdf",
        "content_b64": base64.b64encode(b"not a pdf").decode(),
    })
    assert r.status_code == 400


def test_ingest_attachment_unknown_candidate_is_404(client, monkeypatch):
    monkeypatch.setattr("app.storage.put_object", lambda key, raw, **kw: key)
    monkeypatch.setattr("app.storage.presigned_get_url", lambda key, **kw: None)
    _candidate_and_job(client)
    import base64
    r = client.post("/api/ingest/attachment", json={
        "boss_name": "不存在的人", "job_title": "高级大数据开发工程师",
        "filename": "x.pdf",
        "content_b64": base64.b64encode(b"%PDF-1.4 x").decode(),
    })
    assert r.status_code == 404


# ——— 评分闸门 + 日常收尾（P2）———


def _fake_screening(status, score=4, reason="匹配"):
    """替换 `app.hr_tools._screening`，返回固定判定的替身客户端。"""
    from hr_workbuddy import ScreeningResult

    result = ScreeningResult(
        hard_pass=True, hard_reasons=[], score=score,
        judge_reason=reason, status=status, degraded=False,
    )
    return type("_FakeClient", (), {"screen": staticmethod(lambda req: result)})()


def test_greet_check_screens_then_allows(client, monkeypatch, fake_redis):
    """★ 评分闸门：new 候选人在打招呼前按需评分，通过才放行并落库。"""
    from hr_workbuddy import CandidateStatus
    monkeypatch.setattr(
        "app.hr_tools._screening",
        lambda: _fake_screening(CandidateStatus.SCREENED_PASS),
    )
    _candidate_and_job(client)

    r = client.post("/api/greet/check", json={
        "boss_name": "侯怡旭", "job_title": "高级大数据开发工程师",
    })
    assert r.status_code == 200
    assert r.json()["allowed"] is True, r.json()

    row = client.get("/api/hr/candidates").json()["items"][0]
    assert row["status"] == "screened_pass"
    assert row["match_score"] == 4
    assert row["judge_reason"] == "匹配"


def test_greet_check_blocks_rejected(client, monkeypatch, fake_redis):
    """★ 评分不过 → 打招呼被拦，不扣配额、状态停在 rejected_llm。"""
    from hr_workbuddy import CandidateStatus
    monkeypatch.setattr(
        "app.hr_tools._screening",
        lambda: _fake_screening(CandidateStatus.REJECTED_LLM, score=2, reason="经验不匹配"),
    )
    _candidate_and_job(client)

    r = client.post("/api/greet/check", json={
        "boss_name": "侯怡旭", "job_title": "高级大数据开发工程师",
    }).json()
    assert r["allowed"] is False
    assert "评分未通过" in r["reason"]

    row = client.get("/api/hr/candidates").json()["items"][0]
    assert row["status"] == "rejected_llm"


def test_greet_check_fails_closed_when_screening_down(client, monkeypatch, fake_redis):
    """★ 评分服务不可用 → fail-closed：不放行、不推进，仍停在 new。"""
    def boom(req):
        raise RuntimeError("screening down")
    monkeypatch.setattr(
        "app.hr_tools._screening",
        lambda: type("_Down", (), {"screen": staticmethod(boom)})(),
    )
    _candidate_and_job(client)

    r = client.post("/api/greet/check", json={
        "boss_name": "侯怡旭", "job_title": "高级大数据开发工程师",
    }).json()
    assert r["allowed"] is False
    assert "评分服务不可用" in r["reason"]

    row = client.get("/api/hr/candidates").json()["items"][0]
    assert row["status"] == "new"
    assert row["match_score"] is None


def test_daily_sweep_closes_stale_and_rescores(client, session, monkeypatch):
    """★ 日常收尾：72h 没回应的 awaiting_resume 关闭；new 且没分的补评分。"""
    from datetime import datetime, timedelta

    from hr_workbuddy import CandidateStatus
    from sqlalchemy import select

    from app import models

    monkeypatch.setattr(
        "app.hr_tools._screening",
        lambda: _fake_screening(CandidateStatus.SCREENED_PASS),
    )
    _ingest_job(client)
    job = session.scalar(select(models.Job))

    stale_cand = models.Candidate(boss_key="stale", name="老候选人")
    session.add(stale_cand)
    session.flush()
    jc_stale = models.JobCandidate(
        job_id=job.id, candidate_id=stale_cand.id, status="awaiting_resume",
        resume_requested_at=datetime.now() - timedelta(hours=80),
    )
    session.add(jc_stale)

    fresh_cand = models.Candidate(boss_key="fresh", name="新候选人", summary="5年｜本科｜面议")
    session.add(fresh_cand)
    session.flush()
    jc_fresh = models.JobCandidate(job_id=job.id, candidate_id=fresh_cand.id, status="new")
    session.add(jc_fresh)
    session.commit()

    body = client.post("/api/ingest/daily-sweep", json={}).json()
    assert body == {"closed": 1, "scored": 1}

    assert session.get(models.JobCandidate, jc_stale.id).status == "closed"
    assert session.get(models.JobCandidate, jc_fresh.id).status == "screened_pass"
