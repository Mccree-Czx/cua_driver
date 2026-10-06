"""测试用内存替身：FakeTaskQueue（登记派发 + 记录入队）、FakeScreening。

生产对应物：app/task_queue.ArqTaskQueue（Redis 登记 + arq 入队）、
app/screening_client.ScreeningClient（真实 HTTP 调 screening 服务）。
结果回调路由依赖 task_id → AtomicTask 登记（D1：回调体不含任务类型），
FakeTaskQueue 用内存 dict 实现同一形状；FakeScreening 按 liepin_user_id
脚本化响应，未预设的候选人返回 degraded——防御跨测试残留数据被
延期重判扫描误伤。
"""

from uuid import UUID

from hr_workbuddy import AtomicTask, CandidateStatus, ScreenRequest, ScreeningResult

DEGRADED_RESULT = ScreeningResult(
    hard_pass=True,
    hard_reasons=[],
    score=None,
    judge_reason="deferred: LLM unavailable",
    status=CandidateStatus.SCREENED_PASS,
    degraded=True,
)


class FakeTaskQueue:
    def __init__(self) -> None:
        self.enqueued: list[AtomicTask] = []
        self._dispatched: dict[UUID, AtomicTask] = {}

    def enqueue(self, task: AtomicTask) -> None:
        """编排器入队：登记 + 记录（生产同时写 Redis 与 arq）。"""
        self._dispatched[task.task_id] = task
        self.enqueued.append(task)

    def record(self, task: AtomicTask) -> None:
        """登记外部（scheduler 等）派发的任务，模拟生产 Redis 登记表已有条目。"""
        self._dispatched[task.task_id] = task

    def get_dispatched(self, task_id: UUID) -> AtomicTask | None:
        return self._dispatched.get(task_id)


class FakeScreening:
    """按 liepin_user_id 返回预设 ScreeningResult；未预设 → degraded。

    2026-10-06 策略镜像：请求 llm_scoring=False（inbound 直索要）时，非硬拒
    一律返回「硬规则通过（未评分）」——与 screening 服务语义逐字一致；
    硬拒预设与 llm_scoring=True 的响应原样返回。
    """

    def __init__(self) -> None:
        self.responses: dict[str, ScreeningResult] = {}
        self.requests: list[ScreenRequest] = []

    def set(self, liepin_user_id: str, result: ScreeningResult) -> None:
        self.responses[liepin_user_id] = result

    def screen(self, request: ScreenRequest) -> ScreeningResult:
        self.requests.append(request)
        result = self.responses.get(request.resume.liepin_user_id, DEGRADED_RESULT)
        if (
            not request.llm_scoring
            and result.status is not CandidateStatus.REJECTED_HARD
        ):
            return ScreeningResult(
                hard_pass=True,
                hard_reasons=[],
                score=None,
                judge_reason="硬规则通过（评分后移至简历收到后）",
                status=CandidateStatus.SCREENED_PASS,
                degraded=False,
            )
        return result
