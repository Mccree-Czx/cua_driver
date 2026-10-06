"""假 screening 服务（E2E 专用）：按 liepin_user_id 返回剧本化 ScreeningResult。

controller 裁定：不真调 LLM、不做真实硬规则判定、不改 T5 生产代码——
本服务对每个候选人的判定结果由剧本映射硬编码：
- LP001 → 82 分通过（剧本 A：直索要 → 收到后评分）
- LP002 → 55 分（剧本 B：收到后评分 55 落账）
- LP003 → 硬规则不通过（剧本 B：rejected_hard 零触达）
- LP004 → 82 分（剧本 B：永不回复 → 72h 关闭）

2026-10-06 策略镜像：llm_scoring=False（inbound 直索要）→ 非硬拒一律返回
「硬规则通过（未评分）」（与 screening 服务语义逐字一致）；收到简历后的
补评分调用 llm_scoring=True → 返回剧本分数。

由 scripts/run_m1_e2e.py 以 uvicorn 子进程启动（127.0.0.1:8001）；
pipeline 经 SCREENING_URL 指向本服务。
"""

from fastapi import FastAPI, HTTPException

from hr_workbuddy import CandidateStatus, ScreenRequest, ScreeningResult

app = FastAPI(title="hr-workbuddy fake screening (e2e)")


def _result(
    *,
    hard_pass: bool,
    score: int | None,
    judge_reason: str,
    status: CandidateStatus,
    hard_reasons: list[str] | None = None,
) -> ScreeningResult:
    return ScreeningResult(
        hard_pass=hard_pass,
        hard_reasons=hard_reasons or [],
        score=score,
        judge_reason=judge_reason,
        status=status,
        degraded=False,
    )


# 剧本映射（binding：LP001→82 通过、LP002→55 拒绝、LP003→rejected_hard）
SCRIPTS: dict[str, ScreeningResult] = {
    "LP001": _result(
        hard_pass=True,
        score=82,
        judge_reason="LLM 评分 82 通过（剧本 A）",
        status=CandidateStatus.SCREENED_PASS,
    ),
    "LP002": _result(
        hard_pass=True,
        score=55,
        judge_reason="LLM 评分 55 未达阈值（剧本 B）",
        status=CandidateStatus.REJECTED_LLM,
    ),
    "LP003": _result(
        hard_pass=False,
        score=None,
        judge_reason="硬规则不通过: 学历不满足岗位要求（剧本 B）",
        status=CandidateStatus.REJECTED_HARD,
        hard_reasons=["学历不满足岗位要求"],
    ),
    "LP004": _result(
        hard_pass=True,
        score=82,
        judge_reason="LLM 评分 82 通过（剧本 B 永不回复）",
        status=CandidateStatus.SCREENED_PASS,
    ),
}


@app.post("/screen", response_model=ScreeningResult)
def screen(request: ScreenRequest) -> ScreeningResult:
    result = SCRIPTS.get(request.resume.liepin_user_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"剧本未定义候选人 {request.resume.liepin_user_id}",
        )
    if (
        not request.llm_scoring
        and result.status is not CandidateStatus.REJECTED_HARD
    ):
        # 镜像 screening 服务：inbound 直索要——仅硬规则，评分后移至简历收到后
        return _result(
            hard_pass=True,
            score=None,
            judge_reason="硬规则通过（评分后移至简历收到后）",
            status=CandidateStatus.SCREENED_PASS,
        )
    return result


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
