"""状态机测试（spec §4，边集逐字 + 关键非法边 + override 规则）。

纯函数测试不触库（conftest 的迁移 fixture 照常跑一次）：
transition 不落库、不改传入对象，返回按列值构造的新 JobCandidate，
status 写回 CandidateStatus 字符串值。路径消歧走 jc.candidate.source，
ctx.source 显式传入时优先。
"""

import pytest

from app import models
from app.state_machine import (
    InvalidTransition,
    StateEvent,
    TransitionContext,
    transition,
)
from hr_workbuddy import CandidateStatus as S

INBOUND = "inbound"
OUTBOUND = "recommended"


def _make_jc(source: str = INBOUND, status: S = S.NEW) -> models.JobCandidate:
    """detached JobCandidate + 挂 candidate（路径消歧读 jc.candidate.source）。"""
    candidate = models.Candidate(
        liepin_user_id=f"lp-{source}-{status.value}",
        name="张伟",
        online_resume_minimal={},
        source=source,
    )
    jc = models.JobCandidate(job_id=1, candidate_id=1, status=status.value)
    jc.candidate = candidate
    return jc


# §4 逐字边集（inbound/outbound）+ 2026-10-06 分流新增：new→resume_received（inbound 直收）
LEGAL_EDGES = [
    # —— inbound：new → screened_pass | rejected_hard | rejected_llm ——
    (INBOUND, S.NEW, StateEvent.SCREEN_PASS, S.SCREENED_PASS),
    (INBOUND, S.NEW, StateEvent.REJECT_HARD, S.REJECTED_HARD),
    (INBOUND, S.NEW, StateEvent.REJECT_LLM, S.REJECTED_LLM),
    # —— inbound 直收入库（已带简历，硬规则不拦收）：new → resume_received ——
    (INBOUND, S.NEW, StateEvent.RECEIVE_RESUME, S.RESUME_RECEIVED),
    # —— inbound：screened_pass → resume_requested → awaiting_resume ——
    (INBOUND, S.SCREENED_PASS, StateEvent.REQUEST_RESUME, S.RESUME_REQUESTED),
    (INBOUND, S.RESUME_REQUESTED, StateEvent.AWAIT_RESUME, S.AWAITING_RESUME),
    # —— inbound：awaiting_resume → resume_received | no_response → closed ——
    (INBOUND, S.AWAITING_RESUME, StateEvent.RECEIVE_RESUME, S.RESUME_RECEIVED),
    (INBOUND, S.AWAITING_RESUME, StateEvent.NO_RESPONSE, S.NO_RESPONSE),
    (INBOUND, S.NO_RESPONSE, StateEvent.CLOSE, S.CLOSED),
    # —— outbound：new → screened_pass | rejected_hard | rejected_llm ——
    (OUTBOUND, S.NEW, StateEvent.SCREEN_PASS, S.SCREENED_PASS),
    (OUTBOUND, S.NEW, StateEvent.REJECT_HARD, S.REJECTED_HARD),
    (OUTBOUND, S.NEW, StateEvent.REJECT_LLM, S.REJECTED_LLM),
    # —— outbound：screened_pass → greeted → resume_requested → awaiting_resume ——
    (OUTBOUND, S.SCREENED_PASS, StateEvent.GREET, S.GREETED),
    (OUTBOUND, S.GREETED, StateEvent.REQUEST_RESUME, S.RESUME_REQUESTED),
    (OUTBOUND, S.RESUME_REQUESTED, StateEvent.AWAIT_RESUME, S.AWAITING_RESUME),
    # —— outbound：awaiting_resume → resume_received | no_response → closed ——
    (OUTBOUND, S.AWAITING_RESUME, StateEvent.RECEIVE_RESUME, S.RESUME_RECEIVED),
    (OUTBOUND, S.AWAITING_RESUME, StateEvent.NO_RESPONSE, S.NO_RESPONSE),
    (OUTBOUND, S.NO_RESPONSE, StateEvent.CLOSE, S.CLOSED),
    # —— 共享后段（工作台复核）：resume_received → hr_reviewed → closed ——
    (INBOUND, S.RESUME_RECEIVED, StateEvent.HR_REVIEW, S.HR_REVIEWED),
    (INBOUND, S.HR_REVIEWED, StateEvent.CLOSE, S.CLOSED),
    (OUTBOUND, S.RESUME_RECEIVED, StateEvent.HR_REVIEW, S.HR_REVIEWED),
    (OUTBOUND, S.HR_REVIEWED, StateEvent.CLOSE, S.CLOSED),
]

LEGAL_IDS = [f"{src}:{start.value}--{ev.value}-->{exp.value}" for src, start, ev, exp in LEGAL_EDGES]


@pytest.mark.parametrize(
    ("source", "start", "event", "expected"), LEGAL_EDGES, ids=LEGAL_IDS
)
def test_legal_edges(source, start, event, expected):
    jc = _make_jc(source=source, status=start)
    result = transition(jc, event)
    assert result.status == expected.value
    assert S(result.status) is expected


# 关键非法边：路径消歧 / 边集不允许 / 终态锁定，断言异常类型 + 消息要点
INVALID_EDGES = [
    # —— 路径消歧（controller 裁定）——
    (INBOUND, S.SCREENED_PASS, StateEvent.GREET, "仅存在于 outbound"),  # inbound 出现 greet 相关事件
    (INBOUND, S.GREETED, StateEvent.REQUEST_RESUME, "无法判定合法路径"),  # inbound 无 greeted 后继
    (OUTBOUND, S.SCREENED_PASS, StateEvent.REQUEST_RESUME, "须从 greeted 出发"),  # outbound 跳过 greet
    # —— 边集不允许 ——
    (INBOUND, S.NEW, StateEvent.GREET, "非法迁移"),  # new 不能直接 greet（须先 screened_pass）
    (OUTBOUND, S.NEW, StateEvent.GREET, "非法迁移"),  # 同上
    (INBOUND, S.NEW, StateEvent.CLOSE, "非法迁移"),
    (INBOUND, S.NEW, StateEvent.AWAIT_RESUME, "非法迁移"),
    (OUTBOUND, S.NEW, StateEvent.RECEIVE_RESUME, "无法判定合法路径"),  # 直收仅 inbound（2026-10-06 分流）
    (INBOUND, S.AWAITING_RESUME, StateEvent.REQUEST_RESUME, "非法迁移"),
    (INBOUND, S.RESUME_REQUESTED, StateEvent.RECEIVE_RESUME, "非法迁移"),
    (INBOUND, S.NO_RESPONSE, StateEvent.RECEIVE_RESUME, "非法迁移"),  # no_response 只到 closed
    (INBOUND, S.RESUME_RECEIVED, StateEvent.NO_RESPONSE, "非法迁移"),
    (INBOUND, S.SCREENED_PASS, StateEvent.CLOSE, "非法迁移"),
    (INBOUND, S.GREETED, StateEvent.SCREEN_PASS, "非法迁移"),  # greeted 后回 new 类迁移
    (INBOUND, S.GREETED, StateEvent.GREET, "非法迁移"),
    (OUTBOUND, S.GREETED, StateEvent.SCREEN_PASS, "非法迁移"),
    # —— 终态（rejected_hard/rejected_llm/closed）一旦进入不再迁移 ——
    (INBOUND, S.REJECTED_HARD, StateEvent.SCREEN_PASS, "终态"),
    (OUTBOUND, S.REJECTED_LLM, StateEvent.GREET, "终态"),
    (INBOUND, S.REJECTED_HARD, StateEvent.CLOSE, "终态"),
    (INBOUND, S.CLOSED, StateEvent.HR_REVIEW, "终态"),
]

INVALID_IDS = [f"{src}:{start.value}--{ev.value}--X" for src, start, ev, _ in INVALID_EDGES]


@pytest.mark.parametrize(
    ("source", "start", "event", "message"), INVALID_EDGES, ids=INVALID_IDS
)
def test_invalid_edges_raise(source, start, event, message):
    with pytest.raises(InvalidTransition, match=message):
        transition(_make_jc(source=source, status=start), event)


def test_override_any_non_terminal_to_any_non_terminal():
    """override 可跨任意非终态（目标/来源业务约束在 T7/M3 复核接口执行）。"""
    jc = _make_jc(status=S.SCREENED_PASS)
    result = transition(
        jc, StateEvent.OVERRIDE, TransitionContext(target_status=S.AWAITING_RESUME)
    )
    assert result.status == S.AWAITING_RESUME.value


def test_override_may_target_terminal():
    """目标态约束归 T7 复核接口：T4 不阻止（如复核改判 rejected_hard）。"""
    jc = _make_jc(status=S.SCREENED_PASS)
    result = transition(
        jc, StateEvent.OVERRIDE, TransitionContext(target_status=S.REJECTED_HARD)
    )
    assert result.status == S.REJECTED_HARD.value


def test_override_from_terminal_raises():
    with pytest.raises(InvalidTransition, match="终态"):
        transition(
            _make_jc(status=S.CLOSED),
            StateEvent.OVERRIDE,
            TransitionContext(target_status=S.NEW),
        )


def test_override_without_target_raises():
    with pytest.raises(InvalidTransition, match="target_status"):
        transition(_make_jc(status=S.SCREENED_PASS), StateEvent.OVERRIDE)


def test_override_invalid_target_raises():
    with pytest.raises(InvalidTransition, match="非法 override 目标"):
        transition(
            _make_jc(status=S.SCREENED_PASS),
            StateEvent.OVERRIDE,
            TransitionContext(target_status="bogus"),
        )


def test_ctx_source_takes_precedence_over_candidate_source():
    """candidate.source=recommended 但 ctx 显式 inbound → 走 inbound 路径。"""
    jc = _make_jc(source=OUTBOUND, status=S.SCREENED_PASS)
    result = transition(jc, StateEvent.REQUEST_RESUME, TransitionContext(source=INBOUND))
    assert result.status == S.RESUME_REQUESTED.value

    jc2 = _make_jc(source=OUTBOUND, status=S.SCREENED_PASS)
    with pytest.raises(InvalidTransition, match="仅存在于 outbound"):
        transition(jc2, StateEvent.GREET, TransitionContext(source=INBOUND))


def test_path_agnostic_edge_needs_no_source():
    """与路径无关的边（如 new→screened_pass）无需 candidate/ctx.source。"""
    jc = models.JobCandidate(job_id=1, candidate_id=1, status=S.NEW.value)
    result = transition(jc, StateEvent.SCREEN_PASS)
    assert result.status == S.SCREENED_PASS.value


def test_path_dependent_edge_without_source_raises():
    jc = models.JobCandidate(job_id=1, candidate_id=1, status=S.SCREENED_PASS.value)
    with pytest.raises(InvalidTransition, match="无法判定路径"):
        transition(jc, StateEvent.REQUEST_RESUME)


def test_unknown_event_raises():
    with pytest.raises(InvalidTransition, match="未知事件"):
        transition(_make_jc(), "bogus")


def test_unknown_status_raises():
    jc = models.JobCandidate(job_id=1, candidate_id=1, status="weird")
    with pytest.raises(InvalidTransition, match="无法识别的当前状态"):
        transition(jc, StateEvent.SCREEN_PASS)


def test_transition_is_pure_and_writes_string_status():
    """不落库、不改传入对象；返回新实例，status 为字符串值，列值保持。"""
    jc = _make_jc(status=S.NEW)
    before = jc.status
    result = transition(jc, StateEvent.SCREEN_PASS)

    assert result is not jc
    assert jc.status == before  # 传入对象未被修改
    assert result.id == jc.id and result.job_id == jc.job_id
    assert isinstance(result.status, str)
    assert result.status == S.SCREENED_PASS.value


def test_inbound_happy_path_chain():
    """inbound 全程：new→screened_pass→resume_requested→awaiting_resume
    →resume_received→hr_reviewed→closed（返回对象可继续推进）。"""
    jc = _make_jc(source=INBOUND, status=S.NEW)
    jc = transition(jc, StateEvent.SCREEN_PASS)
    jc = transition(jc, StateEvent.REQUEST_RESUME)
    jc = transition(jc, StateEvent.AWAIT_RESUME)
    jc = transition(jc, StateEvent.RECEIVE_RESUME)
    jc = transition(jc, StateEvent.HR_REVIEW)
    jc = transition(jc, StateEvent.CLOSE)
    assert jc.status == S.CLOSED.value
    with pytest.raises(InvalidTransition, match="终态"):
        transition(jc, StateEvent.OVERRIDE, TransitionContext(target_status=S.NEW))


def test_outbound_happy_path_chain():
    """outbound 全程：new→screened_pass→greeted→resume_requested→awaiting_resume
    →no_response→closed（no_response 非终态，closed 才锁死）。"""
    jc = _make_jc(source=OUTBOUND, status=S.NEW)
    jc = transition(jc, StateEvent.SCREEN_PASS)
    jc = transition(jc, StateEvent.GREET)
    jc = transition(jc, StateEvent.REQUEST_RESUME)
    jc = transition(jc, StateEvent.AWAIT_RESUME)
    jc = transition(jc, StateEvent.NO_RESPONSE)
    assert jc.status == S.NO_RESPONSE.value
    jc = transition(jc, StateEvent.CLOSE)
    assert jc.status == S.CLOSED.value
