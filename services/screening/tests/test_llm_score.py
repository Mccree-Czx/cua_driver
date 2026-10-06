"""POST /screen 判定顺序全覆盖（T5 RED-first），LLM 客户端经依赖注入替换。

四种判定结果：
- hard_pass=False → rejected_hard（短路，不调 LLM）
- score >= threshold → screened_pass（judge_reason = LLM reason）
- score < threshold → rejected_llm
- provider 错误 / 输出 schema 非法 → degraded，HTTP 200 不抛到 API 层（Review Focus 2）

全部用例注入 MockLLM，零真实网络调用；threshold 走 ScreenRequest（R6，默认 70）。
"""

import json

import openai
import pytest
from fastapi.testclient import TestClient

from hr_workbuddy import CandidateStatus, MinimalResume, ScreenRequest, ScreeningResult

from app.main import app, get_client


def _resume(**overrides) -> MinimalResume:
    kwargs = {
        "name": "张伟",
        "liepin_user_id": "LP001",
        "education": "本科",
        "years_of_experience": "5",
        "city": "深圳",
        "salary": "20-30K",
        "experience_summary": "5 年产品经理经验，主导过企业级项目",
    }
    kwargs.update(overrides)
    return MinimalResume(**kwargs)


def _request(**overrides) -> ScreenRequest:
    kwargs = {
        "job_id": 1,
        "resume": _resume(),
        "jd_text": "高级产品经理 JD：负责企业级产品规划与落地",
        "hard_rules": {
            "min_education": "本科",
            "min_years": 3,
            "cities": ["深圳"],
            "exclude_keywords": ["外包"],
        },
    }
    kwargs.update(overrides)
    return ScreenRequest(**kwargs)


class MockLLM:
    """可编程 OpenAI 兼容客户端替身。

    payload: chat.completions.create 返回的 JSON 对象（自动 json.dumps）；
    raw:     直接作为 message.content 返回（测非 JSON 输出）；
    exc:     调用时抛出的异常（测 provider 故障）；
    empty_choices: 返回空 choices 列表（测供应商畸形响应，Review Fix T5(1)）。
    """

    def __init__(
        self,
        payload: dict | None = None,
        *,
        raw: str | None = None,
        exc: Exception | None = None,
        empty_choices: bool = False,
    ):
        self._payload = payload
        self._raw = raw
        self._exc = exc
        self._empty_choices = empty_choices
        self.calls = 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        if self._empty_choices:
            return _Response(choices=[])
        content = self._raw if self._raw is not None else json.dumps(self._payload)
        return _Response(content)


class _Response:
    def __init__(self, content: str | None = None, *, choices: list | None = None):
        self.choices = choices if choices is not None else [_Choice(content)]


class _Choice:
    def __init__(self, content: str):
        self.message = _Message(content)


class _Message:
    def __init__(self, content: str):
        self.content = content


@pytest.fixture
def post():
    """注入 mock 客户端，POST /screen 一次并返回 httpx.Response。"""

    def _post(mock: MockLLM, payload: dict):
        app.dependency_overrides[get_client] = lambda: mock
        try:
            with TestClient(app) as client:
                return client.post("/screen", json=payload)
        finally:
            app.dependency_overrides.clear()

    return _post


class TestRejectedHard:
    def test_hard_fail_short_circuits_without_llm(self, post):
        # 若误调 LLM，exc 会让请求 500 —— 同时用 calls==0 显式断言短路
        mock = MockLLM(exc=AssertionError("LLM 不应被调用"))
        resp = post(
            mock, _request(resume=_resume(education="大专")).model_dump(mode="json")
        )
        assert resp.status_code == 200
        result = ScreeningResult.model_validate(resp.json())
        assert result.hard_pass is False
        assert result.hard_reasons
        assert result.score is None
        assert result.judge_reason.startswith("硬规则不通过")
        assert result.status is CandidateStatus.REJECTED_HARD
        assert result.degraded is False
        assert mock.calls == 0


class TestScreenedPass:
    def test_score_82_above_threshold_passes(self, post):
        mock = MockLLM({"score": 82, "reason": "匹配度高"})
        resp = post(mock, _request().model_dump(mode="json"))
        assert resp.status_code == 200
        result = ScreeningResult.model_validate(resp.json())
        assert result.hard_pass is True
        assert result.hard_reasons == []
        assert result.score == 82
        assert result.judge_reason == "匹配度高"
        assert result.status is CandidateStatus.SCREENED_PASS
        assert result.degraded is False
        assert mock.calls == 1

    def test_score_equal_to_default_threshold_passes(self, post):
        # 阈值边界：>= 通过（默认 threshold 70，R6）
        mock = MockLLM({"score": 70, "reason": "达标"})
        resp = post(mock, _request().model_dump(mode="json"))
        assert resp.status_code == 200
        assert resp.json()["status"] == "screened_pass"

    def test_custom_threshold_from_request_is_used(self, post):
        # R6：阈值由请求携带，screening 不维护自身阈值配置
        mock = MockLLM({"score": 40, "reason": "一般"})
        resp = post(mock, _request(threshold=30).model_dump(mode="json"))
        assert resp.status_code == 200
        assert resp.json()["status"] == "screened_pass"


class TestRejectedLLM:
    def test_score_below_threshold(self, post):
        mock = MockLLM({"score": 40, "reason": "经验不匹配"})
        resp = post(mock, _request().model_dump(mode="json"))
        assert resp.status_code == 200
        result = ScreeningResult.model_validate(resp.json())
        assert result.hard_pass is True
        assert result.score == 40
        assert result.judge_reason == "经验不匹配"
        assert result.status is CandidateStatus.REJECTED_LLM
        assert result.degraded is False


class TestDegraded:
    """Review Focus 2：schema 非法与 provider 失败都进 degraded，HTTP 200。"""

    def test_score_out_of_range_degrades(self, post):
        mock = MockLLM({"score": 150, "reason": "离谱"})
        resp = post(mock, _request().model_dump(mode="json"))
        assert resp.status_code == 200
        result = ScreeningResult.model_validate(resp.json())
        assert result.hard_pass is True
        assert result.hard_reasons == []
        assert result.score is None
        assert result.judge_reason == "deferred: LLM unavailable"
        assert result.degraded is True

    @pytest.mark.parametrize(
        "payload",
        [
            {"score": "82", "reason": "字符串分数"},  # 非整数
            {"score": 82.5, "reason": "小数分数"},
            {"reason": "缺 score 字段"},
            {"score": 82},  # 缺 reason 字段
            {"score": 82, "reason": "多字段", "extra": 1},
        ],
    )
    def test_illegal_output_degrades(self, post, payload):
        mock = MockLLM(payload)
        resp = post(mock, _request().model_dump(mode="json"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["degraded"] is True
        assert body["score"] is None
        assert body["status"] == "screened_pass"

    def test_non_json_content_degrades(self, post):
        mock = MockLLM(raw="这不是 JSON")
        resp = post(mock, _request().model_dump(mode="json"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["degraded"] is True
        assert body["score"] is None

    def test_provider_error_degrades(self, post):
        mock = MockLLM(exc=openai.OpenAIError("provider down"))
        resp = post(mock, _request().model_dump(mode="json"))
        assert resp.status_code == 200
        result = ScreeningResult.model_validate(resp.json())
        assert result.hard_pass is True
        assert result.score is None
        assert result.judge_reason == "deferred: LLM unavailable"
        assert result.status is CandidateStatus.SCREENED_PASS
        assert result.degraded is True

    def test_empty_choices_degrades(self, post):
        # Review Fix T5(1)：HTTP 200 + 空 choices 的畸形响应不得以 IndexError 逃出
        # 降级路径（500），必须 200 + degraded
        mock = MockLLM(empty_choices=True)
        resp = post(mock, _request().model_dump(mode="json"))
        assert resp.status_code == 200
        result = ScreeningResult.model_validate(resp.json())
        assert result.hard_pass is True
        assert result.score is None
        assert result.judge_reason == "deferred: LLM unavailable"
        assert result.degraded is True
        assert mock.calls == 1

    def test_unavailable_client_degrades(self, post):
        # key 由用户稍后提供（brief）：无客户端可用也必须 HTTP 200 降级，不得 500
        resp = post(None, _request().model_dump(mode="json"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["hard_pass"] is True
        assert body["score"] is None
        assert body["judge_reason"] == "deferred: LLM unavailable"
        assert body["degraded"] is True

    def test_get_client_returns_none_without_api_key(self, monkeypatch):
        # openai SDK 在 api_key 为空时构造即抛错——get_client 必须兜住（smoke test 实证）
        from app.config import get_settings

        monkeypatch.setenv("SCREENING_LLM_API_KEY", "")
        get_settings.cache_clear()
        try:
            assert get_client() is None
        finally:
            get_settings.cache_clear()

    def test_get_client_construction_failure_returns_none(self, monkeypatch):
        # 有 key 但客户端构造仍失败（如 SDK 校验）→ 同样降级为 None，不得 500
        from app.config import get_settings

        def _boom(*args, **kwargs):
            raise openai.OpenAIError("construct boom")

        monkeypatch.setenv("SCREENING_LLM_API_KEY", "test-key")
        monkeypatch.setattr("app.main.build_client", _boom)
        get_settings.cache_clear()
        try:
            assert get_client() is None
        finally:
            get_settings.cache_clear()


class TestHealth:
    def test_health(self):
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestConfigDefaults:
    def test_default_model_is_deepseek_flash(self):
        # controller 更正：真实账号 /v1/models 实测仅有 deepseek-flash /
        # deepseek-v4-pro，deepseek-chat 不存在——默认值须锁定为实测存在的模型
        from app.config import Settings

        assert Settings(_env_file=None).screening_llm_model == "deepseek-flash"
