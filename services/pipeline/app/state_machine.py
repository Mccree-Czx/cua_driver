"""候选人状态机（spec §4，边集逐字实现）。

设计裁定（T4）：
- StateEvent 是 pipeline 内部概念（不进 contracts 包），事件覆盖 §4 每条边
- transition(jc, event, ctx) 是纯函数：不落库、不改传入对象，返回按列值构造的
  新 JobCandidate（detached，status 写回 CandidateStatus 字符串值）；
  持久化由 T6/T7 编排层负责
- 路径消歧：jc.candidate.source，或 ctx.source 显式传入（ctx 优先）；
  inbound 上出现 greet 相关事件、outbound 跳过 greet 直接 request_resume
  均抛 InvalidTransition
- 终态（rejected_hard / rejected_llm / closed）一旦进入不再迁移；
  no_response 不是终态（no_response → closed 是合法边）
- OVERRIDE：任意非终态 → ctx.target_status（任意合法 CandidateStatus）。
  来源/目标的业务约束在 T7/M3 复核接口执行，本任务只守住"终态不再迁移"不变式
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sqlalchemy.orm.exc import DetachedInstanceError

from app.models import JobCandidate
from hr_workbuddy import CandidateStatus

S = CandidateStatus  # 边表可读性简写

INBOUND = "inbound"
OUTBOUND = "recommended"

TERMINAL = frozenset({S.REJECTED_HARD, S.REJECTED_LLM, S.CLOSED})


class InvalidTransition(Exception):
    """非法状态迁移：边集不允许 / 路径不允许 / 终态锁定 / 输入非法。"""


class StateEvent(str, Enum):
    """状态机事件（§4 逐边覆盖）。"""

    SCREEN_PASS = "screen_pass"  # new → screened_pass
    REJECT_HARD = "reject_hard"  # new → rejected_hard
    REJECT_LLM = "reject_llm"  # new → rejected_llm
    GREET = "greet"  # outbound: screened_pass → greeted
    REQUEST_RESUME = "request_resume"  # inbound: screened_pass → resume_requested | outbound: greeted → resume_requested
    AWAIT_RESUME = "await_resume"  # resume_requested → awaiting_resume
    RECEIVE_RESUME = "receive_resume"  # awaiting_resume → resume_received
    NO_RESPONSE = "no_response"  # awaiting_resume → no_response
    HR_REVIEW = "hr_review"  # resume_received → hr_reviewed
    CLOSE = "close"  # no_response → closed | hr_reviewed → closed
    OVERRIDE = "override"  # 任意非终态 → ctx.target_status（人工复核推翻）


@dataclass(frozen=True)
class TransitionContext:
    """transition 的补充输入。

    source: 显式路径判定（与 jc.candidate.source 并存时以 ctx 为准），
            用于 candidate 关系不可得的场景；
    target_status: OVERRIDE 事件的目标状态。
    """

    source: str | None = None
    target_status: CandidateStatus | None = None


# 路径无关边：(当前状态, 事件) → 目标状态
_EDGES: dict[tuple[CandidateStatus, StateEvent], CandidateStatus] = {
    (S.NEW, StateEvent.SCREEN_PASS): S.SCREENED_PASS,
    (S.NEW, StateEvent.REJECT_HARD): S.REJECTED_HARD,
    (S.NEW, StateEvent.REJECT_LLM): S.REJECTED_LLM,
    (S.RESUME_REQUESTED, StateEvent.AWAIT_RESUME): S.AWAITING_RESUME,
    (S.AWAITING_RESUME, StateEvent.RECEIVE_RESUME): S.RESUME_RECEIVED,
    (S.AWAITING_RESUME, StateEvent.NO_RESPONSE): S.NO_RESPONSE,
    (S.NO_RESPONSE, StateEvent.CLOSE): S.CLOSED,
    (S.RESUME_RECEIVED, StateEvent.HR_REVIEW): S.HR_REVIEWED,
    (S.HR_REVIEWED, StateEvent.CLOSE): S.CLOSED,
}

# 路径相关边：(当前状态, 事件) → {路径: 目标状态}
_PATH_EDGES: dict[
    tuple[CandidateStatus, StateEvent], dict[str, CandidateStatus]
] = {
    (S.SCREENED_PASS, StateEvent.GREET): {OUTBOUND: S.GREETED},
    (S.SCREENED_PASS, StateEvent.REQUEST_RESUME): {INBOUND: S.RESUME_REQUESTED},
    (S.GREETED, StateEvent.REQUEST_RESUME): {OUTBOUND: S.RESUME_REQUESTED},
}


def transition(
    jc: JobCandidate, event: StateEvent, ctx: TransitionContext | None = None
) -> JobCandidate:
    """推进一次状态迁移，返回更新后的 JobCandidate（不落库、不改传入对象）。

    非法迁移（边集不允许 / 路径不允许 / 终态锁定 / 输入非法）抛 InvalidTransition。
    """
    current = _current_status(jc)
    if not isinstance(event, StateEvent):
        raise InvalidTransition(f"未知事件：{event!r}（须为 StateEvent）")
    if current in TERMINAL:
        raise InvalidTransition(
            f"终态 {current.value} 一旦进入不再迁移（事件 {event.value}）"
        )
    if event is StateEvent.OVERRIDE:
        return _override(jc, ctx)

    key = (current, event)
    if key in _PATH_EDGES:
        path = _resolve_source(jc, ctx)
        variants = _PATH_EDGES[key]
        if path in variants:
            return _copy_with_status(jc, variants[path])
        raise InvalidTransition(_path_error(event, path, current))

    target = _EDGES.get(key)
    if target is None:
        raise InvalidTransition(
            f"非法迁移：状态 {current.value} 不接受事件 {event.value}"
        )
    return _copy_with_status(jc, target)


def _override(jc: JobCandidate, ctx: TransitionContext | None) -> JobCandidate:
    """OVERRIDE：任意非终态 → ctx.target_status（T7 复核接口负责来源/目标业务约束）。"""
    target = ctx.target_status if ctx is not None else None
    if target is None:
        raise InvalidTransition("OVERRIDE 事件须在 ctx.target_status 提供目标状态")
    if not isinstance(target, CandidateStatus):
        raise InvalidTransition(
            f"非法 override 目标：{target!r}（须为 CandidateStatus）"
        )
    return _copy_with_status(jc, target)


def _current_status(jc: JobCandidate) -> CandidateStatus:
    raw = jc.status
    if isinstance(raw, CandidateStatus):
        return raw
    try:
        return CandidateStatus(raw)
    except ValueError:
        raise InvalidTransition(
            f"无法识别的当前状态：{raw!r}（须为 CandidateStatus 11 态之一）"
        ) from None


def _resolve_source(jc: JobCandidate, ctx: TransitionContext | None) -> str | None:
    """路径判定：ctx.source 显式传入时优先，否则读 jc.candidate.source。"""
    if ctx is not None and ctx.source is not None:
        return ctx.source
    candidate = _safe_candidate(jc)
    return getattr(candidate, "source", None) if candidate is not None else None


def _safe_candidate(jc: JobCandidate):
    """未加载的 relationship 在 detached 实例上会抛 DetachedInstanceError，视为不可得。"""
    try:
        return jc.candidate
    except DetachedInstanceError:
        return None


def _path_error(event: StateEvent, path: str | None, current: CandidateStatus) -> str:
    if event is StateEvent.GREET:
        if path == INBOUND:
            return "greet 仅存在于 outbound 路径（当前 source=inbound，inbound 是对方先开口）"
        return f"greet 仅存在于 outbound 路径（source={path!r}）"
    # REQUEST_RESUME
    if path == OUTBOUND:
        return (
            f"outbound 的 request_resume 须从 greeted 出发"
            f"（当前 {current.value}；§4: screened_pass → greeted → resume_requested）"
        )
    if path is None:
        return (
            "无法判定路径（candidate.source 与 ctx.source 均不可得）；"
            "request_resume 在 inbound 从 screened_pass、outbound 从 greeted 出发"
        )
    return (
        f"无法判定合法路径（source={path!r}）："
        "request_resume 在 inbound 从 screened_pass、outbound 从 greeted 出发"
    )


def _copy_with_status(jc: JobCandidate, new_status: CandidateStatus) -> JobCandidate:
    """按列值构造新 JobCandidate 并写回 status 字符串值；保留已挂载的 candidate
    关系（供编排层 merge 与后续迁移的路径判定）。"""
    updated = JobCandidate(
        **{col.name: getattr(jc, col.name) for col in jc.__table__.columns}
    )
    candidate = jc.__dict__.get("candidate")
    if candidate is not None:
        updated.candidate = candidate
    updated.status = new_status.value
    return updated
