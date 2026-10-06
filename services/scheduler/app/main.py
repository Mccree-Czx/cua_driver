"""scheduler 入口：APScheduler 装配 interval 轮次（测试不依赖真实调度，直调 round 函数）。

轮次间隔（spec 默认，env 可覆盖）：inbound 5min、sweep 10min、login_health
每小时（工作窗口内首轮即生效）、reconcile 每小时、deferred 30min。
窗口判断在各轮次内（rounds.within_work_window），窗口外调度照常触发但零派发。

启动即首轮（评审 Important）：五个 job 均设 next_run_time=now——服务在窗口
中途启动时，首次 CHECK_LOGIN / 配额对账不再延迟一个 interval（否则过期
login/配额状态会压制派发最长 1h）。
"""

import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import Settings, get_settings
from .notifier import Notifier
from .pipeline_client import HttpPipeline
from .rounds import (
    Gate,
    RoundDeps,
    awaiting_resume_sweep,
    build_enqueuer,
    build_in_flight,
    daily_quota_reconcile,
    deferred_sweep,
    inbound_round,
    login_health_round,
    parse_work_window,
    window_checker,
)


def build_deps(settings: Settings) -> RoundDeps:
    """生产依赖组装（单进程单份；Gate 为调度器内存标志）。"""
    start, end = parse_work_window(settings.work_window_start, settings.work_window_end)
    return RoundDeps(
        pipeline=HttpPipeline(settings.pipeline_url),
        enqueue=build_enqueuer(settings.redis_url),
        in_flight=build_in_flight(settings.redis_url),
        notifier=Notifier(),
        gate=Gate(),
        within_window=window_checker(start, end),
        daily_msg_cap=settings.daily_msg_cap,
    )


def build_scheduler(settings: Settings) -> BlockingScheduler:
    deps = build_deps(settings)
    scheduler = BlockingScheduler()
    first_run = datetime.now()  # 启动即首轮（评审 Important：CHECK_LOGIN 不延迟 1h）
    jobs = [
        (
            "inbound_round",
            inbound_round,
            settings.inbound_interval_seconds,
        ),
        (
            "awaiting_resume_sweep",
            awaiting_resume_sweep,
            settings.sweep_interval_seconds,
        ),
        (
            "login_health_round",
            login_health_round,
            settings.login_health_interval_seconds,
        ),
        (
            "daily_quota_reconcile",
            daily_quota_reconcile,
            settings.reconcile_interval_seconds,
        ),
        (
            "deferred_sweep",
            deferred_sweep,
            settings.deferred_interval_seconds,
        ),
    ]
    for job_id, round_fn, interval_seconds in jobs:
        scheduler.add_job(
            lambda fn=round_fn: fn(deps),
            IntervalTrigger(seconds=interval_seconds),
            id=job_id,
            next_run_time=first_run,
        )
    return scheduler


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    build_scheduler(get_settings()).start()


if __name__ == "__main__":
    main()
