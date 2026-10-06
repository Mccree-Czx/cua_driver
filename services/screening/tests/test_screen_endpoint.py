"""POST /screen 端点：llm_scoring 策略（2026-10-06 inbound 直索要——仅硬规则）。

- llm_scoring=False：硬规则通过即返回 screened_pass（score=None，评分后移）——
  不依赖 LLM client；硬拒仍然短路。
- llm_scoring=True（默认，outbound 两层）：无 client → degraded（回归）。
LLM 客户端一律经 dependency_overrides 注入 None/Mock——测试零网络调用。
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_client

client = TestClient(app)

RESUME = {
    "name": "张伟",
    "liepin_user_id": "LP1",
    "education": "本科",
    "years_of_experience": "3年",
    "city": "北京",
    "salary": "20-30K",
    "experience_summary": "三年互联网产品经验",
}
HARD_RULES = {"min_education": "本科", "min_years": 3}


@pytest.fixture(autouse=True)
def _no_llm_client():
    """全部用例注入 None client：llm_scoring=False 不应触达它；=True 走 degraded。"""
    app.dependency_overrides[get_client] = lambda: None
    yield
    app.dependency_overrides.clear()


def _post(**overrides):
    payload = dict(
        job_id=1, resume=RESUME, jd_text="产品经理 JD", hard_rules=HARD_RULES, threshold=70
    )
    payload.update(overrides)
    return client.post("/screen", json=payload)


def test_llm_scoring_false_hard_pass_returns_unscored_pass():
    """仅硬规则模式：硬规则通过 → screened_pass（未评分），不使用 LLM client。"""
    resp = _post(llm_scoring=False)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "screened_pass"
    assert body["hard_pass"] is True
    assert body["score"] is None
    assert "评分后移至简历收到后" in body["judge_reason"]
    assert body["degraded"] is False


def test_llm_scoring_false_hard_reject_still_short_circuits():
    """仅硬规则模式：硬拒仍短路（rejected_hard，不受 llm_scoring 影响）。"""
    resp = _post(llm_scoring=False, hard_rules={"min_years": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "rejected_hard"
    assert body["score"] is None
    assert body["degraded"] is False


def test_llm_scoring_true_without_client_degrades():
    """默认两层模式（outbound）：无 LLM client → degraded（回归保持）。"""
    resp = _post(llm_scoring=True)
    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] is True
    assert body["judge_reason"] == "deferred: LLM unavailable"
