"""跨服务共享 pydantic 契约模型（spec v1.6 §任务契约，逐字对齐）。"""

from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AtomicTaskType(str, Enum):
    """原子任务类型。M2 预留 list_recommended，本迭代不实现。"""

    CHECK_LOGIN = "check_login"
    LIST_UNREAD = "list_unread"
    READ_RESUME = "read_resume"
    SEND_MESSAGE = "send_message"
    CHECK_ATTACHMENT = "check_attachment"
    DOWNLOAD_ATTACHMENT = "download_attachment"


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


class TaskResult(BaseModel):
    """cua-agent 执行结果，回调 pipeline 落 TaskLog。

    evidence 字段约定：{screenshot_keys, brain_tokens, cost_est, sent_at}。
    """

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    outcome: Literal["success", "failed_retryable", "failed_needs_manual"]
    evidence: dict[str, Any]
    error: str | None


class FallbackSuggestion(BaseModel):
    """LLM 读取链兜底建议（结构化输出契约，2026-10-06 二次风控事件后新增）。

    action：click_text（target 为需点击元素上文本）/ click_coords（target 为
    "x,y" 截图像素坐标，左上原点）/ none（仅诊断不动作）。confidence 0-1。
    """

    model_config = ConfigDict(extra="forbid")

    diagnosis: str
    action: Literal["click_text", "click_coords", "none"] = "none"
    target: str = ""
    confidence: float = 0.0


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
    threshold: int = 70  # R6：jobs.llm_threshold 默认 70 的请求侧表达 （spec §4）
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
