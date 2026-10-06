"""screening 入口：POST /screen（硬规则 → LLM 评分 → 阈值判定）+ GET /health。

判定顺序（controller 裁定，spec v1.6 §3 步骤 3-4）：
1. hard_pass=False → rejected_hard（短路，不调 LLM）
2. LLM 评分（客户端可注入）；provider 错误 / schema 非法 → degraded（HTTP 200）
3. score >= threshold → screened_pass；否则 rejected_llm
degraded 时 status 无实际含义（pipeline 侧只存快照不推进，spec §5），
固定写 screened_pass 以保持契约合法。
"""

from fastapi import Depends, FastAPI
from openai import OpenAI, OpenAIError

from hr_workbuddy import CandidateStatus, ScreenRequest, ScreeningResult

from app.config import get_settings
from app.hard_rules import evaluate
from app.llm_score import LLMScoreError, build_client, score

app = FastAPI(title="hr-workbuddy screening")

DEGRADED_REASON = "deferred: LLM unavailable"


def _degraded() -> ScreeningResult:
    """LLM 不可用（provider 错误 / schema 非法 / 无 key）的统一降级结果。"""
    return ScreeningResult(
        hard_pass=True,
        hard_reasons=[],
        score=None,
        judge_reason=DEGRADED_REASON,
        status=CandidateStatus.SCREENED_PASS,
        degraded=True,
    )


def get_client() -> OpenAI | None:
    """评分客户端依赖（测试经 dependency_overrides 注入 MockLLM）。

    返回 None = provider 不可用（key 未提供 / 客户端构造失败），调用方降级。
    openai SDK 在 api_key 为空时构造即抛错，而 key 由用户稍后提供（brief），
    因此不能等第一次调用才兜底——服务必须无 key 也正常响应 degraded。
    """
    settings = get_settings()
    if not settings.screening_llm_api_key:
        return None
    try:
        return build_client(
            settings.screening_llm_base_url, settings.screening_llm_api_key
        )
    except OpenAIError:
        return None


@app.post("/screen", response_model=ScreeningResult)
def screen(
    request: ScreenRequest, client: OpenAI | None = Depends(get_client)
) -> ScreeningResult:
    hard_pass, hard_reasons = evaluate(request.hard_rules, request.resume)
    if not hard_pass:
        return ScreeningResult(
            hard_pass=False,
            hard_reasons=hard_reasons,
            score=None,
            judge_reason="硬规则不通过: " + "；".join(hard_reasons),
            status=CandidateStatus.REJECTED_HARD,
            degraded=False,
        )

    if client is None:
        return _degraded()

    try:
        llm_score, reason = score(
            client,
            request.jd_text,
            request.resume,
            get_settings().screening_llm_model,
        )
    except LLMScoreError:
        return _degraded()

    if llm_score >= request.threshold:
        return ScreeningResult(
            hard_pass=True,
            hard_reasons=[],
            score=llm_score,
            judge_reason=reason,
            status=CandidateStatus.SCREENED_PASS,
            degraded=False,
        )
    return ScreeningResult(
        hard_pass=True,
        hard_reasons=[],
        score=llm_score,
        judge_reason=reason,
        status=CandidateStatus.REJECTED_LLM,
        degraded=False,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
