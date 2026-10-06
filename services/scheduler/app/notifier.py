"""告警通知（M1 最小）：结构化日志行。

登录失效 / 配额触顶的"落账"由 pipeline 侧承担（R9 登录态键 + R10 TaskLog
行）；本模块把事件打为日志（stdout/stderr，容器日志可采集）。webhook /
监控页告警属 M4（计划明确不做）。
"""

import logging

logger = logging.getLogger("scheduler.notifier")


class Notifier:
    """告警通道。alert(event, message) → WARNING 日志行。测试用子类记录替身。"""

    def alert(self, event: str, message: str) -> None:
        logger.warning("[%s] %s", event, message)
