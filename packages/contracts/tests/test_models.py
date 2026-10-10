"""契约块逐名覆盖（B 方案 2026-10-09 清理死契约后）。

- AtomicTask JSON 往返（model_dump_json → model_validate_json 全等）
- AtomicTaskType 恰 7 值（含 M2 list_recommended，2026-10-06 实装）
- CandidateStatus 恰 11 态（成员集合逐字一致）
- MinimalResume 恰 7 字段（缺任一字段报 ValidationError）
- ScreenRequest 嵌套 resume 往返
- ScreeningResult 往返 + status 为完整 CandidateStatus（无子集枚举）
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from hr_workbuddy import (
    AtomicTask,
    AtomicTaskType,
    CandidateStatus,
    MinimalResume,
    ScreenRequest,
    ScreeningResult,
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

    def test_min_stars_default_3(self):
        """2026-10-10：min_stars 默认 3（1-5 星，≥3 星即主动沟通）。"""
        req = ScreenRequest(
            job_id=1,
            resume=MinimalResume(**_minimal_resume()),
            jd_text="产品经理 JD",
            hard_rules={},
        )
        assert req.min_stars == 3

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
