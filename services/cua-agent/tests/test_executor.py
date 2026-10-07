"""worker + executor：动作前延时 / 令牌桶权威检查 / 动作后截图校验 /
重试策略 / evidence 契约 / artifact 回调。

全 mock：FakeLiepinDriver + MockBrain + 内存 fake 桶/重排/管线，无真实
网络 / Redis / MinIO 依赖；延时与睡眠注入，不真 sleep。

evidence 契约（对照 pipeline orchestrator docstring 逐字段）：
- READ_RESUME 成功：resume（MinimalResume 7 字段）+ screenshot_keys
  + brain_tokens + cost_est
- SEND_MESSAGE 成功：sent_at（ISO str）+ brain_tokens + cost_est
- CHECK_ATTACHMENT 成功：has_attachment + brain_tokens + cost_est
"""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from arq.worker import Retry

from app.brain.mock import MockBrain
from app.brain.openai_brain import BrainUnavailableError
from app.brain.usage import BrainUsage
from app.drivers.cua_sdk import (
    LocatorFailedError,
    RiskControlDetectedError,
    WindowUnavailableError,
    _translate_driver_error,
    has_risk_control,
)
from app.drivers.fake import FakeLiepinDriver
from app.fallback import FallbackOutcome, LlmFallback
from app.worker import WorkerDeps, execute_task, run_task
from app.executor import ExecutorDeps
from app.world import AttachmentSpec, ConversationScript, ReplyEvent, World
from hr_workbuddy import AtomicTask, AtomicTaskType, FallbackSuggestion, MinimalResume

TASK_ID = uuid4()
CANDIDATE = "uid_a"
SNAPSHOT_KEY = "snapshots/uid_a/20261005_120000.png"


def make_resume(uid: str) -> MinimalResume:
    """恰 7 字段的最小简历 fixture。"""
    return MinimalResume(
        name=f"候选人{uid}",
        liepin_user_id=uid,
        education="本科",
        years_of_experience="3年",
        city="杭州",
        salary="20-30K",
        experience_summary="3 年后端开发经验",
    )


def make_world(**overrides) -> World:
    """两会话剧本：uid_a 未读+简历、uid_b 已读无简历；默认登录态。"""
    defaults = dict(
        login_state=True,
        conversations=[
            ConversationScript(liepin_user_id="uid_a", unread=True, resume_fixture="fixture_a"),
            ConversationScript(liepin_user_id="uid_b", unread=False, resume_fixture=None),
        ],
        resume_fixtures={"fixture_a": make_resume("uid_a")},
        reply_timeline="never",
    )
    defaults.update(overrides)
    return World(**defaults)


def make_task(
    task_type: AtomicTaskType,
    *,
    attempt: int = 0,
    max_attempts: int = 3,
    candidate_liepin_id: str | None = CANDIDATE,
    context: dict | None = None,
) -> AtomicTask:
    return AtomicTask(
        task_id=TASK_ID,
        type=task_type,
        job_id=1,
        job_candidate_id=1,
        candidate_liepin_id=candidate_liepin_id,
        context=context or {},
        attempt=attempt,
        max_attempts=max_attempts,
    )


# —— 内存替身：桶 / 重排 / 延时 / 管线 ——


class FakeBucket:
    """脚本化令牌桶：按序返回预设判定，记录 (key, rate, window) 调用。"""

    def __init__(self, *verdicts: bool) -> None:
        self.verdicts: list[bool] = list(verdicts)
        self.calls: list[tuple[str, int, int]] = []

    async def acquire_async(self, key: str, rate_per_window: int, window_seconds: int) -> bool:
        self.calls.append((key, rate_per_window, window_seconds))
        return self.verdicts.pop(0) if self.verdicts else True


class FakeRequeue:
    """重排记录器：记录 (task, defer_seconds)，不真入队。"""

    def __init__(self) -> None:
        self.calls: list[tuple[AtomicTask, int]] = []

    async def __call__(self, task: AtomicTask, defer_seconds: int) -> None:
        self.calls.append((task, defer_seconds))


class FakeRiskGate:
    """全局风控熔断记录器：paused 可预置；pause() 记录并翻转。"""

    def __init__(self, paused: bool = False) -> None:
        self.paused = paused
        self.pause_calls = 0

    async def is_paused(self) -> bool:
        return self.paused

    async def pause(self) -> None:
        self.pause_calls += 1
        self.paused = True


class FakeFallback:
    """兜底记录器：recovers 预置 outcome；记录 (task, error) 调用。"""

    def __init__(self, outcome: FallbackOutcome | None = None) -> None:
        self._outcome = outcome
        self.calls: list = []

    def recover(self, task: AtomicTask, error: Exception) -> FallbackOutcome | None:
        self.calls.append((task, error))
        return self._outcome


class FakePipeline:
    """假 pipeline HTTP：记录 result / artifact 回调，artifact 返回预设对象键。"""

    def __init__(self, object_key: str = SNAPSHOT_KEY) -> None:
        self.object_key = object_key
        self.results: list = []
        self.artifacts: list = []

    def post_result(self, task_id, result) -> None:
        self.results.append((task_id, result))

    def post_artifact(self, task_id, kind: str, filename: str, data: bytes) -> str:
        self.artifacts.append((task_id, kind, filename, data))
        return self.object_key


class FakeSleep:
    """睡眠记录器（不真睡）。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class FakeUniform:
    """均匀随机源记录器：记录 (lo, hi) 并返回固定值 0。"""

    def __init__(self) -> None:
        self.calls: list[tuple[float, float]] = []

    def __call__(self, lo: float, hi: float) -> float:
        self.calls.append((lo, hi))
        return 0.0


def make_deps(
    *,
    bucket: FakeBucket | None = None,
    uniform: FakeUniform | None = None,
    risk_gate: FakeRiskGate | None = None,
    fallback: FakeFallback | None = None,
) -> dict:
    """组装 WorkerDeps：driver 读世界剧本，brain 默认通过，全部副作用注入。"""
    world = make_world()
    driver = FakeLiepinDriver(world)
    brain = MockBrain()
    pipeline = FakePipeline()
    sleep = FakeSleep()
    uniform = uniform or FakeUniform()
    requeue = FakeRequeue()
    bucket = bucket or FakeBucket(True)
    risk_gate = risk_gate or FakeRiskGate()
    clock = lambda: 3600.0 * 10 + 30.0  # 任意固定时刻：距下一小时边界 3570s
    now = lambda: datetime(2026, 10, 5, 10, 0, 30, tzinfo=timezone.utc)
    executor = ExecutorDeps(
        driver=driver,
        brain=brain,
        capture=lambda: b"\x89PNG\r\n\x1a\nfake-capture",
        upload_artifact=pipeline.post_artifact,
        price_per_1k_tokens=0.002,
        now=now,
        fallback=fallback,
    )
    return dict(
        driver=driver,
        world=world,
        brain=brain,
        pipeline=pipeline,
        sleep=sleep,
        uniform=uniform,
        now=now,
        risk_gate=risk_gate,
        fallback=fallback,
        deps=WorkerDeps(
            executor=executor,
            pipeline=pipeline,
            bucket=bucket,
            requeue=requeue,
            risk_paused=risk_gate.is_paused,
            risk_pause=risk_gate.pause,
            uniform=uniform,
            sleep=sleep,
            clock=clock,
            now=now,
        ),
    )


def run(task: AtomicTask, fixtures: dict, *, attempt: int = 0):
    return asyncio.run(run_task(task, fixtures["deps"], attempt=attempt))


# —— 成功路径与 evidence 契约 ——


def test_read_resume_success_evidence_and_snapshot_artifact():
    """READ_RESUME 成功：evidence 七字段 + screenshot_keys + 账目；
    PNG 截图以 kind=snapshot 上传 fake pipeline 并取回对象键。"""
    fixtures = make_deps()
    result = run(make_task(AtomicTaskType.READ_RESUME), fixtures)

    assert result.outcome == "success"
    resume = result.evidence["resume"]
    assert set(resume) == {
        "name",
        "liepin_user_id",
        "education",
        "years_of_experience",
        "city",
        "salary",
        "experience_summary",
    }  # 恰 7 字段（pipeline _minimal_from_evidence 逐字段消费）
    assert result.evidence["screenshot_keys"] == [SNAPSHOT_KEY]
    assert result.evidence["brain_tokens"] == 0
    assert result.evidence["cost_est"] == 0.0
    assert result.error is None

    pipeline = fixtures["pipeline"]
    (task_id, kind, filename, data), = pipeline.artifacts
    assert task_id == TASK_ID
    assert kind == "snapshot"
    assert data == fixtures["driver"].read_online_resume(CANDIDATE)[0]  # 上传即截图原字节
    assert filename.endswith(".png")
    (posted_task_id, posted_result), = pipeline.results
    assert posted_task_id == TASK_ID and posted_result is result


def test_send_message_success_evidence_sent_at():
    """SEND_MESSAGE 成功：sent_at ISO str + 账目；消息经驱动发出。"""
    fixtures = make_deps()
    task = make_task(
        AtomicTaskType.SEND_MESSAGE,
        context={"text": "您好，方便发一份简历吗？", "candidate_liepin_id": CANDIDATE},
    )
    result = run(task, fixtures)

    assert result.outcome == "success"
    assert result.evidence["sent_at"] == fixtures["now"]().isoformat()
    assert fixtures["driver"].sent_messages == [(CANDIDATE, "您好，方便发一份简历吗？")]


def test_send_message_native_channel_calls_request_resume():
    """SEND_MESSAGE(native_channel)：走平台「向TA索要」而非自定义文本发送（M2 定稿）。"""
    fixtures = make_deps()
    task = make_task(
        AtomicTaskType.SEND_MESSAGE,
        context={
            "text": "平台系统文案（记录用）",
            "candidate_liepin_id": CANDIDATE,
            "variant": "greet_request",
            "native_channel": True,
        },
    )
    result = run(task, fixtures)

    assert result.outcome == "success"
    assert fixtures["driver"].resume_requests == [CANDIDATE]
    assert fixtures["driver"].sent_messages == [
        (
            CANDIDATE,
            "你好~我这里有个职位很适合你，待遇优厚，了解一下吗？期待回复！\n"
            "我想要一份你的简历，你是否同意？",
        )
    ]


def test_read_resume_prefer_recommend_passthrough():
    """READ_RESUME(context.source=recommended) → prefer_recommend=True 透传
    （2026-10-07 风控修复：推荐人直连推荐页，不碰批量页）。"""
    fixtures = make_deps()
    result = run(
        make_task(AtomicTaskType.READ_RESUME, context={"source": "recommended"}),
        fixtures,
    )

    assert result.outcome == "success"
    assert fixtures["driver"].read_prefer_flags == [True]


def test_read_resume_default_no_prefer():
    """READ_RESUME 无 source 上下文（inbound 等）→ prefer_recommend=False（批量页主路径）。"""
    fixtures = make_deps()
    result = run(make_task(AtomicTaskType.READ_RESUME, context={}), fixtures)

    assert result.outcome == "success"
    assert fixtures["driver"].read_prefer_flags == [False]


def test_check_attachment_negative_is_success():
    """CHECK_ATTACHMENT 无附件：success + has_attachment=False（pipeline 等下一轮巡检）。"""
    fixtures = make_deps()
    result = run(make_task(AtomicTaskType.CHECK_ATTACHMENT), fixtures)
    assert result.outcome == "success"
    assert result.evidence["has_attachment"] is False


def test_check_login_and_list_unread_evidence_shapes():
    """CHECK_LOGIN / LIST_UNREAD 映射：evidence 形状（T10 编排对齐点）。"""
    fixtures = make_deps()
    login = run(make_task(AtomicTaskType.CHECK_LOGIN), fixtures)
    assert login.outcome == "success"
    assert login.evidence["logged_in"] is True
    unread = run(make_task(AtomicTaskType.LIST_UNREAD), fixtures)
    assert unread.outcome == "success"
    assert unread.evidence["unread_ids"] == ["uid_a"]


def test_download_attachment_uploads_resume_artifact_with_filename():
    """DOWNLOAD_ATTACHMENT 成功：字节按不透明透传，kind=resume + 剧本文件名（R7）。"""
    world = make_world(
        reply_timeline=[
            ReplyEvent(tick=0, conversation_id=CANDIDATE, attachment=AttachmentSpec(file_name="简历.pdf"))
        ]
    )
    fixtures = make_deps()
    fixtures["deps"].executor.driver = FakeLiepinDriver(world)
    result = run(make_task(AtomicTaskType.DOWNLOAD_ATTACHMENT), fixtures)

    assert result.outcome == "success"
    assert result.evidence["filename"] == "简历.pdf"
    (task_id, kind, filename, data), = fixtures["pipeline"].artifacts
    assert task_id == TASK_ID and kind == "resume" and filename == "简历.pdf"
    script_bytes, _ = FakeLiepinDriver(world).download_attachment(CANDIDATE)  # R7：(字节, 文件名)
    assert data == script_bytes  # 原字节透传，不解析/不预览（T8 遗留：占位 PDF）


# —— 失败重试：3 次重试后 → failed_needs_manual ——


def _fail_driver(deps: WorkerDeps) -> None:
    """让 driver 抛错（未知会话）。"""

    def boom(*args, **kwargs):
        raise RuntimeError("driver 动作失败")

    deps.executor.driver.read_online_resume = boom


def test_action_failure_below_max_attempts_raises_arq_retry():
    """attempt < max_attempts：结果 failed_retryable 已回调，arq Retry 重试。"""
    fixtures = make_deps()
    _fail_driver(fixtures["deps"])
    task = make_task(AtomicTaskType.READ_RESUME, attempt=2)

    with pytest.raises(Retry):
        run(task, fixtures, attempt=2)
    (_, posted), = fixtures["pipeline"].results
    assert posted.outcome == "failed_retryable"
    assert "驱动" in posted.error or "失败" in posted.error


def test_action_failure_at_max_attempts_needs_manual():
    """attempt 已达 max_attempts=3：failed_needs_manual，不再重试（不抛 Retry）。"""
    fixtures = make_deps()
    _fail_driver(fixtures["deps"])
    task = make_task(AtomicTaskType.READ_RESUME, attempt=3)

    result = run(task, fixtures, attempt=3)
    assert result.outcome == "failed_needs_manual"
    assert result.error is not None
    (_, posted), = fixtures["pipeline"].results
    assert posted.outcome == "failed_needs_manual"


# —— 真实模式门禁 ①：SEND_MESSAGE 发送后失败不重试（防同一任务重发）——


def test_send_message_post_send_verify_failure_needs_manual_no_resend():
    """SEND_MESSAGE 发送后 verify 失败：failed_needs_manual 且不 raise Retry。

    消息已真实发出，arq 同 payload 重跑会重发同一消息（一人一消息只防第二条
    任务的落库，防不住同一任务的重发）——evidence 带 post_send_failure 与
    sent_at，fake 驱动记录 send 调用恰 1 次。
    """
    fixtures = make_deps()
    fixtures["deps"].executor.brain = MockBrain(
        script=lambda screenshot, criteria: False
    )
    task = make_task(
        AtomicTaskType.SEND_MESSAGE,
        context={"text": "您好", "candidate_liepin_id": CANDIDATE},
    )

    result = run(task, fixtures)  # 不抛 Retry

    assert result.outcome == "failed_needs_manual"
    assert result.evidence["post_send_failure"] is True
    assert result.evidence["sent_at"] == fixtures["now"]().isoformat()
    assert result.evidence["attempt"] == 0
    assert fixtures["driver"].sent_messages == [(CANDIDATE, "您好")]  # 恰发一次
    (_, posted), = fixtures["pipeline"].results
    assert posted.outcome == "failed_needs_manual"
    assert posted.evidence["post_send_failure"] is True


def test_send_message_post_send_brain_unavailable_needs_manual_no_resend():
    """SEND_MESSAGE 发送后大脑不可用：failed_needs_manual，不 defer 重判不重发。"""
    fixtures = make_deps()
    fixtures["deps"].executor.brain = MockBrain(
        script=lambda screenshot, criteria: (_ for _ in ()).throw(
            BrainUnavailableError("供应商 500")
        )
    )
    task = make_task(
        AtomicTaskType.SEND_MESSAGE,
        context={"text": "您好", "candidate_liepin_id": CANDIDATE},
    )

    result = run(task, fixtures)

    assert result.outcome == "failed_needs_manual"
    assert result.evidence["post_send_failure"] is True
    assert fixtures["driver"].sent_messages == [(CANDIDATE, "您好")]
    assert fixtures["deps"].requeue.calls == []  # 不 deferred 重判（重判会重发）


def test_send_message_pre_send_driver_failure_still_retries():
    """SEND_MESSAGE 发送前驱动抛错（send 未完成）：failed_retryable + Retry 语义不变。"""
    fixtures = make_deps()

    def boom(candidate_liepin_id, text):
        raise RuntimeError("driver 动作失败")

    fixtures["deps"].executor.driver.send_message = boom
    task = make_task(
        AtomicTaskType.SEND_MESSAGE,
        context={"text": "您好", "candidate_liepin_id": CANDIDATE},
    )

    with pytest.raises(Retry):
        run(task, fixtures, attempt=0)
    (_, posted), = fixtures["pipeline"].results
    assert posted.outcome == "failed_retryable"
    assert posted.evidence.get("post_send_failure") is None
    assert fixtures["driver"].sent_messages == []  # 消息未发出


def test_read_task_verify_failure_still_retryable():
    """读类任务 verify 失败：仍 failed_retryable + Retry（重试语义不变）。"""
    fixtures = make_deps()
    fixtures["deps"].executor.brain = MockBrain(
        script=lambda screenshot, criteria: False
    )

    with pytest.raises(Retry):
        run(make_task(AtomicTaskType.READ_RESUME), fixtures, attempt=0)
    (_, posted), = fixtures["pipeline"].results
    assert posted.outcome == "failed_retryable"
    assert posted.evidence.get("post_send_failure") is None
    assert posted.evidence["attempt"] == 0


# —— evidence 账目：attempt 与 duration_s 传递（pipeline 按 (task_id, attempt) 落账）——


class _AdvancingClock:
    """每次调用推进 0.25s 的时钟：duration_s 可控可断言。"""

    def __init__(self) -> None:
        self.t = 10.0
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        self.t += 0.25
        return self.t


def test_success_evidence_carries_attempt_and_duration_s():
    """evidence 带折算 attempt（task.attempt + job_try - 1）与 duration_s（耗时秒）。"""
    fixtures = make_deps()
    clock = _AdvancingClock()
    fixtures["deps"].clock = clock

    result = run(make_task(AtomicTaskType.READ_RESUME), fixtures, attempt=2)

    assert result.evidence["attempt"] == 2
    assert result.evidence["duration_s"] == pytest.approx(0.25)
    assert clock.calls == 2  # 起止各取一次


# —— 空桶 → defer 重排（不发消息、不失败） ——


def test_empty_bucket_defers_without_sending():
    """触达前权威检查：桶耗尽 → _defer_by 重排自身 + 返回标记 deferred 的结果；
    不失败、不发消息、不回调 pipeline。"""
    fixtures = make_deps(bucket=FakeBucket(False))
    task = make_task(
        AtomicTaskType.SEND_MESSAGE,
        context={"text": "您好", "candidate_liepin_id": CANDIDATE},
    )
    result = run(task, fixtures)

    assert result.outcome == "failed_retryable"
    assert result.evidence["deferred"] is True
    assert fixtures["driver"].sent_messages == []  # 不得绕过桶直接发
    assert fixtures["pipeline"].results == []  # deferred 不回调（无失败账）
    (requeued, defer_seconds), = fixtures["deps"].requeue.calls
    assert requeued == task
    assert defer_seconds == 3570  # 距下一小时窗口（桶随 {date-hour} 键回填）
    (key, rate, window), = fixtures["deps"].bucket.calls
    assert key == "msg-touch:2026100510"
    assert rate == 20 and window == 3600


def test_brain_unavailable_defers_reevaluate():
    """视觉大脑不可用（BrainUnavailableError）→ deferred 重判，不按动作失败重试。"""
    fixtures = make_deps()
    fixtures["deps"].executor.brain = MockBrain(
        script=lambda screenshot, criteria: (_ for _ in ()).throw(BrainUnavailableError("供应商 500"))
    )
    result = run(make_task(AtomicTaskType.READ_RESUME), fixtures)

    assert result.outcome == "failed_retryable"
    assert result.evidence["deferred"] is True
    (requeued, defer_seconds), = fixtures["deps"].requeue.calls
    assert requeued.task_id == TASK_ID and defer_seconds == 60


# —— 动作前延时：触达 10-60s / 读 5-15s 均匀随机，注入不真 sleep ——


def test_delay_touch_range():
    fixtures = make_deps()
    task = make_task(AtomicTaskType.SEND_MESSAGE, context={"text": "您好"})
    run(task, fixtures)
    (lo, hi), = fixtures["uniform"].calls
    assert (lo, hi) == (10.0, 60.0)
    assert fixtures["sleep"].calls == [0.0]  # 注入随机源的返回值，不真 sleep


def test_delay_read_range():
    fixtures = make_deps()
    run(make_task(AtomicTaskType.READ_RESUME), fixtures)
    (lo, hi), = fixtures["uniform"].calls
    assert (lo, hi) == (5.0, 15.0)
    assert fixtures["sleep"].calls == [0.0]


# —— arq 入口：payload 解析 + job_try 折算 attempt ——


def test_execute_task_derives_attempt_from_job_try():
    """execute_task 从 payload 解析 AtomicTask；effective attempt =
    task.attempt + job_try - 1（arq 重试不重写 payload，attempt 靠 job_try 折算）。"""
    fixtures = make_deps()
    _fail_driver(fixtures["deps"])
    payload = make_task(AtomicTaskType.READ_RESUME, attempt=0).model_dump(mode="json")

    with pytest.raises(Retry):  # job_try=3 → attempt 2 → 未达上限，重试
        asyncio.run(
            execute_task({"worker_deps": fixtures["deps"], "job_try": 3}, payload)
        )
    result = asyncio.run(
        execute_task({"worker_deps": fixtures["deps"], "job_try": 4}, payload)
    )
    assert result.outcome == "failed_needs_manual"  # job_try=4 → attempt 3 → 达上限


# —— 风控防线（2026-10-06 实测教训：连续高频操作触发平台安全验证）——


def test_has_risk_control_detects_security_page_markers():
    """风控页判据纯函数：命中「账号行为异常/猎聘安全中心/图形验证码」任一即 True。"""
    assert has_risk_control("猎聘安全中心发现您的帐号存在异常行为……请点击下方图形验证码") is True
    assert has_risk_control("账号行为异常") is True
    assert has_risk_control("消息列表页正常展示") is False


def test_risk_control_error_fails_manual_without_retry():
    """风控检测：failed_needs_manual + 不 raise Retry + 回调落账（人工接管，绝不自动重试）。"""
    fixtures = make_deps()

    def boom(*args, **kwargs):
        raise RiskControlDetectedError("检测到账号行为异常验证页")

    fixtures["deps"].executor.driver.read_online_resume = boom
    result = run(make_task(AtomicTaskType.READ_RESUME), fixtures)  # 不抛 Retry

    assert result.outcome == "failed_needs_manual"
    assert result.evidence["risk_control"] is True
    assert result.evidence["attempt"] == 0
    (_, posted), = fixtures["pipeline"].results
    assert posted.outcome == "failed_needs_manual"
    assert fixtures["deps"].requeue.calls == []  # 不 deferred 重排


def test_task_gap_applied_at_end_of_run():
    """任务间隔闸：run_task 结束时按 deps.task_gap_seconds 冷却（真实默认 30s）。"""
    fixtures = make_deps()
    fixtures["deps"].task_gap_seconds = 7.5
    run(make_task(AtomicTaskType.CHECK_LOGIN), fixtures)
    assert 7.5 in fixtures["sleep"].calls


def test_retry_uses_configured_defer_not_immediate():
    """失败重试按 retry_defer_seconds 延后（禁止 0 秒快速连重试）。"""
    fixtures = make_deps()
    fixtures["deps"].retry_defer_seconds = 90
    _fail_driver(fixtures["deps"])
    with pytest.raises(Retry) as excinfo:
        run(make_task(AtomicTaskType.READ_RESUME), fixtures)
    assert excinfo.value.defer_score == 90_000


# —— 全局熔断 / 窗口不可达 / LLM 兜底（2026-10-06 二次风控事件防线加固）——


def test_risk_control_writes_global_pause_flag():
    """风控检测：写全局熔断标志（risk_pause 被调）+ failed_needs_manual。"""
    fixtures = make_deps()

    def boom(*args, **kwargs):
        raise RiskControlDetectedError("检测到账号行为异常验证页")

    fixtures["deps"].executor.driver.read_online_resume = boom
    result = run(make_task(AtomicTaskType.READ_RESUME), fixtures)

    assert result.outcome == "failed_needs_manual"
    assert fixtures["risk_gate"].pause_calls == 1
    assert fixtures["risk_gate"].paused is True


def test_globally_paused_task_skips_without_driver_calls():
    """全局熔断中：任务零操作（driver 禁调）+ 直接转人工 + 已回调落账 + 零延时。"""
    fixtures = make_deps(risk_gate=FakeRiskGate(paused=True))

    def forbidden(*args, **kwargs):
        raise AssertionError("熔断中不得调用 driver")

    fixtures["deps"].executor.driver.list_unread_conversations = forbidden
    result = run(make_task(AtomicTaskType.LIST_UNREAD), fixtures)

    assert result.outcome == "failed_needs_manual"
    assert result.evidence["globally_paused"] is True
    assert result.evidence["risk_control"] is True
    (_, posted), = fixtures["pipeline"].results
    assert posted.outcome == "failed_needs_manual"
    assert fixtures["sleep"].calls == []  # 连动作前延时都不发生


def test_window_unavailable_fails_manual_without_retry():
    """窗口不可达（off_space）：保守化 failed_needs_manual，不重试、不全局熔断。"""
    fixtures = make_deps()

    def boom(*args, **kwargs):
        raise WindowUnavailableError("窗口不可达（off_space_or_ax_unresolved）")

    fixtures["deps"].executor.driver.read_online_resume = boom
    result = run(make_task(AtomicTaskType.READ_RESUME), fixtures)  # 不抛 Retry

    assert result.outcome == "failed_needs_manual"
    assert result.evidence["window_unavailable"] is True
    assert fixtures["risk_gate"].pause_calls == 0
    (_, posted), = fixtures["pipeline"].results
    assert posted.outcome == "failed_needs_manual"


def test_translate_driver_error_maps_off_space_code():
    """SDK 错误翻译纯函数：off_space_or_ax_unresolved → WindowUnavailableError；其余原样。"""

    class OffSpaceStub(Exception):
        error_code = "off_space_or_ax_unresolved"

    class OtherStub(Exception):
        error_code = "invalid_tree"

    assert isinstance(_translate_driver_error(OffSpaceStub("x")), WindowUnavailableError)
    other = OtherStub("x")
    assert _translate_driver_error(other) is other
    plain = RuntimeError("普通错误")
    assert _translate_driver_error(plain) is plain


def test_locator_failure_llm_fallback_recovers():
    """读取链定位失败 → LLM 兜底执行修复 → 重试 perform 成功 → success + 兜底账目。"""
    calls = {"n": 0}
    fixtures = make_deps(
        fallback=FakeFallback(
            FallbackOutcome(
                diagnosis="需先勾选候选人再点浏览简历",
                action="click_text",
                target="全部",
                confidence=0.9,
                executed=True,
                usage=BrainUsage(prompt_tokens=30, completion_tokens=12, total_tokens=42),
            )
        )
    )
    real = fixtures["driver"].list_unread_conversations

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LocatorFailedError("聊天页未找到「浏览简历」按钮")
        return real()

    fixtures["deps"].executor.driver.list_unread_conversations = flaky
    result = run(make_task(AtomicTaskType.LIST_UNREAD), fixtures)

    assert result.outcome == "success"
    assert calls["n"] == 2  # 首次失败 + 兜底后重试一次
    assert len(fixtures["fallback"].calls) == 1
    assert result.evidence["llm_fallback"]["action"] == "click_text"
    assert result.evidence["brain_tokens"] == 42  # 兜底 suggest 用量计入账目


def test_send_message_locator_failure_skips_fallback():
    """发送类任务定位失败：不兜底（fallback 零调用），按既有失败流程（可重试）。"""
    fixtures = make_deps(fallback=FakeFallback(None))

    def boom(*args, **kwargs):
        raise LocatorFailedError("候选人详情未找到「继续沟通」按钮")

    fixtures["deps"].executor.driver.send_message = boom
    with pytest.raises(Retry):  # attempt 0 → failed_retryable → 重试
        run(make_task(AtomicTaskType.SEND_MESSAGE, context={"text": "您好"}), fixtures)
    assert fixtures["fallback"].calls == []


def test_fallback_exhausted_after_retry_fails_manual():
    """兜底执行后重试仍定位失败：转人工 failed_needs_manual，不重试（防循环）。"""
    fixtures = make_deps(
        fallback=FakeFallback(
            FallbackOutcome(
                diagnosis="尝试点击修复",
                action="click_text",
                target="全部",
                confidence=0.9,
                executed=True,
            )
        )
    )

    def boom(*args, **kwargs):
        raise LocatorFailedError("始终找不到锚点")

    fixtures["deps"].executor.driver.read_online_resume = boom
    result = run(make_task(AtomicTaskType.READ_RESUME), fixtures)  # 不抛 Retry

    assert result.outcome == "failed_needs_manual"
    assert result.evidence["fallback_exhausted"] is True
    assert len(fixtures["fallback"].calls) == 1


# —— LlmFallback 模块（置信度闸 / 坐标解析 / 风控上抛）——


class FakeDriverForFallback:
    """兜底驱动替身：截图可注入异常；记录文本/坐标点击调用。"""

    def __init__(
        self, *, screenshot: bytes = b"png", screenshot_error: Exception | None = None
    ) -> None:
        self._screenshot = screenshot
        self._screenshot_error = screenshot_error
        self.text_calls: list[str] = []
        self.px_calls: list[tuple[float, float]] = []

    def capture_desktop_png(self) -> bytes:
        if self._screenshot_error is not None:
            raise self._screenshot_error
        return self._screenshot

    def click_text_contains(self, text: str) -> bool:
        self.text_calls.append(text)
        return True

    def click_at_screenshot_px(self, x: float, y: float) -> bool:
        self.px_calls.append((x, y))
        return True


def test_llm_fallback_confidence_gate_skips_action():
    """置信度低于阈值：不执行动作（executed False），诊断仍返回。"""
    driver = FakeDriverForFallback()
    brain = MockBrain(
        suggest_result=FallbackSuggestion(
            diagnosis="不太确定", action="click_text", target="全部", confidence=0.3
        )
    )
    outcome = LlmFallback(driver=driver, brain=brain).recover(
        make_task(AtomicTaskType.LIST_UNREAD), RuntimeError("x")
    )
    assert outcome is not None and outcome.executed is False
    assert driver.text_calls == []


def test_llm_fallback_click_coords_parses_target():
    """坐标动作：target "x,y" 解析成功才执行；坏值不执行。"""
    driver = FakeDriverForFallback()
    brain = MockBrain(
        suggest_result=FallbackSuggestion(
            diagnosis="点坐标", action="click_coords", target="840,1700", confidence=0.8
        )
    )
    outcome = LlmFallback(driver=driver, brain=brain).recover(
        make_task(AtomicTaskType.READ_RESUME), RuntimeError("x")
    )
    assert outcome.executed is True
    assert driver.px_calls == [(840.0, 1700.0)]

    brain_bad = MockBrain(
        suggest_result=FallbackSuggestion(
            diagnosis="坏坐标", action="click_coords", target="abc", confidence=0.8
        )
    )
    outcome_bad = LlmFallback(driver=driver, brain=brain_bad).recover(
        make_task(AtomicTaskType.READ_RESUME), RuntimeError("x")
    )
    assert outcome_bad.executed is False


def test_llm_fallback_risk_control_propagates():
    """兜底流程撞风控：RiskControlDetectedError 上抛（绝不被吞）。"""
    driver = FakeDriverForFallback(screenshot_error=RiskControlDetectedError("风控页"))
    with pytest.raises(RiskControlDetectedError):
        LlmFallback(driver=driver, brain=MockBrain()).recover(
            make_task(AtomicTaskType.LIST_UNREAD), RuntimeError("x")
        )


# —— M2：LIST_RECOMMENDED（limit 透传 + evidence）——


def test_list_recommended_passes_limit_and_returns_evidence():
    """LIST_RECOMMENDED：context.limit → 驱动逐卡上限；evidence.recommended_ids 落账。"""
    world = make_world(
        conversations=[
            ConversationScript(liepin_user_id="r1", recommended=True),
            ConversationScript(liepin_user_id="r2", recommended=True),
            ConversationScript(liepin_user_id="r3", recommended=True),
        ]
    )
    fixtures = make_deps()
    fixtures["deps"].executor.driver = FakeLiepinDriver(world)
    result = run(
        make_task(AtomicTaskType.LIST_RECOMMENDED, context={"limit": 2}), fixtures
    )
    assert result.outcome == "success"
    assert result.evidence["recommended_ids"] == ["r1", "r2"]  # 驱动侧按 limit 截断


def test_list_recommended_default_limit_when_context_missing():
    """context 无 limit → 默认 5（爬坡缺省）。"""
    world = make_world(
        conversations=[
            ConversationScript(liepin_user_id=f"r{i}", recommended=True) for i in range(7)
        ]
    )
    fixtures = make_deps()
    fixtures["deps"].executor.driver = FakeLiepinDriver(world)
    result = run(make_task(AtomicTaskType.LIST_RECOMMENDED, context={}), fixtures)
    assert result.outcome == "success"
    assert result.evidence["recommended_ids"] == [f"r{i}" for i in range(5)]
