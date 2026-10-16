"""安全联锁。

这些是**唯一挡住「系统对真实的人乱发消息」的东西**，所以每一条都要单独钉住。
猎聘那边对应的是 `tests/test_tools_mcp.py`。

★ 用内存假 Redis，不引 `fakeredis` 依赖——需要的原语只有六个。
"""

from datetime import date

import pytest

from app import hr_tools, keys, models
from app.state_machine import INBOUND, RECOMMENDED, StateEvent, TransitionContext


# ——— 数据 ———

def seed(session, *, title="高级大数据开发工程师", jd_complete=True,
         status=models.JOB_STATUS_ACTIVE, state="开放中"):
    job = models.Job(title=title, state=state, details="上海",
                     jd_text="JD 正文", jd_complete=jd_complete, status=status)
    cand = models.Candidate(boss_key=f"张雄瑜｜云南白药{title}", name="张雄瑜")
    session.add_all([job, cand])
    session.commit()
    return job, cand


# ——— 职位联锁 ———

def test_job_list_active_needs_all_three(session):
    """★ 三个条件缺一不可：启用过 + BOSS 在招 + **JD 读全了**。

    逐个改坏再改回来，确认**每一个条件单独都能把它挡在外面**。
    """
    job, _ = seed(session)
    assert [j.title for j in hr_tools.job_list_active(session)] == [job.title]

    job.jd_complete = False                      # 1) JD 没读全
    session.commit()
    assert hr_tools.job_list_active(session) == [], "半截 JD 的职位不该可用"

    job.jd_complete = True
    job.state = "已关闭"                          # 2) BOSS 那边不在招
    session.commit()
    assert hr_tools.job_list_active(session) == [], "BOSS 已关闭的职位不该可用"

    job.state = "开放中"
    job.status = models.JOB_STATUS_DRAFT         # 3) 系统没启用
    session.commit()
    assert hr_tools.job_list_active(session) == [], "系统没启用的职位不该可用"

    job.status = models.JOB_STATUS_ACTIVE
    session.commit()
    assert len(hr_tools.job_list_active(session)) == 1, "三个条件都满足时应当可用"


# ——— 风控熔断 ———

def test_risk_gate_is_fail_closed_when_redis_is_down(session, use_broken_redis):
    """★ Redis 一抖就必须什么都别做，而不是「读不到就当没风控」。"""
    assert hr_tools.risk_check() is True
    job, cand = seed(session)
    jc = hr_tools.jc_get_or_create(session, job.id, cand.id)
    ok, why = hr_tools.may_greet(session, jc)
    assert ok is False and "风控" in why


def test_risk_set_blocks_greeting(session, fake_redis):
    job, cand = seed(session)
    jc = hr_tools.jc_get_or_create(session, job.id, cand.id)

    assert hr_tools.may_greet(session, jc)[0] is True
    hr_tools.risk_set("撞到滑块验证码")

    ok, why = hr_tools.may_greet(session, jc)
    assert ok is False and "撞到滑块验证码" in why

    hr_tools.risk_clear()
    assert hr_tools.risk_check() is False


# ——— 配额 ———

def test_quota_blocks_after_cap(session, fake_redis, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "daily_greet_cap", 2, raising=False)
    assert hr_tools.quota_acquire() is True
    assert hr_tools.quota_acquire() is True
    assert hr_tools.quota_acquire() is False, "超过上限还能扣"

    # ★ 超了要把计数退回去，别越滚越大
    assert hr_tools.quota_used() == 2


def test_quota_key_has_no_account_id(fake_redis):
    """★ BOSS 单账号——键名里不该有 account_id（跟猎聘不一样）。"""
    k = keys.msg_touch(date(2026, 10, 16))
    assert k == "boss:msg-touch:2026-10-16"
    assert "account" not in k


def test_boss_keys_are_prefixed(monkeypatch):
    """★ 两层隔离的第二层：所有键都以 `boss:` 开头。

    猎聘与 BOSS 共用同一个 Redis 实例，db 号是第一层，
    前缀是防「db 号抄错」的那一层。
    """
    for k in (keys.msg_touch(date(2026, 10, 16)), keys.risk_paused(), keys.login_gate_in_flight()):
        assert k.startswith("boss:"), f"{k} 没有 boss: 前缀"


# ——— 一人一消息 ———

def test_one_message_guard(session, fake_redis):
    job, cand = seed(session)
    jc = hr_tools.jc_get_or_create(session, job.id, cand.id)

    assert hr_tools.has_out_greet(session, jc) is False
    assert hr_tools.may_greet(session, jc)[0] is True

    hr_tools.interaction_log(session, jc, "out", "greet", "您好，有兴趣的话…")
    assert hr_tools.has_out_greet(session, jc) is True

    ok, why = hr_tools.may_greet(session, jc)
    assert ok is False and "已经打过招呼" in why


def test_incoming_message_does_not_count_as_our_greet(session, fake_redis):
    """★ 对方发来的消息**不算**我们打过招呼——这就是 messages/interactions 分开的意义。"""
    job, cand = seed(session)
    jc = hr_tools.jc_get_or_create(session, job.id, cand.id)
    hr_tools.interaction_log(session, jc, "in", "reply", "请问还在招吗？")
    assert hr_tools.has_out_greet(session, jc) is False


def test_interaction_rejects_unknown_msg_type(session, fake_redis):
    job, cand = seed(session)
    jc = hr_tools.jc_get_or_create(session, job.id, cand.id)
    with pytest.raises(ValueError, match="msg_type"):
        hr_tools.interaction_log(session, jc, "out", "direct_request", "x")


def test_may_greet_does_not_burn_quota_when_it_will_refuse(session, fake_redis):
    """★ 注定要拒的场景不该白扣配额——顺序是刻意的（先熔断、再重复、最后扣）。"""
    job, cand = seed(session)
    jc = hr_tools.jc_get_or_create(session, job.id, cand.id)
    hr_tools.interaction_log(session, jc, "out", "greet")

    assert hr_tools.may_greet(session, jc)[0] is False
    assert hr_tools.quota_used() == 0, "被拒的时候把配额扣掉了"


# ——— 状态落库 ———

def test_apply_transition_persists_and_stamps(session, fake_redis):
    job, cand = seed(session)
    jc = hr_tools.jc_get_or_create(session, job.id, cand.id)

    hr_tools.apply_transition(session, jc, StateEvent.GREET, TransitionContext(source=RECOMMENDED))
    assert jc.status == "greeted"
    assert jc.last_touch_at is not None

    session.commit()
    again = session.get(models.JobCandidate, jc.id)
    assert again.status == "greeted", "没落库"


def test_apply_transition_stamps_resume_requested(session, fake_redis):
    """★ `resume_requested_at` 是 72h 无人回应的锚点，必须打上。"""
    job, cand = seed(session)
    jc = hr_tools.jc_get_or_create(session, job.id, cand.id)
    hr_tools.apply_transition(session, jc, StateEvent.GREET, TransitionContext(source=RECOMMENDED))
    hr_tools.apply_transition(session, jc, StateEvent.REQUEST_RESUME)
    assert jc.resume_requested_at is not None


def test_inbound_direct_resume_through_tools(session, fake_redis):
    job, cand = seed(session)
    jc = hr_tools.jc_get_or_create(session, job.id, cand.id)
    hr_tools.apply_transition(session, jc, StateEvent.RECEIVE_RESUME, TransitionContext(source=INBOUND))
    assert jc.status == "resume_received"


def test_illegal_transition_is_not_persisted(session, fake_redis):
    job, cand = seed(session)
    jc = hr_tools.jc_get_or_create(session, job.id, cand.id)
    from app.state_machine import InvalidTransition

    with pytest.raises(InvalidTransition):
        hr_tools.apply_transition(session, jc, StateEvent.GREET, TransitionContext(source=INBOUND))
    assert jc.status == "new"
