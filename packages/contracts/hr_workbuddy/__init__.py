"""hr_workbuddy 共享契约包：数据模型 + 驱动/大脑协议（spec v1.6 §任务契约）。"""

from hr_workbuddy.models import (
    AtomicTask,
    AtomicTaskType,
    CandidateStatus,
    FallbackSuggestion,
    MinimalResume,
    ScreenRequest,
    ScreeningResult,
    TaskResult,
)
from hr_workbuddy.protocols import BrainClient, LiepinDriver

__all__ = [
    "AtomicTask",
    "AtomicTaskType",
    "TaskResult",
    "MinimalResume",
    "ScreenRequest",
    "ScreeningResult",
    "CandidateStatus",
    "FallbackSuggestion",
    "LiepinDriver",
    "BrainClient",
]
