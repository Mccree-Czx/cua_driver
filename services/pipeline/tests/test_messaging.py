"""话术渲染 + 一人一消息检查（spec §4 / 决策 3）。

- render_message：template_msgs JSON 变量填充（{name}/{title}），纯函数
- ensure_no_out_message：发送前检查，该 job_candidate 已有 out 方向消息即抛
  OneMessagePerCandidateError（真实打库，沿用 T3 conftest 的 session fixture）
"""

import uuid
from datetime import datetime

import pytest

from app import models
from app.messaging import (
    OneMessagePerCandidateError,
    ensure_no_out_message,
    render_message,
)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _job(**overrides) -> models.Job:
    defaults = dict(
        title="高级产品经理",
        jd_text="负责产品规划与迭代",
        template_msgs={
            "greet_request": "您好 {name}，看到您在关注{title}岗位，方便发一份简历吗？"
        },
    )
    defaults.update(overrides)
    return models.Job(**defaults)


def _candidate(name: str = "张伟", source: str = "inbound") -> models.Candidate:
    return models.Candidate(
        liepin_user_id=_unique("lp"), name=name, online_resume_minimal={}, source=source
    )


# —— render_message：变量填充（纯函数） ——


def test_render_fills_name_and_title():
    text = render_message(_job(), _candidate(name="李娜"), "greet_request")
    assert text == "您好 李娜，看到您在关注高级产品经理岗位，方便发一份简历吗？"


def test_render_comes_from_template_msgs_and_selects_variant():
    job = _job(
        template_msgs={
            "greet_request": "{name}，{title}岗位招人",
            "backup": "备用话术 {name}",
        }
    )
    assert render_message(job, _candidate(), "greet_request") == "张伟，高级产品经理岗位招人"
    assert render_message(job, _candidate(name="王五"), "backup") == "备用话术 王五"


def test_render_missing_variant_raises():
    with pytest.raises(ValueError, match="greet_request"):
        render_message(_job(template_msgs={}), _candidate(), "greet_request")


def test_render_empty_template_raises():
    with pytest.raises(ValueError, match="greet_request"):
        render_message(_job(template_msgs={"greet_request": ""}), _candidate(), "greet_request")


def test_render_direct_request_default_fallback():
    """direct_request 未配置 → 内置默认模板兜底（存量岗位兼容，2026-10-06 策略）。"""
    text = render_message(_job(template_msgs={}), _candidate(), "direct_request")
    assert text == "您好 张伟，感谢关注高级产品经理岗位，方便发一份简历吗？"


def test_render_direct_request_explicit_config_wins():
    """direct_request 显式配置优先于内置默认。"""
    job = _job(template_msgs={"direct_request": "定制的直索要 {name}"})
    assert render_message(job, _candidate(), "direct_request") == "定制的直索要 张伟"


def test_render_unknown_placeholder_raises():
    job = _job(template_msgs={"greet_request": "您好 {name}，{company}正在招聘"})
    with pytest.raises(ValueError, match="company"):
        render_message(job, _candidate(), "greet_request")


# —— ensure_no_out_message：一人一消息（决策 3，真实打库） ——


def _persisted_jc(session) -> models.JobCandidate:
    job = models.Job(title=_unique("岗位"), jd_text="负责产品规划")
    candidate = models.Candidate(
        liepin_user_id=_unique("LP"), name="张伟", online_resume_minimal={}, source="inbound"
    )
    session.add_all([job, candidate])
    session.flush()
    jc = models.JobCandidate(job_id=job.id, candidate_id=candidate.id)
    session.add(jc)
    session.flush()
    return jc


def test_ensure_no_out_message_passes_without_interactions(session):
    jc = _persisted_jc(session)
    ensure_no_out_message(session, jc)  # 不抛


def test_ensure_no_out_message_passes_with_only_inbound_interaction(session):
    """对方来信（in）不占用发送额度：仅 out 方向触发一人一消息。"""
    jc = _persisted_jc(session)
    session.add(
        models.Interaction(
            job_candidate_id=jc.id,
            direction="in",
            msg_type="reply",
            content="您好，我对这个岗位感兴趣",
            sent_at=datetime(2026, 10, 5, 9, 0, 0),
        )
    )
    session.flush()
    ensure_no_out_message(session, jc)  # 不抛


def test_second_out_message_raises_one_message_error(session):
    """该 job_candidate 已有 out 消息 → 第二次发送抛 OneMessagePerCandidateError。"""
    jc = _persisted_jc(session)
    session.add(
        models.Interaction(
            job_candidate_id=jc.id,
            direction="out",
            msg_type="greet_request",
            content="您好 张伟，看到您在关注岗位",
            sent_at=datetime(2026, 10, 5, 10, 30, 0),
        )
    )
    session.flush()

    with pytest.raises(OneMessagePerCandidateError, match="一人一消息"):
        ensure_no_out_message(session, jc)


def test_ensure_no_out_message_requires_persisted_jc(session):
    with pytest.raises(ValueError, match="尚未持久化"):
        ensure_no_out_message(session, models.JobCandidate(job_id=1, candidate_id=1))
