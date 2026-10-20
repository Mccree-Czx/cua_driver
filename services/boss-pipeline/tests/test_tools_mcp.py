"""BOSS hr-tools MCP 工具守卫单测。

每一条都挡在「系统对真实的人乱发消息」前面，必须单独钉住。对应猎聘的
`tests/test_tools_mcp.py`，但按 BOSS 语义：单账号、无 attachment 证据守卫
（附件简历走确定性 HTTP 路径，不进 MCP）、评分闸门在 greet_check。

★ 数据播种用 `SessionLocal()`（MCP 工具内部也用它），**不是** conftest 的
  `session` fixture——那个是内存库，跟工具连的不是同一个库。
"""

from datetime import datetime, timedelta
from uuid import uuid4

from app import models
from app.db import SessionLocal
from hr_workbuddy import CandidateStatus

import tools_mcp_server as tools


def _seed(status="screened_pass") -> int:
    """在真实测试库（SessionLocal）里建 (job, candidate, jc) 并提交，返回 jc.id。

    ★ `Job.title` 有唯一约束，而这里的库（SessionLocal）在整场测试里是共用的，
      所以 title / boss_key 都带 uuid——不然相邻用例会撞唯一键。
    """
    with SessionLocal() as s:
        job = models.Job(title=f"工具职位_{uuid4().hex[:8]}", state="开放中", details="上海",
                         jd_text="JD 正文", jd_complete=True, status=models.JOB_STATUS_ACTIVE)
        cand = models.Candidate(boss_key=f"工具候选人_{uuid4().hex[:8]}", name="工具候选人")
        s.add_all([job, cand])
        s.flush()
        jc = models.JobCandidate(job_id=job.id, candidate_id=cand.id, status=status)
        s.add(jc)
        s.commit()
        return jc.id


def _fake_screening(status, score=4, reason="匹配"):
    """替换 `app.hr_tools._screening`，返回固定判定的替身客户端。"""
    from hr_workbuddy import ScreeningResult

    result = ScreeningResult(
        hard_pass=True, hard_reasons=[], score=score,
        judge_reason=reason, status=status, degraded=False,
    )
    return type("_FakeClient", (), {"screen": staticmethod(lambda req: result)})()


class _FakeProc:
    returncode = 0
    stderr = ""


# ——— 写动作：gated_tap ———

def test_gated_tap_fail_closed_when_risk_paused(fake_redis, monkeypatch):
    """★ 熔断中绝不能碰到 adb——哪怕 Pi 传了 touch=True 也直接返回不执行。"""
    import app.hr_tools as hr_tools

    hr_tools.risk_set("撞到滑块")
    calls: list = []
    monkeypatch.setattr(hr_tools.subprocess, "run", lambda *a, **k: calls.append(a) or _FakeProc())

    result = tools.gated_tap(100, 200, touch=True)
    assert result["executed"] is False
    assert "risk_paused" in result["reason"]
    assert calls == [], "熔断中不该发任何 adb tap"


def test_gated_tap_touch_false_executes(fake_redis, monkeypatch):
    """读操作（touch=False）不经过风控闸门，直接执行 tap。"""
    import app.hr_tools as hr_tools

    calls: list = []
    monkeypatch.setattr(hr_tools.subprocess, "run", lambda *a, **k: calls.append(a) or _FakeProc())

    result = tools.gated_tap(100, 200, touch=False)
    assert result["executed"] is True
    assert len(calls) == 1
    assert calls[0][0][1:] == ["shell", "input", "tap", "100", "200"]


# ——— 联锁：greet_check（评分闸门 + 配额）———

def test_greet_check_allows_screened_pass(fake_redis):
    jc_id = _seed(status="screened_pass")
    result = tools.greet_check(jc_id)
    assert result["allowed"] is True, result


def test_greet_check_blocks_rejected(fake_redis, monkeypatch):
    """★ 评分不过 → 拦，不扣配额、状态停在 rejected_llm。"""
    import app.hr_tools as hr_tools

    monkeypatch.setattr(
        hr_tools, "_screening",
        lambda: _fake_screening(CandidateStatus.REJECTED_LLM, score=2, reason="经验不匹配"),
    )
    jc_id = _seed(status="new")
    result = tools.greet_check(jc_id)
    assert result["allowed"] is False
    assert "评分未通过" in result["reason"]

    with SessionLocal() as s:
        assert s.get(models.JobCandidate, jc_id).status == "rejected_llm"


def test_greet_check_fails_closed_when_screening_down(fake_redis, monkeypatch):
    """★ 评分服务不可用 → fail-closed：不放行、不推进，仍停在 new。"""
    import app.hr_tools as hr_tools

    def boom(req):
        raise RuntimeError("screening down")

    monkeypatch.setattr(hr_tools, "_screening", lambda: type("_Down", (), {"screen": staticmethod(boom)})())
    jc_id = _seed(status="new")
    result = tools.greet_check(jc_id)
    assert result["allowed"] is False
    assert "评分服务不可用" in result["reason"]

    with SessionLocal() as s:
        row = s.get(models.JobCandidate, jc_id)
        assert row.status == "new"
        assert row.match_score is None


# ——— 状态推进：先落账后推进 ———

def test_state_transition_touch_requires_out_interaction():
    """★ 触达类事件要求该 jc 已有 direction=out 流水，否则拒绝推进。"""
    jc_id = _seed(status="greeted")  # greeted → request_resume 本身合法，但没有 out 流水
    result = tools.state_transition(jc_id, "request_resume")
    assert "error" in result
    assert "out" in result["error"]
    with SessionLocal() as s:
        assert s.get(models.JobCandidate, jc_id).status == "greeted"  # 未被推进


def test_state_transition_after_out_interaction_works(fake_redis):
    jc_id = _seed(status="screened_pass")
    tools.interaction_log(jc_id, "out", "greet", "您好")  # 落 out 流水并顺带推进到 greeted
    result = tools.state_transition(jc_id, "request_resume")
    assert "error" not in result
    assert result["new_status"] == "resume_requested"


def test_state_transition_greet_from_new_rejected_by_machine():
    """★ greet 只能从 screened_pass 出发（状态机兜底，不靠提示词）。

    先落一条 out 流水让触达守卫放行，再验证状态机本身拒绝 new → greet。
    """
    jc_id = _seed(status="new")
    tools.interaction_log(jc_id, "out", "reply", "回复")  # 落 out 流水，但不推进状态
    result = tools.state_transition(jc_id, "greet", source="recommended")
    assert "error" in result
    assert "非法迁移" in result["error"]


# ——— interaction_log：out+greet 顺带推进（一人一消息 + 状态落库）———

def test_interaction_log_out_greet_advances(fake_redis):
    jc_id = _seed(status="screened_pass")
    result = tools.interaction_log(jc_id, "out", "greet", "您好")
    assert "error" not in result, result
    assert result["status"] == "greeted"
    with SessionLocal() as s:
        assert s.get(models.JobCandidate, jc_id).status == "greeted"


def test_interaction_log_out_greet_from_new_fails(fake_redis):
    """★ 没评分（new）不能打招呼：日志会落，但状态推进失败并报错。"""
    jc_id = _seed(status="new")
    result = tools.interaction_log(jc_id, "out", "greet", "您好")
    assert "error" in result
    assert "greet" in result["error"]


def test_interaction_log_rejects_unknown_msg_type(fake_redis):
    jc_id = _seed()
    result = tools.interaction_log(jc_id, "out", "direct_request", "x")
    assert "error" in result
    assert "msg_type" in result["error"]


# ——— 风控 / 配额 ———

def test_risk_set_clear_roundtrip(fake_redis):
    assert tools.risk_check()["risk_paused"] is False
    tools.risk_set("撞到滑块")
    assert tools.risk_check()["risk_paused"] is True
    tools.risk_clear()
    assert tools.risk_check()["risk_paused"] is False


def test_quota_status_reflects_greet(fake_redis, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "daily_greet_cap", 2, raising=False)
    assert tools.quota_status()["remaining"] == 2

    jc_id = _seed(status="screened_pass")
    tools.greet_check(jc_id)  # 通过 → 扣 1 配额
    assert tools.quota_status()["remaining"] == 1


# ——— 建档：空值不盖已有 ———

def test_candidate_get_or_create_empty_does_not_clobber():
    name = f"杜延龙_{uuid4().hex[:6]}"
    first = tools.candidate_get_or_create(
        name=name, degree="本科", description="多年大数据平台研发经验", work_year="10年以上",
    )
    assert first["created"] is True

    # 列表页再推一遍：没有详情字段
    second = tools.candidate_get_or_create(name=name, summary="10年以上 ｜ 本科 ｜ 面议")
    assert second["created"] is False
    assert second["candidate_id"] == first["candidate_id"]

    with SessionLocal() as s:
        cand = s.get(models.Candidate, second["candidate_id"])
        assert cand.degree == "本科", "空值把详情字段盖掉了"
        assert cand.description == "多年大数据平台研发经验"
        assert cand.summary == "10年以上 ｜ 本科 ｜ 面议", "新的非空值没写进去"


def test_candidate_get_or_create_key_falls_back_to_name():
    r = tools.candidate_get_or_create(name=f"张雄瑜_{uuid4().hex[:6]}")
    assert r["boss_key"].startswith("张雄瑜_"), "没给 boss_key 应退回 name 作主键"


def test_candidate_get_or_create_rejects_empty_name():
    r = tools.candidate_get_or_create(name="   ")
    assert "error" in r


# ——— 每日收尾 ———

def test_daily_sweep_closes_stale_and_rescores(fake_redis, monkeypatch):
    import app.hr_tools as hr_tools

    monkeypatch.setattr(
        hr_tools, "_screening", lambda: _fake_screening(CandidateStatus.SCREENED_PASS),
    )
    with SessionLocal() as s:
        job = models.Job(title=f"工具职位_{uuid4().hex[:8]}", state="开放中", details="上海",
                         jd_text="JD 正文", jd_complete=True, status=models.JOB_STATUS_ACTIVE)
        stale_cand = models.Candidate(boss_key=f"stale_{uuid4().hex[:6]}", name="老候选人")
        fresh_cand = models.Candidate(boss_key=f"fresh_{uuid4().hex[:6]}", name="新候选人",
                                      summary="5年｜本科｜面议")
        s.add_all([job, stale_cand, fresh_cand])
        s.flush()
        jc_stale = models.JobCandidate(
            job_id=job.id, candidate_id=stale_cand.id, status="awaiting_resume",
            resume_requested_at=datetime.now() - timedelta(hours=80),
        )
        jc_fresh = models.JobCandidate(job_id=job.id, candidate_id=fresh_cand.id, status="new")
        s.add_all([jc_stale, jc_fresh])
        s.commit()
        stale_id, fresh_id = jc_stale.id, jc_fresh.id

    result = tools.daily_sweep()
    assert result == {"closed": 1, "scored": 1}

    with SessionLocal() as s:
        assert s.get(models.JobCandidate, stale_id).status == "closed"
        assert s.get(models.JobCandidate, fresh_id).status == "screened_pass"
