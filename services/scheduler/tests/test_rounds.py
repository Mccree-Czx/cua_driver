"""T10 轮次测试（依赖注入，不依赖真实 APScheduler / HTTP / Redis）：
- 登录失败暂停派发【Review Focus 6】：已入队任务照常（恢复探测），新派发全停
- sweep 只打 awaiting_resume（pipeline awaiting 端点返回的行）
- 窗口外不派发
- 在途去重：同一 (type, job_candidate_id) 有在途任务不重复入队
- quota 触顶停触达（deferred 重判产出 SEND_MESSAGE → 跳过）+ 告警
"""

from datetime import datetime, time

from hr_workbuddy import AtomicTaskType

from scheduler_app.notifier import Notifier
from scheduler_app.rounds import (
    Gate,
    RoundDeps,
    awaiting_resume_sweep,
    daily_quota_reconcile,
    deferred_sweep,
    inbound_round,
    login_health_round,
    within_work_window,
)

NOW = datetime(2026, 10, 5, 10, 0)  # 周二 10:00（窗口 08:00-20:00 内）


class FakePipelineApi:
    """记录调用的 pipeline 替身：jobs / awaiting / login_state / quota 可脚本化。"""

    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.awaiting: list[dict] = []
        self.login_state: dict = {"is_login": None, "checked_at": None, "task_id": None}
        self.quota: dict = {"count": 0, "date": "2026-10-05"}
        self.posted_stale = 0
        self.posted_deferred = 0

    def list_jobs(self) -> list[dict]:
        return self.jobs

    def get_awaiting(self) -> list[dict]:
        return self.awaiting

    def get_login_state(self) -> dict:
        return self.login_state

    def get_quota_today(self) -> dict:
        return self.quota

    def post_stale_awaiting(self) -> dict:
        self.posted_stale += 1
        return {"closed": 0}

    def post_deferred(self) -> dict:
        self.posted_deferred += 1
        return {"judged": 0}


class FakeNotifier(Notifier):
    def __init__(self) -> None:
        self.alerts: list[tuple[str, str]] = []

    def alert(self, event: str, message: str) -> None:
        self.alerts.append((event, message))


class RecordingEnqueuer:
    """记录入队任务；in_flight 按注入脚本判定在途。"""

    def __init__(self, in_flight_script=None) -> None:
        self.tasks: list = []
        self._in_flight_script = in_flight_script or (lambda task_type, jc_id: False)

    def __call__(self, task) -> None:
        self.tasks.append(task)

    def in_flight(self, task_type, job_candidate_id: int) -> bool:
        return self._in_flight_script(task_type, job_candidate_id)

    def of_type(self, task_type):
        return [t for t in self.tasks if t.type is task_type]


def make_deps(pipeline=None, enqueuer=None, notifier=None, gate=None, in_flight=None, within_window=None, daily_msg_cap=240):
    return RoundDeps(
        pipeline=pipeline or FakePipelineApi(),
        enqueue=enqueuer or RecordingEnqueuer(),
        notifier=notifier or FakeNotifier(),
        gate=gate or Gate(),
        in_flight=in_flight,
        within_window=within_window,
        daily_msg_cap=daily_msg_cap,
        now=lambda: NOW,
    )


# —— 工作窗口 ——


def test_within_work_window_boundaries():
    """08:00 起含、20:00 终不含（[start, end)）。"""
    day = datetime(2026, 10, 5)
    window = (time(8, 0), time(20, 0))
    assert within_work_window(day.replace(hour=7, minute=59), *window) is False
    assert within_work_window(day.replace(hour=8, minute=0), *window) is True
    assert within_work_window(day.replace(hour=19, minute=59), *window) is True
    assert within_work_window(day.replace(hour=20, minute=0), *window) is False


def test_outside_window_no_dispatch():
    """窗口外不派发：全部轮次跳过，零入队、零 pipeline 调用。"""
    pipeline = FakePipelineApi()
    pipeline.jobs = [{"id": 1, "status": "active"}]
    pipeline.awaiting = [{"id": 11, "job_id": 1, "candidate_liepin_id": "LP-A"}]
    enqueuer = RecordingEnqueuer()
    outside = lambda now: False  # noqa: E731
    deps = make_deps(pipeline=pipeline, enqueuer=enqueuer, within_window=outside)

    rounds = [
        inbound_round(deps),
        awaiting_resume_sweep(deps),
        login_health_round(deps),
        daily_quota_reconcile(deps),
        deferred_sweep(deps),
    ]
    for report in rounds:
        assert report.skipped == "outside_work_window"
    assert enqueuer.tasks == []
    assert pipeline.posted_stale == 0
    assert pipeline.posted_deferred == 0


# —— inbound_round ——


def test_inbound_round_enqueues_list_unread_per_active_job():
    """对每个 active 岗位入队 LIST_UNREAD；非 active 岗位跳过。"""
    pipeline = FakePipelineApi()
    pipeline.jobs = [
        {"id": 1, "status": "active"},
        {"id": 2, "status": "paused"},
        {"id": 3, "status": "active"},
    ]
    enqueuer = RecordingEnqueuer()
    report = inbound_round(make_deps(pipeline=pipeline, enqueuer=enqueuer))
    assert report.dispatched == 2
    assert report.skipped is None
    tasks = enqueuer.of_type(AtomicTaskType.LIST_UNREAD)
    assert len(tasks) == 2
    assert {t.job_id for t in tasks} == {1, 3}
    assert all(t.job_candidate_id is None and t.candidate_liepin_id is None for t in tasks)


# —— awaiting_resume_sweep ——


def test_sweep_only_hits_awaiting_resume():
    """sweep 只打 pipeline awaiting 端点返回的行：72h 关闭委托 + 逐 jc 入队 CHECK_ATTACHMENT。"""
    pipeline = FakePipelineApi()
    pipeline.awaiting = [
        {"id": 11, "job_id": 1, "candidate_liepin_id": "LP-A"},
        {"id": 12, "job_id": 2, "candidate_liepin_id": "LP-B"},
    ]
    enqueuer = RecordingEnqueuer()
    report = awaiting_resume_sweep(make_deps(pipeline=pipeline, enqueuer=enqueuer))
    assert pipeline.posted_stale == 1  # 72h 关闭委托给 pipeline
    assert report.dispatched == 2
    tasks = enqueuer.of_type(AtomicTaskType.CHECK_ATTACHMENT)
    assert len(tasks) == 2
    by_id = {t.job_candidate_id: t for t in tasks}
    assert by_id[11].job_id == 1 and by_id[11].candidate_liepin_id == "LP-A"
    assert by_id[12].job_id == 2 and by_id[12].candidate_liepin_id == "LP-B"
    assert len(enqueuer.tasks) == 2  # 除 CHECK_ATTACHMENT 外无其他类型


def test_in_flight_dedup_skips_duplicate_enqueue():
    """在途去重：同一 (type, job_candidate_id) 已有在途任务 → 不重复入队。"""
    pipeline = FakePipelineApi()
    pipeline.awaiting = [
        {"id": 11, "job_id": 1, "candidate_liepin_id": "LP-A"},
        {"id": 12, "job_id": 1, "candidate_liepin_id": "LP-B"},
    ]
    enqueuer = RecordingEnqueuer(
        in_flight_script=lambda task_type, jc_id: jc_id == 11
    )
    report = awaiting_resume_sweep(
        make_deps(pipeline=pipeline, enqueuer=enqueuer, in_flight=enqueuer.in_flight)
    )
    assert report.dispatched == 1
    tasks = enqueuer.of_type(AtomicTaskType.CHECK_ATTACHMENT)
    assert [t.job_candidate_id for t in tasks] == [12]  # 11 在途被跳过


# —— login_health_round ——


def test_login_fail_pauses_dispatch():
    """【Review Focus 6】登录失效 → 暂停全部派发 + 告警「请扫码登录」；
    已暂停后其余轮次零派发；login_health 仍入队 CHECK_LOGIN（扫码恢复探测）。"""
    pipeline = FakePipelineApi()
    pipeline.jobs = [{"id": 1, "status": "active"}]
    pipeline.awaiting = [{"id": 11, "job_id": 1, "candidate_liepin_id": "LP-A"}]
    pipeline.login_state = {
        "is_login": False,
        "checked_at": "2026-10-05T10:00:00",
        "task_id": "00000000-0000-0000-0000-000000000001",
    }
    enqueuer = RecordingEnqueuer()
    notifier = FakeNotifier()
    gate = Gate()
    deps = make_deps(pipeline=pipeline, enqueuer=enqueuer, notifier=notifier, gate=gate)

    report = login_health_round(deps)
    assert gate.paused is True
    assert gate.pause_reason == "login"
    assert report.dispatched == 1  # 首轮 CHECK_LOGIN 已入队

    # 告警恰一条「请扫码登录」
    assert len(notifier.alerts) == 1
    event, message = notifier.alerts[0]
    assert event == "login"
    assert "请扫码登录" in message

    # 暂停后：其余轮次零派发
    assert inbound_round(deps).skipped == "paused:login"
    assert awaiting_resume_sweep(deps).skipped == "paused:login"
    assert deferred_sweep(deps).skipped == "paused:login"
    assert pipeline.posted_stale == 0
    assert pipeline.posted_deferred == 0
    assert enqueuer.of_type(AtomicTaskType.LIST_UNREAD) == []
    assert enqueuer.of_type(AtomicTaskType.CHECK_ATTACHMENT) == []

    # 恢复探测：login_health 仍入队 CHECK_LOGIN，且不重复告警
    report2 = login_health_round(deps)
    assert len(enqueuer.of_type(AtomicTaskType.CHECK_LOGIN)) == 2
    assert len(notifier.alerts) == 1  # 已暂停 → 不重复告警


def test_login_ok_resumes_dispatch():
    """登录恢复 → 解除暂停；未暂停时无告警。"""
    pipeline = FakePipelineApi()
    pipeline.login_state = {
        "is_login": True,
        "checked_at": "2026-10-05T10:00:00",
        "task_id": "00000000-0000-0000-0000-000000000002",
    }
    gate = Gate(paused=True, pause_reason="login")
    notifier = FakeNotifier()
    login_health_round(make_deps(pipeline=pipeline, notifier=notifier, gate=gate))
    assert gate.paused is False
    assert gate.pause_reason is None
    assert notifier.alerts == []


def test_login_unknown_does_not_pause():
    """从未检查过（is_login=None）→ 不动暂停标志、不告警。"""
    pipeline = FakePipelineApi()
    gate = Gate()
    notifier = FakeNotifier()
    login_health_round(make_deps(pipeline=pipeline, notifier=notifier, gate=gate))
    assert gate.paused is False
    assert notifier.alerts == []


def test_check_login_task_shape():
    """CHECK_LOGIN 任务形状：job_id=0 哨兵、无 jc/候选人。"""
    enqueuer = RecordingEnqueuer()
    login_health_round(make_deps(enqueuer=enqueuer))
    (task,) = enqueuer.tasks
    assert task.type is AtomicTaskType.CHECK_LOGIN
    assert task.job_id == 0
    assert task.job_candidate_id is None
    assert task.candidate_liepin_id is None
    assert task.context == {}


# —— daily_quota_reconcile / deferred_sweep ——


def test_quota_top_stops_deferred_and_alerts():
    """quota 触顶 → 停触达（deferred 重判产出 SEND_MESSAGE → 跳过）+ 告警；回落恢复。"""
    pipeline = FakePipelineApi()
    pipeline.quota = {"count": 240, "date": "2026-10-05"}
    notifier = FakeNotifier()
    gate = Gate()
    deps = make_deps(pipeline=pipeline, notifier=notifier, gate=gate)

    daily_quota_reconcile(deps)
    assert gate.quota_exhausted is True
    assert len(notifier.alerts) == 1
    event, message = notifier.alerts[0]
    assert event == "quota"
    assert "触达" in message

    # 触顶后 deferred_sweep 跳过（不产出新的 SEND_MESSAGE）
    assert deferred_sweep(deps).skipped == "quota_exhausted"
    assert pipeline.posted_deferred == 0
    # 重复对账不重复告警
    daily_quota_reconcile(deps)
    assert len(notifier.alerts) == 1

    # 配额回落 → 解除，deferred 恢复触发
    pipeline.quota = {"count": 100, "date": "2026-10-05"}
    daily_quota_reconcile(deps)
    assert gate.quota_exhausted is False
    assert deferred_sweep(deps).skipped is None
    assert pipeline.posted_deferred == 1


def test_quota_top_skips_attachment_enqueues_but_keeps_72h_close():
    """配额触顶 → sweep 不再入队 CHECK_ATTACHMENT（scheduler 触发的附件链源头），
    72h 关闭零触达、照常执行。"""
    pipeline = FakePipelineApi()
    pipeline.awaiting = [{"id": 11, "job_id": 1, "candidate_liepin_id": "LP-A"}]
    pipeline.quota = {"count": 240, "date": "2026-10-05"}
    enqueuer = RecordingEnqueuer()
    gate = Gate()
    deps = make_deps(pipeline=pipeline, enqueuer=enqueuer, gate=gate)

    daily_quota_reconcile(deps)
    assert gate.quota_exhausted is True

    report = awaiting_resume_sweep(deps)
    assert report.skipped == "quota_exhausted"
    assert enqueuer.of_type(AtomicTaskType.CHECK_ATTACHMENT) == []
    assert pipeline.posted_stale == 1  # 72h 关闭零触达，不受配额门约束


def test_scheduler_jobs_run_immediately_at_startup():
    """启动即首轮（评审 Important 修复）：五个 job 均设 next_run_time=now，
    窗口中途启动时首次 CHECK_LOGIN / 配额对账不延迟一个 interval。"""
    from datetime import datetime, timedelta

    from scheduler_app.config import Settings
    from scheduler_app.main import build_scheduler

    scheduler = build_scheduler(Settings())
    now = datetime.now().astimezone()
    for job_id in (
        "inbound_round",
        "awaiting_resume_sweep",
        "login_health_round",
        "daily_quota_reconcile",
        "deferred_sweep",
    ):
        job = scheduler.get_job(job_id)
        assert job is not None, job_id
        assert job.next_run_time is not None, job_id
        assert abs(job.next_run_time - now) <= timedelta(seconds=60), job_id


def test_default_deps_now_is_callable_and_rounds_run():
    """R11 回归：RoundDeps 默认 now 曾因 default_factory=datetime.now 产出 datetime
    对象（不可调用），生产 build_deps 首轮直接 TypeError。默认构造必须可调用，
    且默认 now 下轮次可正常执行。"""
    from datetime import datetime

    deps = RoundDeps(
        pipeline=FakePipelineApi(),
        enqueue=RecordingEnqueuer(),
        notifier=FakeNotifier(),
        gate=Gate(),
        within_window=lambda now: True,  # 强制轮次路径调用 deps.now()
    )
    assert callable(deps.now)
    assert isinstance(deps.now(), datetime)
    assert inbound_round(deps).dispatched == 0  # 默认 now 下轮次不炸（jobs 为空）


def test_reconcile_below_cap_no_alert():
    gate = Gate()
    notifier = FakeNotifier()
    daily_quota_reconcile(make_deps(notifier=notifier, gate=gate))
    assert gate.quota_exhausted is False
    assert notifier.alerts == []


def test_deferred_sweep_posts_when_clear():
    """正常状态：deferred_sweep 委托 pipeline 延期重判。"""
    pipeline = FakePipelineApi()
    report = deferred_sweep(make_deps(pipeline=pipeline))
    assert report.skipped is None
    assert pipeline.posted_deferred == 1
