"""跨服务共享 pydantic 契约模型（spec v1.6 §任务契约，逐字对齐）。"""

from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AtomicTaskType(str, Enum):
    """原子任务类型（恰 7 值）。list_recommended = M2 推荐人列表读取（2026-10-06 实装）。"""

    CHECK_LOGIN = "check_login"
    LIST_UNREAD = "list_unread"
    READ_RESUME = "read_resume"
    SEND_MESSAGE = "send_message"
    CHECK_ATTACHMENT = "check_attachment"
    DOWNLOAD_ATTACHMENT = "download_attachment"
    LIST_RECOMMENDED = "list_recommended"


class CandidateStatus(str, Enum):
    """候选人状态（恰 11 态，spec §4 状态机）。"""

    NEW = "new"
    SCREENED_PASS = "screened_pass"
    REJECTED_HARD = "rejected_hard"
    REJECTED_LLM = "rejected_llm"
    GREETED = "greeted"
    RESUME_REQUESTED = "resume_requested"
    AWAITING_RESUME = "awaiting_resume"
    RESUME_RECEIVED = "resume_received"
    NO_RESPONSE = "no_response"
    HR_REVIEWED = "hr_reviewed"
    CLOSED = "closed"


class AtomicTask(BaseModel):
    """scheduler → arq（Redis）的原子任务，JSON 入队。"""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    type: AtomicTaskType
    job_id: int
    job_candidate_id: int | None
    candidate_liepin_id: str | None
    context: dict[str, Any]
    attempt: int = 0
    max_attempts: int = 3


class MinimalResume(BaseModel):
    """在线简历最小字段集（恰 7 字段），落 job_candidate.online_resume_minimal。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    liepin_user_id: str
    education: str
    years_of_experience: str
    city: str
    salary: str
    experience_summary: str


class ScreenRequest(BaseModel):
    """pipeline → screening POST /screen 请求体。"""

    model_config = ConfigDict(extra="forbid")

    job_id: int
    resume: MinimalResume
    jd_text: str
    hard_rules: dict[str, Any]
    min_stars: int = 3  # 1-5 星最低主动沟通星级（3=基本符合）
    scoring_prefs: dict[str, Any] = {}  # 评分卡：boosters/veto/requirements
    llm_scoring: bool = True  # 2026-10-06 策略：inbound 直索要 → False（仅硬规则，
    # LLM 评分后移至简历收到后）；outbound（推荐人）保持两层判定 → True


class ScreeningResult(BaseModel):
    """screening 响应体。status 取完整 CandidateStatus（实际只出现
    screened_pass | rejected_hard | rejected_llm，不做子集枚举）。"""

    model_config = ConfigDict(extra="forbid")

    hard_pass: bool
    hard_reasons: list[str]
    score: int | None
    judge_reason: str
    status: CandidateStatus
    degraded: bool
