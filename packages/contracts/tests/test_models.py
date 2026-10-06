"""契约块逐名覆盖（spec v1.6 §任务契约）。

- AtomicTask JSON 往返（model_dump_json → model_validate_json 全等）
- AtomicTaskType 恰 7 值（含 M2 list_recommended，2026-10-06 实装）
- TaskResult JSON 往返 + outcome 域（3 值，其他拒绝）
- CandidateStatus 恰 11 态（成员集合逐字一致）
- MinimalResume 恰 7 字段（缺任一字段报 ValidationError）
- ScreenRequest 嵌套 resume 往返
- ScreeningResult 往返 + status 为完整 CandidateStatus（无子集枚举）
- LiepinDriver / BrainClient 协议运行时一致性（runtime_checkable）
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from hr_workbuddy import (
    AtomicTask,
    AtomicTaskType,
    BrainClient,
    CandidateStatus,
    FallbackSuggestion,
    LiepinDriver,
    MinimalResume,
    ScreenRequest,
    ScreeningResult,
    TaskResult,
)

RESUME_FIELDS = (
    "name",
    "liepin_user_id",
    "education",
    "years_of_experience",
    "city",
    "salary",
    "experience_summary",
)

CANDIDATE_STATUSES = {
    "new",
    "screened_pass",
    "rejected_hard",
    "rejected_llm",
    "greeted",
    "resume_requested",
    "awaiting_resume",
    "resume_received",
    "no_response",
    "hr_reviewed",
    "closed",
}


def _minimal_resume() -> dict:
    return {
        "name": "张伟",
        "liepin_user_id": "LP001",
        "education": "本科",
        "years_of_experience": "5",
        "city": "深圳",
        "salary": "20-30K",
        "experience_summary": "5 年产品经理经验",
    }


def _atomic_task(**overrides) -> AtomicTask:
    kwargs = {
        "task_id": uuid4(),
        "type": AtomicTaskType.READ_RESUME,
        "job_id": 1,
        "job_candidate_id": 7,
        "candidate_liepin_id": "LP001",
        "context": {"resume_url": "https://example.com/resume/LP001"},
    }
    kwargs.update(overrides)
    return AtomicTask(**kwargs)


class TestAtomicTask:
    def test_json_roundtrip(self):
        task = _atomic_task()
        restored = AtomicTask.model_validate_json(task.model_dump_json())
        assert restored == task

    def test_defaults_attempt_and_max_attempts(self):
        task = _atomic_task()
        assert task.attempt == 0
        assert task.max_attempts == 3


def test_atomic_task_type_values_verbatim():
    assert {t.value for t in AtomicTaskType} == {
        "check_login",
        "list_unread",
        "read_resume",
        "send_message",
        "check_attachment",
        "download_attachment",
        "list_recommended",
    }


class TestTaskResult:
    def test_json_roundtrip(self):
        result = TaskResult(
            task_id=uuid4(),
            outcome="success",
            evidence={
                "screenshot_keys": ["snapshots/LP001/20261005_100000.png"],
                "brain_tokens": 0,
                "cost_est": 0.0,
                "sent_at": "2026-10-05T10:00:00",
            },
            error=None,
        )
        restored = TaskResult.model_validate_json(result.model_dump_json())
        assert restored == result

    @pytest.mark.parametrize(
        "outcome", ["success", "failed_retryable", "failed_needs_manual"]
    )
    def test_outcome_domain(self, outcome):
        result = TaskResult(task_id=uuid4(), outcome=outcome, evidence={}, error=None)
        assert result.outcome == outcome

    def test_rejects_unknown_outcome(self):
        with pytest.raises(ValidationError):
            TaskResult(task_id=uuid4(), outcome="maybe", evidence={}, error=None)


def test_candidate_status_exactly_11_states():
    assert {s.value for s in CandidateStatus} == CANDIDATE_STATUSES
    assert len(CandidateStatus) == 11


class TestMinimalResume:
    @pytest.mark.parametrize("missing", RESUME_FIELDS)
    def test_missing_field_raises(self, missing):
        kwargs = _minimal_resume()
        del kwargs[missing]
        with pytest.raises(ValidationError):
            MinimalResume(**kwargs)

    def test_exactly_7_fields(self):
        assert set(MinimalResume.model_fields) == set(RESUME_FIELDS)


class TestScreenRequest:
    def test_nested_resume_roundtrip(self):
        req = ScreenRequest(
            job_id=1,
            resume=MinimalResume(**_minimal_resume()),
            jd_text="产品经理 JD",
            hard_rules={
                "min_education": "本科",
                "min_years": 3,
                "cities": ["深圳"],
                "exclude_keywords": ["外包"],
            },
        )
        restored = ScreenRequest.model_validate_json(req.model_dump_json())
        assert restored == req
        assert isinstance(restored.resume, MinimalResume)

    def test_threshold_defaults_to_70(self):
        """R6：threshold 为 jobs.llm_threshold 默认 70 的请求侧表达（spec §4）。"""
        req = ScreenRequest(
            job_id=1,
            resume=MinimalResume(**_minimal_resume()),
            jd_text="产品经理 JD",
            hard_rules={},
        )
        assert req.threshold == 70

    def test_llm_scoring_default_true_and_override(self):
        """2026-10-06 策略：llm_scoring 默认 True（outbound 两层）；inbound 直索要传 False。"""
        base = dict(
            job_id=1,
            resume=MinimalResume(**_minimal_resume()),
            jd_text="产品经理 JD",
            hard_rules={},
        )
        assert ScreenRequest(**base).llm_scoring is True
        assert ScreenRequest(**base, llm_scoring=False).llm_scoring is False


class TestScreeningResult:
    def test_json_roundtrip(self):
        result = ScreeningResult(
            hard_pass=True,
            hard_reasons=[],
            score=82,
            judge_reason="匹配度高",
            status=CandidateStatus.SCREENED_PASS,
            degraded=False,
        )
        restored = ScreeningResult.model_validate_json(result.model_dump_json())
        assert restored == result

    @pytest.mark.parametrize("status", sorted(CANDIDATE_STATUSES))
    def test_accepts_full_candidate_status_domain(self, status):
        result = ScreeningResult(
            hard_pass=False,
            hard_reasons=[],
            score=None,
            judge_reason="",
            status=status,
            degraded=True,
        )
        assert result.status is CandidateStatus(status)

    def test_rejects_unknown_status(self):
        with pytest.raises(ValidationError):
            ScreeningResult(
                hard_pass=False,
                hard_reasons=[],
                score=None,
                judge_reason="",
                status="bogus",
                degraded=True,
            )


class _StubLiepinDriver:
    def check_login(self) -> bool:
        return True

    def list_unread_conversations(self) -> list[str]:
        return []

    def list_recommended(self) -> list[str]:
        return []

    def open_conversation(self, candidate_liepin_id: str) -> None:
        return None

    def read_online_resume(
        self, candidate_liepin_id: str
    ) -> tuple[bytes, MinimalResume]:
        return b"", MinimalResume(**_minimal_resume())

    def send_message(self, candidate_liepin_id: str, text: str) -> None:
        return None

    def check_attachment(self, candidate_liepin_id: str) -> bool:
        return False

    def download_attachment(self, candidate_liepin_id: str) -> tuple[bytes, str]:
        return b"", "简历.pdf"


class _StubBrain:
    def verify(self, screenshot: bytes, criteria: str) -> bool:
        return True

    def suggest(self, screenshot: bytes, context: str):
        return FallbackSuggestion(diagnosis="stub", action="none", confidence=0.0)


class TestProtocols:
    def test_liepin_driver_runtime_conformance(self):
        assert isinstance(_StubLiepinDriver(), LiepinDriver)
        assert not isinstance(_StubBrain(), LiepinDriver)

    def test_brain_client_runtime_conformance(self):
        assert isinstance(_StubBrain(), BrainClient)
        assert not isinstance(_StubLiepinDriver(), BrainClient)
