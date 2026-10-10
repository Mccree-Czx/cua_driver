"""hr_workbuddy 共享契约包：数据模型（B 方案 2026-10-09 清理死契约后）。"""

from hr_workbuddy.models import (
    AtomicTask,
    AtomicTaskType,
    CandidateStatus,
    MinimalResume,
    ScreenRequest,
    ScreeningResult,
)

__all__ = [
    "AtomicTask",
    "AtomicTaskType",
    "MinimalResume",
    "ScreenRequest",
    "ScreeningResult",
    "CandidateStatus",
]
