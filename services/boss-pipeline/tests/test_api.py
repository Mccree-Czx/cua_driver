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
