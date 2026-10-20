"""BOSS 版候选人状态机（照猎聘 `services/pipeline/app/state_machine.py` 的形状）。

## 枚举照搬，边集裁剪

11 态枚举**直接用猎聘那份**（`hr_workbuddy.CandidateStatus`）——两边前端都要按状态
上色/上标签，枚举一旦分叉就会各说各话。

但**边集按 BOSS 的实际情况裁**，见下面每一段。

## BOSS 上真正能走通的链

    new ──screen_pass──▶ screened_pass ──greet──▶ greeted ──request_resume──▶ resume_requested
                                                                                 │
                                                                           await_resume
                                                                                 ▼
                                                                           awaiting_resume ──receive_resume──▶ resume_received
                                                                                 │                                     │
                                                                           no_response                           hr_review
                                                                                 │                                     ▼
                                                                                 └───────────close────────▶ closed ◀──┘

## 与猎聘的三处实质差异

1. **评分闸门（P2）。**
   BOSS 复用猎聘的 screening 服务，牛人推荐流进来的候选人**先评分、通过才打招呼**：
   `new → screened_pass / rejected_hard / rejected_llm`，再 `screened_pass → greeted`。
   inbound 的「新招呼」在**求简历前也评分**（`screen(jc_id, source="inbound")`），
   通过才发「求简历」、不通过不发——但「对方直接发来简历」的 inbound 直收仍不评分。

2. **`new → greeted` 直连已拆掉。**
   打分启用后 `greet` 只能从 `screened_pass` 出发；没评过分或没过的 `new` 打不了招呼。
   评分在 `hr_tools.screen_candidate` 里按需跑（打招呼前、逐候选人一次）。

3. **★ BOSS 独有的「inbound 直收」边**：`(new, RECEIVE_RESUME) → resume_received`。
   对方在会话里**主动**发来简历、我们根本没求过——这是 BOSS 上很常见的一种开场。

## 路径消歧

路径相关的事件必须显式给 `ctx.source`，推不出来就抛 `InvalidTransition`，**不猜**：

- `recommended` —— 从牛人推荐流来的
- `inbound` —— 从消息 tab 先开口的

各事件的路径约束：

- `greet` **恒 outbound**（BOSS 上只有牛人列表「立即沟通」一条路能打招呼）。
- `screen_pass` / `reject_hard` / `reject_llm` **两条路径都行**——推荐流打分放行打招呼，
  inbound 打分放行「求简历」（对方主动开口先看过在线简历再决定求不求）。
- `request_resume` 从 `greeted` 出发**与路径无关**（两条路径最终都汇到 greeted），
  但从 `screened_pass` 出发**仅 inbound**——inbound 跳过 greet，评分通过后直接求简历；
  recommended 仍必须走 `screened_pass → greet → greeted → request_resume`。
- `receive_resume` 的直收边**仅 inbound**（对方没等我们求就发来简历）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from hr_workbuddy import CandidateStatus

from app.models import JobCandidate

S = CandidateStatus  # 边表可读性简写

RECOMMENDED = "recommended"
INBOUND = "inbound"
SOURCES = (RECOMMENDED, INBOUND)

#: 终态。跟猎聘一致：进去了就不再迁移。
#: `rejected_hard` / `rejected_llm` 现在**能进**（评分判拒），进了就锁死。
TERMINAL = frozenset({S.REJECTED_HARD, S.REJECTED_LLM, S.CLOSED})


class InvalidTransition(Exception):
    """非法状态迁移：边集不允许 / 路径不允许 / 终态锁定 / 输入非法。"""


class StateEvent(str, Enum):
    """状态机事件。★ 只保留 BOSS 上**有生产者**的那些。"""

    GREET = "greet"                    # outbound: screened_pass → greeted
    SCREEN_PASS = "screen_pass"        # outbound: new → screened_pass（评分通过）
    REJECT_HARD = "reject_hard"        # outbound: new → rejected_hard（硬规则不通过）
    REJECT_LLM = "reject_llm"          # outbound: new → rejected_llm（LLM 打分低于门槛）
    REQUEST_RESUME = "request_resume"  # greeted → resume_requested
    AWAIT_RESUME = "await_resume"      # resume_requested → awaiting_resume
    RECEIVE_RESUME = "receive_resume"  # awaiting_resume → resume_received；inbound 可从 new 直收
    NO_RESPONSE = "no_response"        # awaiting_resume → no_response
    HR_REVIEW = "hr_review"            # resume_received → hr_reviewed
    CLOSE = "close"                    # no_response|hr_reviewed → closed
    OVERRIDE = "override"              # 任意非终态 → ctx.target_status（人工复核推翻）


@dataclass(frozen=True)
class TransitionContext:
    """补充输入。

    source: 路径判定。**路径相关的事件必须给**，否则抛错（不猜）。
    target_status: OVERRIDE 的目标状态。
    """

    source: str | None = None
    target_status: CandidateStatus | None = None


# 路径无关边：(当前状态, 事件) → 目标状态
_EDGES: dict[tuple[CandidateStatus, StateEvent], CandidateStatus] = {
    # ★ 求简历从 greeted 出发，**两条路径的目标态是同一个**（都是 resume_requested），
    #   所以它实际上与路径无关——放进这张表，调用方不用白给一个 source。
    #   （第一版把它放进了 _PATH_EDGES，于是每条正常路径都被迫传 source，
    #     纯属自找麻烦：路径相关的意义在于**目标态会因路径而异**，这里不是。）
    (S.GREETED, StateEvent.REQUEST_RESUME): S.RESUME_REQUESTED,
    (S.RESUME_REQUESTED, StateEvent.AWAIT_RESUME): S.AWAITING_RESUME,
    (S.AWAITING_RESUME, StateEvent.RECEIVE_RESUME): S.RESUME_RECEIVED,
    # ★ BOSS 实测（2026-10-19）：打招呼之后，候选人可以**主动**提出发附件简历
    #   （聊天页出现「对方想发送附件简历给您，您是否同意」）。我们没求过，所以
    #   不能强行走 request_resume → await_resume 那条链——直接从 greeted /
    #   resume_requested 收下。
    (S.GREETED, StateEvent.RECEIVE_RESUME): S.RESUME_RECEIVED,
    (S.RESUME_REQUESTED, StateEvent.RECEIVE_RESUME): S.RESUME_RECEIVED,
    (S.AWAITING_RESUME, StateEvent.NO_RESPONSE): S.NO_RESPONSE,
    (S.NO_RESPONSE, StateEvent.CLOSE): S.CLOSED,
    (S.RESUME_RECEIVED, StateEvent.HR_REVIEW): S.HR_REVIEWED,
    (S.HR_REVIEWED, StateEvent.CLOSE): S.CLOSED,
}

# 路径相关边：(当前状态, 事件) → {路径: 目标状态}
_PATH_EDGES: dict[tuple[CandidateStatus, StateEvent], dict[str, CandidateStatus]] = {
    # 评分三步：推荐流打分放行打招呼，inbound 打分放行求简历（2026-10-20 起 inbound 也评分）
    (S.NEW, StateEvent.SCREEN_PASS): {RECOMMENDED: S.SCREENED_PASS, INBOUND: S.SCREENED_PASS},
    (S.NEW, StateEvent.REJECT_HARD): {RECOMMENDED: S.REJECTED_HARD, INBOUND: S.REJECTED_HARD},
    (S.NEW, StateEvent.REJECT_LLM): {RECOMMENDED: S.REJECTED_LLM, INBOUND: S.REJECTED_LLM},
    # greet 恒 outbound：BOSS 上只有牛人列表的「立即沟通」能打招呼，且必须先评分通过
    (S.SCREENED_PASS, StateEvent.GREET): {RECOMMENDED: S.GREETED},
    # ★ inbound 跳过 greet：评分通过后直接从 screened_pass 求简历（recommended 仍走 greet）
    (S.SCREENED_PASS, StateEvent.REQUEST_RESUME): {INBOUND: S.RESUME_REQUESTED},
    # ★ BOSS 独有：对方主动发来简历，我们没求过
    (S.NEW, StateEvent.RECEIVE_RESUME): {INBOUND: S.RESUME_RECEIVED},
}


def transition(
    jc: JobCandidate, event: StateEvent, ctx: TransitionContext | None = None
) -> JobCandidate:
    """推进一次迁移，返回更新后的 JobCandidate（**纯函数**：不落库、不改传入对象）。

    非法迁移抛 `InvalidTransition`。
    """
    current = _current_status(jc)

    if not isinstance(event, StateEvent):
        raise InvalidTransition(f"未知事件：{event!r}（须为 StateEvent）")
    if current in TERMINAL:
        raise InvalidTransition(f"终态 {current.value} 一旦进入不再迁移（事件 {event.value}）")
    if event is StateEvent.OVERRIDE:
        return _override(jc, ctx)

    key = (current, event)
    if key in _PATH_EDGES:
        source = ctx.source if ctx is not None else None
        variants = _PATH_EDGES[key]
        if source in variants:
            return _copy_with_status(jc, variants[source])
        raise InvalidTransition(_path_error(event, source, current))

    target = _EDGES.get(key)
    if target is None:
        raise InvalidTransition(f"非法迁移：状态 {current.value} 不接受事件 {event.value}")
    return _copy_with_status(jc, target)


def _override(jc: JobCandidate, ctx: TransitionContext | None) -> JobCandidate:
    """OVERRIDE：任意非终态 → `ctx.target_status`。

    ★ 只守住「终态不再迁移」这条不变式；**来源/目标的业务约束由复核接口负责**。
    """
    target = ctx.target_status if ctx is not None else None
    if target is None:
        raise InvalidTransition("OVERRIDE 须在 ctx.target_status 提供目标状态")
    if not isinstance(target, CandidateStatus):
        raise InvalidTransition(f"非法 override 目标：{target!r}（须为 CandidateStatus）")
    return _copy_with_status(jc, target)


def _current_status(jc: JobCandidate) -> CandidateStatus:
    raw = jc.status
    if isinstance(raw, CandidateStatus):
        return raw
    try:
        return CandidateStatus(raw)
    except ValueError:
        raise InvalidTransition(f"无法识别的当前状态：{raw!r}") from None


def _path_error(event: StateEvent, source: str | None, current: CandidateStatus) -> str:
    if event is StateEvent.GREET:
        return (
            f"greet 仅存在于 recommended 路径（BOSS 上只有牛人列表能打招呼）；"
            f"source={source!r}"
        )
    if event is StateEvent.RECEIVE_RESUME:
        if source is None:
            return (
                "无法判定路径（ctx.source 未给）：receive_resume 的直收边仅存在于 inbound；"
                f"从 awaiting_resume 出发则不需要 source（当前 {current.value}）"
            )
        return f"该边不适用于 source={source!r}（当前 {current.value}）"
    return f"无法判定合法路径（source={source!r}，当前 {current.value}）"


def _copy_with_status(jc: JobCandidate, new_status: CandidateStatus) -> JobCandidate:
    """按列值构造新的 JobCandidate（detached），status 写字符串值。

    ★ 保留已挂载的 relationship，供后续迁移的路径判定复用。
    """
    updated = JobCandidate(
        **{col.name: getattr(jc, col.name) for col in jc.__table__.columns}
    )
    for name in ("job", "candidate"):
        value = jc.__dict__.get(name)
        if value is not None:
            setattr(updated, name, value)
    updated.status = new_status.value
    return updated
