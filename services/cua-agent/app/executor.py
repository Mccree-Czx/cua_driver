"""执行器：任务类型 → 驱动方法调用 + 成功判据 + evidence/artifact 组装
（纯编排，副作用全部经 ExecutorDeps 注入）。

execute(task, deps) -> TaskResult：
- outcome=success 时 evidence 按 pipeline orchestrator 的消费契约组装
  （模块 docstring 的 evidence 契约段逐字段对齐）；
- 动作/校验失败抛 TaskExecutionError（携带已烧 token 用量，worker 据此
  组装 failed 结果）；BrainUnavailableError 原样上抛（worker 降级为
  deferred 重判，不按动作失败计）。

每类型的"动作后截图 → verify"：
- 判定型动作（check_login / check_attachment）返回 bool 即页面判定结果，
  判据随结果取正/负两种形态——校验确认的是"页面形态与本次判定一致"，
  负向判定（未登录/无附件）也是合法结果，不按失败处理；
- 读取/触达型动作（其余 4 类）判据为固定成功形态，verify 不通过按动作
  失败处理。

evidence 契约（消费方 pipeline orchestrator docstring，逐字段）：
- READ_RESUME 成功：{"resume": MinimalResume 7 字段, "screenshot_keys": [...],
  brain_tokens, cost_est}——截图先经 artifact 上传（kind=snapshot），
  取回 object_key 填入 screenshot_keys；
- SEND_MESSAGE 成功：{"sent_at": ISO str, brain_tokens, cost_est}；
- CHECK_ATTACHMENT 成功：{"has_attachment": bool, brain_tokens, cost_est}；
- CHECK_LOGIN / LIST_UNREAD / DOWNLOAD_ATTACHMENT：pipeline 暂无消费
  handler（T10 编排），形状 {"logged_in": bool} / {"unread_ids": [...]} /
  {"filename": str} + 账目——T10 对齐点。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from uuid import UUID

from hr_workbuddy import AtomicTask, AtomicTaskType, BrainClient, LiepinDriver, TaskResult

from app.brain.openai_brain import BrainUnavailableError
from app.brain.usage import BrainUsage
from app.cost import accumulate
from app.verify import verify_success


class TaskExecutionError(Exception):
    """动作/校验失败：携带已烧 token 用量（worker 组装 failed evidence）。

    post_send / sent_at：SEND_MESSAGE 的 perform 已返回（消息已发出）之后
    的失败标记——worker 据此置 failed_needs_manual 且不 raise Retry
    （arq 同 payload 重跑会再次真实发送同一消息；一人一消息只防第二条
    任务的落库，防不住同一任务的重发）。
    """

    def __init__(
        self,
        message: str,
        usage: BrainUsage,
        *,
        post_send: bool = False,
        sent_at: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.usage = usage
        self.post_send = post_send
        self.sent_at = sent_at


@dataclass(frozen=True)
class Artifact:
    """待上传的 artifact：kind（snapshot|resume）+ 文件名 + 字节（不透明透传）。"""

    kind: str
    filename: str
    data: bytes


@dataclass
class ExecutorDeps:
    """执行器副作用注入点：驱动 / 大脑 / 截图源 / 上传回调 / 定价 / 时钟。"""

    driver: LiepinDriver
    brain: BrainClient  # verify_success 优先走 verify_with_usage 扩展（用量账目）
    capture: Callable[[], bytes]  # 动作后截图（mock：确定性 PNG；real：SDK 桌面截图 T12）
    upload_artifact: Callable[[UUID, str, str, bytes], str]  # (task_id, kind, filename, data) -> object_key
    price_per_1k_tokens: float = 0.0
    now: Callable[[], datetime] = datetime.now


def _candidate_id(task: AtomicTask) -> str:
    cid = task.candidate_liepin_id or task.context.get("candidate_liepin_id")
    if not cid:
        raise ValueError(f"{task.type.value} 任务缺 candidate_liepin_id")
    return str(cid)


def _send_text(task: AtomicTask) -> str:
    text = task.context.get("text")
    if not text:
        raise ValueError("SEND_MESSAGE 任务 context 缺渲染文本 text")
    return str(text)


def _page_capture(deps: ExecutorDeps, raw: Any) -> bytes:
    """动作后另拍页面截图（判定型/触达型/下载型动作）。"""
    return deps.capture()


def _resume_png(deps: ExecutorDeps, raw: Any) -> bytes:
    """READ_RESUME 的动作返回值即页面截图（同字节上传 snapshot）。"""
    return raw[0]


@dataclass(frozen=True)
class ActionSpec:
    """单任务类型的编排映射：动作 → 判据 → evidence → artifact。"""

    perform: Callable[[LiepinDriver, AtomicTask], Any]
    screenshot: Callable[[ExecutorDeps, Any], bytes]
    criteria: Callable[[Any], str]
    evidence: Callable[[ExecutorDeps, Any], dict]
    artifact: Callable[[AtomicTask, Any], Artifact | None]


def _none_artifact(task: AtomicTask, raw: Any) -> None:
    return None


def _resume_evidence(deps: ExecutorDeps, raw: Any) -> dict:
    return {"resume": raw[1].model_dump()}


def _sent_evidence(deps: ExecutorDeps, raw: Any) -> dict:
    return {"sent_at": deps.now().isoformat()}


def _resume_artifact(task: AtomicTask, raw: Any) -> Artifact:
    return Artifact(kind="snapshot", filename=f"snapshot-{task.task_id}.png", data=raw[0])


def _attachment_artifact(task: AtomicTask, raw: Any) -> Artifact:
    # R7：文件名来自驱动返回（剧本附件定义）；字节不透明透传，绝不解析/预览
    return Artifact(kind="resume", filename=raw[1], data=raw[0])


_ACTIONS: dict[AtomicTaskType, ActionSpec] = {
    AtomicTaskType.CHECK_LOGIN: ActionSpec(
        perform=lambda d, t: d.check_login(),
        screenshot=_page_capture,
        criteria=lambda r: "页面处于已登录状态" if r else "页面未显示已登录状态",
        evidence=lambda d, r: {"logged_in": r},
        artifact=_none_artifact,
    ),
    AtomicTaskType.LIST_UNREAD: ActionSpec(
        perform=lambda d, t: d.list_unread_conversations(),
        screenshot=_page_capture,
        criteria=lambda r: "消息列表页已打开，可见未读会话列表",
        evidence=lambda d, r: {"unread_ids": r},
        artifact=_none_artifact,
    ),
    AtomicTaskType.READ_RESUME: ActionSpec(
        perform=lambda d, t: d.read_online_resume(_candidate_id(t)),
        screenshot=_resume_png,
        criteria=lambda r: "会话页已打开，在线简历内容已完整显示",
        evidence=_resume_evidence,
        artifact=_resume_artifact,
    ),
    AtomicTaskType.SEND_MESSAGE: ActionSpec(
        perform=lambda d, t: d.send_message(_candidate_id(t), _send_text(t)),
        screenshot=_page_capture,
        criteria=lambda r: "消息已成功发送并显示在会话中",
        evidence=_sent_evidence,
        artifact=_none_artifact,
    ),
    AtomicTaskType.CHECK_ATTACHMENT: ActionSpec(
        perform=lambda d, t: d.check_attachment(_candidate_id(t)),
        screenshot=_page_capture,
        criteria=lambda r: "对方已发送简历附件" if r else "会话中尚无对方发送的简历附件",
        evidence=lambda d, r: {"has_attachment": r},
        artifact=_none_artifact,
    ),
    AtomicTaskType.DOWNLOAD_ATTACHMENT: ActionSpec(
        perform=lambda d, t: d.download_attachment(_candidate_id(t)),
        screenshot=_page_capture,
        criteria=lambda r: "附件已成功下载，内容完整",
        evidence=lambda d, r: {"filename": r[1]},
        artifact=_attachment_artifact,
    ),
}


def execute(task: AtomicTask, deps: ExecutorDeps) -> TaskResult:
    """执行单任务：动作 → 截图 → verify → evidence → artifact 上传。

    成功返回 outcome=success；动作/校验失败抛 TaskExecutionError（含用量）；
    BrainUnavailableError 原样上抛（worker 降级 deferred，不吞）——例外：
    SEND_MESSAGE 的 perform 已返回（消息已发出）之后大脑不可用，转为
    post_send=True 的 TaskExecutionError（deferred 重跑会重发消息）。
    """
    spec = _ACTIONS[task.type]
    usage = BrainUsage()
    post_send = False  # perform 已返回 = 动作已真实发生（SEND_MESSAGE 即消息已发出）
    sent_at: str | None = None
    try:
        raw = spec.perform(deps.driver, task)
        if task.type is AtomicTaskType.SEND_MESSAGE:
            post_send = True  # 驱动契约：send_message 返回即发送完成
            sent_at = deps.now().isoformat()
        screenshot = spec.screenshot(deps, raw)
        verdict = verify_success(screenshot, spec.criteria(raw), deps.brain)
        usage = usage + verdict.usage
        if not verdict.ok:
            raise TaskExecutionError(
                f"{task.type.value} 动作后校验未通过：{spec.criteria(raw)}",
                usage,
                post_send=post_send,
                sent_at=sent_at,
            )
        evidence = spec.evidence(deps, raw)
        accumulate(evidence, usage, deps.price_per_1k_tokens)
        artifact = spec.artifact(task, raw)
        if artifact is not None:
            key = deps.upload_artifact(task.task_id, artifact.kind, artifact.filename, artifact.data)
            evidence["screenshot_keys"] = evidence.get("screenshot_keys", []) + [key]
        return TaskResult(
            task_id=task.task_id, outcome="success", evidence=evidence, error=None
        )
    except TaskExecutionError:
        raise
    except BrainUnavailableError as e:
        if post_send:
            raise TaskExecutionError(
                f"{task.type.value} 发送后视觉大脑不可用：{e}",
                usage,
                post_send=True,
                sent_at=sent_at,
            ) from e
        raise
    except Exception as e:
        raise TaskExecutionError(
            f"{task.type.value} 动作失败：{e}",
            usage,
            post_send=post_send,
            sent_at=sent_at,
        ) from e
