"""cost：token 用量 → TaskResult.evidence 账目（brain_tokens / cost_est）。

覆盖：估价数学、账目累加、OpenAIBrain 用量提取（含无 usage 字段的旧
响应兼容）、MockBrain 剧本判定 + 用量、执行器成功 evidence 含账目。
pipeline 侧通用 TaskLog 落账由 T10 补（R10）——本服务只保证 evidence 有。
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from app.brain.mock import MockBrain
from app.brain.openai_brain import OpenAIBrain
from app.brain.usage import BrainUsage
from app.cost import accumulate, estimate_cost, usage_evidence
from app.drivers.fake import FakeLiepinDriver
from app.executor import ExecutorDeps, execute
from app.world import ConversationScript, World
from hr_workbuddy import AtomicTask, AtomicTaskType, MinimalResume

MODEL = "deepseek-flash"
PNG = b"\x89PNG\r\n\x1a\nfake-screenshot"
PRICE = 2.0  # 每 1k tokens 单价 2 元（占位，T12 按视觉模型定价校准）


def make_usage(*, prompt: int = 120, completion: int = 30, total: int = 150) -> BrainUsage:
    return BrainUsage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)


# —— 估价与账目累加 ——


def test_estimate_cost_math():
    assert estimate_cost(make_usage(total=500), PRICE) == 1.0
    assert estimate_cost(make_usage(total=0), PRICE) == 0.0


def test_accumulate_puts_brain_tokens_and_cost_est():
    evidence: dict = {}
    accumulate(evidence, make_usage(total=150), PRICE)
    assert evidence["brain_tokens"] == 150
    assert evidence["cost_est"] == 0.3


def test_accumulate_sums_across_calls():
    evidence: dict = {"screenshot_keys": ["k1"]}
    accumulate(evidence, make_usage(total=150), PRICE)
    accumulate(evidence, make_usage(total=50), PRICE)
    assert evidence["brain_tokens"] == 200
    assert evidence["cost_est"] == 0.4
    assert evidence["screenshot_keys"] == ["k1"]  # 既有字段不动


def test_usage_evidence_returns_fresh_account():
    ev = usage_evidence(make_usage(total=100), PRICE)
    assert ev == {"brain_tokens": 100, "cost_est": 0.2}


# —— OpenAIBrain：verify_with_usage 提取供应商 usage ——


class FakeChat:
    """chat.completions.create 替身：返回预设 content + usage。"""

    def __init__(self, content: str, usage=None) -> None:
        self.content = content
        self.usage = usage

    def create(self, **kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=self.usage,
        )


class FakeClient:
    def __init__(self, chat: FakeChat) -> None:
        self.chat = SimpleNamespace(completions=chat)


def make_brain(chat: FakeChat) -> OpenAIBrain:
    return OpenAIBrain(
        base_url="http://fake-brain",
        api_key="fake-key",
        model=MODEL,
        client_factory=lambda: FakeClient(chat),
    )


def test_openai_brain_extracts_usage():
    usage = SimpleNamespace(prompt_tokens=120, completion_tokens=30, total_tokens=150)
    brain = make_brain(FakeChat(content='{"ok": true, "reason": "yes"}', usage=usage))
    verdict = brain.verify_with_usage(PNG, "criteria")
    assert verdict.ok is True
    assert verdict.usage == make_usage()
    assert brain.last_usage == make_usage()


def test_openai_brain_verify_without_usage_field_still_verdicts():
    """旧响应无 usage 字段（T8 测试同款替身）：账目取零，判定不受影响。"""
    brain = make_brain(FakeChat(content='{"ok": false, "reason": "no"}'))
    verdict = brain.verify_with_usage(PNG, "criteria")
    assert verdict.ok is False
    assert verdict.usage == BrainUsage()
    assert brain.verify(PNG, "criteria") is False  # 协议方法仍只回 bool


# —— MockBrain：剧本判定 + 注入用量 ——


def test_mock_brain_verify_with_usage():
    usage = make_usage(total=42)
    brain = MockBrain(script=lambda screenshot, criteria: criteria == "A", usage=usage)
    verdict = brain.verify_with_usage(b"png", "A")
    assert verdict.ok is True and verdict.usage == usage
    assert brain.verify_with_usage(b"png", "B").ok is False


# —— 执行器成功 evidence 含账目 ——


def test_executor_success_evidence_has_cost_accounts():
    """READ_RESUME 成功：evidence 含 brain_tokens / cost_est，与注入用量一致。"""
    world = World(
        login_state=True,
        conversations=[
            ConversationScript(liepin_user_id="uid_a", unread=True, resume_fixture="fixture_a")
        ],
        resume_fixtures={
            "fixture_a": MinimalResume(
                name="候选人uid_a",
                liepin_user_id="uid_a",
                education="本科",
                years_of_experience="3年",
                city="杭州",
                salary="20-30K",
                experience_summary="3 年后端",
            )
        },
    )
    task = AtomicTask(
        task_id=uuid4(),
        type=AtomicTaskType.READ_RESUME,
        job_id=1,
        job_candidate_id=1,
        candidate_liepin_id="uid_a",
        context={},
    )
    deps = ExecutorDeps(
        driver=FakeLiepinDriver(world),
        brain=MockBrain(usage=make_usage(total=150)),
        capture=lambda: PNG,
        upload_artifact=lambda task_id, kind, filename, data: "snapshots/x.png",
        price_per_1k_tokens=PRICE,
        now=lambda: datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc),
    )
    result = execute(task, deps)
    assert result.outcome == "success"
    assert result.evidence["brain_tokens"] == 150
    assert result.evidence["cost_est"] == 0.3
    assert len(result.evidence["resume"]) == 7
