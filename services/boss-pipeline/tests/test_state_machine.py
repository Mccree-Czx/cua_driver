"""BOSS 状态机。

重点钉住三件**跟猎聘不一样**的事，它们都是「少做」而不是「多做」：

1. **评分闸门重新打开（P2）**：牛人推荐流进来的候选人先评分、
   `new → screened_pass / rejected_hard / rejected_llm`，再 `screened_pass → greeted`。
   对方主动开口（inbound）不打分——先回应，别用打分挡人。
2. **`new → greeted` 直连已拆掉**：`greet` 只能从 `screened_pass` 出发。
3. **inbound 直收**（`new → resume_received`）是 BOSS 独有的边——
   对方主动发来简历、我们没求过。

外加纯函数不变式：`transition()` 不改传入对象。
"""

import pytest
from hr_workbuddy import CandidateStatus as S

from app.models import JobCandidate
from app.state_machine import (
    INBOUND,
    RECOMMENDED,
    InvalidTransition,
    StateEvent,
    TransitionContext,
    transition,
)


def jc(status: S) -> JobCandidate:
    return JobCandidate(job_id=1, candidate_id=1, status=status.value)


# ——— 主链 ———

def test_recommended_main_chain():
    """牛人推荐流：new → screened_pass → greeted → resume_requested → awaiting_resume → resume_received → hr_reviewed → closed"""
    row = jc(S.NEW)
    row = transition(row, StateEvent.SCREEN_PASS, TransitionContext(source=RECOMMENDED))
    assert row.status == S.SCREENED_PASS.value

    row = transition(row, StateEvent.GREET, TransitionContext(source=RECOMMENDED))
    assert row.status == S.GREETED.value

    row = transition(row, StateEvent.REQUEST_RESUME)
    assert row.status == S.RESUME_REQUESTED.value

    row = transition(row, StateEvent.AWAIT_RESUME)
    assert row.status == S.AWAITING_RESUME.value

    row = transition(row, StateEvent.RECEIVE_RESUME)
    assert row.status == S.RESUME_RECEIVED.value

    row = transition(row, StateEvent.HR_REVIEW)
    assert row.status == S.HR_REVIEWED.value

    row = transition(row, StateEvent.CLOSE)
    assert row.status == S.CLOSED.value


def test_no_response_branch():
    row = jc(S.AWAITING_RESUME)
    row = transition(row, StateEvent.NO_RESPONSE)
    assert row.status == S.NO_RESPONSE.value
    assert transition(row, StateEvent.CLOSE).status == S.CLOSED.value


def test_transition_is_pure():
    """★ 纯函数：不改传入对象（改的是返回的新对象）。"""
    row = jc(S.NEW)
    out = transition(row, StateEvent.SCREEN_PASS, TransitionContext(source=RECOMMENDED))
    assert row.status == S.NEW.value, "传入对象被改了"
    assert out is not row


# ——— 路径消歧 ———

def test_greet_is_outbound_only():
    """★ greet 恒为 recommended 路径——BOSS 上只有牛人列表能打招呼。"""
    row = transition(jc(S.NEW), StateEvent.SCREEN_PASS, TransitionContext(source=RECOMMENDED))
    with pytest.raises(InvalidTransition, match="greet 仅存在于 recommended"):
        transition(row, StateEvent.GREET, TransitionContext(source=INBOUND))


def test_greet_without_source_is_rejected():
    """推不出路径就抛错，**不猜**。"""
    row = transition(jc(S.NEW), StateEvent.SCREEN_PASS, TransitionContext(source=RECOMMENDED))
    with pytest.raises(InvalidTransition, match="greet 仅存在于 recommended"):
        transition(row, StateEvent.GREET)


def test_inbound_direct_resume():
    """★ BOSS 独有：对方主动发来简历，我们没求过 → new 直接到 resume_received。"""
    row = transition(
        jc(S.NEW), StateEvent.RECEIVE_RESUME, TransitionContext(source=INBOUND)
    )
    assert row.status == S.RESUME_RECEIVED.value


def test_inbound_direct_resume_needs_source():
    """这条边**只在 inbound**，不给 source 就不许走。"""
    with pytest.raises(InvalidTransition, match="无法判定路径"):
        transition(jc(S.NEW), StateEvent.RECEIVE_RESUME)


def test_inbound_direct_resume_not_from_recommended():
    with pytest.raises(InvalidTransition):
        transition(
            jc(S.NEW), StateEvent.RECEIVE_RESUME, TransitionContext(source=RECOMMENDED)
        )


def test_receive_resume_from_awaiting_needs_no_source():
    """从 awaiting_resume 出发是**路径无关**的——已经在等简历了，谁先开口不重要。"""
    row = transition(jc(S.AWAITING_RESUME), StateEvent.RECEIVE_RESUME)
    assert row.status == S.RESUME_RECEIVED.value


def test_receive_resume_after_greet():
    """★ BOSS 实测（2026-10-19）：打过招呼后，候选人可以**主动**发附件简历，
    我们没求过——不能强走 request_resume 那条链，直接从 greeted 收下。"""
    row = transition(jc(S.GREETED), StateEvent.RECEIVE_RESUME)
    assert row.status == S.RESUME_RECEIVED.value


def test_receive_resume_after_request():
    """求过简历、对方还没正式投之前，也可以直接收到附件（不经过 awaiting_resume）。"""
    row = transition(jc(S.RESUME_REQUESTED), StateEvent.RECEIVE_RESUME)
    assert row.status == S.RESUME_RECEIVED.value


# ——— 评分闸门（P2）———


def test_screen_pass_then_greet():
    """评分通过是打招呼的前置：new → screened_pass → greeted。"""
    row = transition(jc(S.NEW), StateEvent.SCREEN_PASS, TransitionContext(source=RECOMMENDED))
    assert row.status == S.SCREENED_PASS.value
    row = transition(row, StateEvent.GREET, TransitionContext(source=RECOMMENDED))
    assert row.status == S.GREETED.value


def test_greet_requires_screened_pass():
    """★ `new` 上直接 greet 走不通——必须先评分通过（严格卡打招呼）。"""
    with pytest.raises(InvalidTransition, match="非法迁移"):
        transition(jc(S.NEW), StateEvent.GREET, TransitionContext(source=RECOMMENDED))


def test_reject_hard_terminal():
    row = transition(jc(S.NEW), StateEvent.REJECT_HARD, TransitionContext(source=RECOMMENDED))
    assert row.status == S.REJECTED_HARD.value
    with pytest.raises(InvalidTransition, match="终态"):
        transition(row, StateEvent.GREET, TransitionContext(source=RECOMMENDED))


def test_reject_llm_terminal():
    row = transition(jc(S.NEW), StateEvent.REJECT_LLM, TransitionContext(source=RECOMMENDED))
    assert row.status == S.REJECTED_LLM.value
    with pytest.raises(InvalidTransition, match="终态"):
        transition(row, StateEvent.GREET, TransitionContext(source=RECOMMENDED))


def test_screen_is_recommended_only():
    """★ 评分只在牛人推荐流跑，对方主动开口（inbound）不打分。"""
    with pytest.raises(InvalidTransition):
        transition(jc(S.NEW), StateEvent.SCREEN_PASS, TransitionContext(source=INBOUND))


def test_cannot_greet_twice():
    """已经 greeted 了不能再 greet（这就是「一人一消息」在状态机层的兜底）。"""
    row = transition(jc(S.NEW), StateEvent.SCREEN_PASS, TransitionContext(source=RECOMMENDED))
    row = transition(row, StateEvent.GREET, TransitionContext(source=RECOMMENDED))
    with pytest.raises(InvalidTransition):
        transition(row, StateEvent.GREET, TransitionContext(source=RECOMMENDED))


# ——— 终态锁 ———

@pytest.mark.parametrize("terminal", [S.CLOSED, S.REJECTED_HARD, S.REJECTED_LLM])
def test_terminal_states_are_locked(terminal):
    for event in StateEvent:
        with pytest.raises(InvalidTransition, match="终态"):
            transition(jc(terminal), event)


def test_unknown_status_is_rejected():
    row = JobCandidate(job_id=1, candidate_id=1, status="不存在的状态")
    with pytest.raises(InvalidTransition, match="无法识别的当前状态"):
        transition(row, StateEvent.GREET, TransitionContext(source=RECOMMENDED))


# ——— 人工复核 ———

def test_override_to_closed_from_greeted():
    """「不合适」在 BOSS 上没有打分链路，用 override 表达。"""
    row = transition(
        jc(S.GREETED), StateEvent.OVERRIDE, TransitionContext(target_status=S.CLOSED)
    )
    assert row.status == S.CLOSED.value


def test_override_needs_target():
    with pytest.raises(InvalidTransition, match="须在 ctx.target_status"):
        transition(jc(S.GREETED), StateEvent.OVERRIDE)


def test_override_cannot_leave_terminal():
    with pytest.raises(InvalidTransition, match="终态"):
        transition(
            jc(S.CLOSED), StateEvent.OVERRIDE, TransitionContext(target_status=S.GREETED)
        )
