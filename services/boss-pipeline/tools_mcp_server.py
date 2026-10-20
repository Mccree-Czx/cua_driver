"""确定性工具 MCP server（BOSS 版）：Pi 内核调用的状态/数据/一人一消息/触达工具。

复用 boss-pipeline 现有模块（models / state_machine / hr_tools），
工具方法全部确定性（无 LLM 决策）——Pi 是唯一调用方，负责决策与编排。

启动：<boss-pipeline venv python> tools_mcp_server.py（stdio）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 必须先锚定工作目录再导入 app.*：pydantic-settings 的 env_file=".env" 是**按 CWD**
# 解析的，而本 server 由 Pi 拉起，Pi 的工作目录不是我们说了算。
#
# ★ 这里锚到 `services/boss-pipeline`，**不是**照猎聘锚到仓根（parents[2]）：
#   仓根的 `.env` 是**猎聘**的（DATABASE_URL→hr_workbuddy、REDIS_URL→db 0）。
#   BOSS 这边根本没有自己的 `.env`——`scripts/boss/pipeline.cmd` 是
#   `cd services/boss-pipeline && uv run uvicorn ...`，读不到 .env 就退回
#   config.py 的默认值（boss_workbuddy、Redis db 1）。MCP server 必须跟服务本体
#   **同一套解析**，否则会静默连到猎聘的库 / db 0，症状极隐蔽（工具照跑、数据写错）。
os.chdir(Path(__file__).resolve().parent)

from mcp.server.mcpserver import MCPServer
from sqlalchemy import func, select

from app import hr_tools, models
from app.config import get_settings
from app.db import SessionLocal
from app.state_machine import (
    RECOMMENDED,
    InvalidTransition,
    StateEvent,
    TransitionContext,
)
from hr_workbuddy import CandidateStatus

mcp = MCPServer("hr-tools")


# 触达类事件：推进这些状态之前，该 jc 必须已有 direction=out 的互动流水（先落账后推进）。
_TOUCH_EVENTS = frozenset(
    {StateEvent.GREET, StateEvent.REQUEST_RESUME, StateEvent.AWAIT_RESUME}
)


# ——— 读：职位 ———

@mcp.tool()
def job_list_active() -> dict:
    """列出系统敢用的职位（status=active 且 state=开放中 且 jd_complete）。单账号。"""
    with SessionLocal() as session:
        jobs = hr_tools.job_list_active(session)
        return {
            "count": len(jobs),
            "jobs": [
                {
                    "job_id": j.id,
                    "title": j.title,
                    "state": j.state,
                    "details": j.details,
                    "jd_complete": j.jd_complete,
                }
                for j in jobs
            ],
        }


@mcp.tool()
def job_get(job_id: int) -> dict:
    """读单个职位全文（含 jd_text）。单账号。"""
    with SessionLocal() as session:
        job = session.get(models.Job, job_id)
        if job is None:
            return {"error": f"job {job_id} 不存在"}
        return {
            "job_id": job.id,
            "title": job.title,
            "state": job.state,
            "details": job.details,
            "jd_text": job.jd_text or "",
            "jd_complete": job.jd_complete,
            "tags": job.tags or [],
        }


# ——— 建档 ———

@mcp.tool()
def candidate_get_or_create(
    name: str,
    boss_key: str = "",
    job_title: str = "",
    job_and_company: str = "",
    work_status: str = "",
    work_year: str = "",
    degree: str = "",
    age: str = "",
    description: str | None = None,
    expect_job_city: str = "",
    expect_salary: str = "",
    expect_industry: str = "",
    summary: str = "",
    tags: list[str] | None = None,
    company_history: list[str] | None = None,
    active: str = "",
) -> dict:
    """幂等取/建候选人并把详情字段叠上去。

    ★ 主键 = `boss_key or name`——BOSS 实测退回只用 `name`（列表卡与详情页的
      公司串会不一样，别把公司串拼进主键）。**建档时留 `boss_key` 为空即可。**
    ★ **空值不盖已有**：列表页缺的详情字段不会冲掉详情页已采到的值。
    ★ 给了 `job_title` 且职位存在 → 顺带建 jc，返回里带 `jc_id`。
    """
    with SessionLocal() as session:
        return hr_tools.candidate_get_or_create(
            session,
            name=name, boss_key=boss_key, job_title=job_title,
            job_and_company=job_and_company, work_status=work_status,
            work_year=work_year, degree=degree, age=age, description=description,
            expect_job_city=expect_job_city, expect_salary=expect_salary,
            expect_industry=expect_industry, summary=summary, tags=tags,
            company_history=company_history, active=active,
        )


@mcp.tool()
def jc_get_or_create(job_id: int, candidate_id: int) -> dict:
    """幂等取/建 (job, candidate) 关系行。两端的 id 必须真实存在。"""
    with SessionLocal() as session:
        job = session.get(models.Job, job_id)
        cand = session.get(models.Candidate, candidate_id)
        if job is None or cand is None:
            return {"error": f"job {job_id} 或 candidate {candidate_id} 不存在"}
        existed = session.scalar(
            select(models.JobCandidate)
            .where(models.JobCandidate.job_id == job_id)
            .where(models.JobCandidate.candidate_id == candidate_id)
        )
        jc = hr_tools.jc_get_or_create(session, job_id, candidate_id)
        session.commit()
        return {"jc_id": jc.id, "created": existed is None}


@mcp.tool()
def jc_status(jc_id: int) -> dict:
    """读 jc 状态。"""
    with SessionLocal() as session:
        jc = session.get(models.JobCandidate, jc_id)
        if jc is None:
            return {"error": f"job_candidate {jc_id} 不存在"}
        return {
            "jc_id": jc.id,
            "job_id": jc.job_id,
            "candidate_id": jc.candidate_id,
            "status": jc.status,
            "match_score": jc.match_score,
            "judge_reason": jc.judge_reason,
        }


# ——— 状态推进 ———

@mcp.tool()
def state_transition(
    jc_id: int,
    event: str,
    source: str | None = None,
    target_status: str | None = None,
) -> dict:
    """推进状态机（非法迁移拒绝）。

    event 取值：screen_pass/reject_hard/reject_llm/greet/request_resume/
    await_resume/receive_resume/no_response/hr_review/close/override。
    路径相关边需 source（inbound|recommended）；override 需 target_status。

    ★ 守卫（先落账后推进）：触达类事件（greet/request_resume/await_resume）
      要求该 jc 已有 direction=out 的互动流水，否则拒绝——防止「先推进、后落账」。
      正常打招呼路径是 `gated_tap` 后 `interaction_log(out, greet)`，那里会顺带推进。
    """
    with SessionLocal() as session:
        jc = session.get(models.JobCandidate, jc_id)
        if jc is None:
            return {"error": f"job_candidate {jc_id} 不存在"}
        try:
            ev = StateEvent(event)
        except ValueError as exc:
            return {"error": f"非法 event：{exc}"}
        if ev in _TOUCH_EVENTS:
            has_out = session.scalar(
                select(func.count())
                .select_from(models.Interaction)
                .where(models.Interaction.job_candidate_id == jc_id)
                .where(models.Interaction.direction == "out")
            )
            if not has_out:
                return {
                    "error": f"触达类事件 {event} 要求先落 direction=out 互动流水（先落账后推进，拒绝）"
                }
        old = jc.status
        try:
            ctx = TransitionContext(
                source=source,
                target_status=(
                    CandidateStatus(target_status) if target_status else None
                ),
            )
            updated = hr_tools.apply_transition(session, jc, ev, ctx)
        except (InvalidTransition, ValueError) as exc:
            return {"error": f"非法迁移：{exc}"}
        return {
            "jc_id": jc_id,
            "old_status": old,
            "new_status": updated.status,
            "last_touch_at": updated.last_touch_at.isoformat() if updated.last_touch_at else None,
            "resume_requested_at": (
                updated.resume_requested_at.isoformat() if updated.resume_requested_at else None
            ),
        }


# ——— 联锁 ———

@mcp.tool()
def greet_check(jc_id: int) -> dict:
    """打招呼总闸门：风控 → 一人一消息 → 评分 → 配额，全过才 allowed。

    ★ 有副作用：new 会**按需评分**（推进到 screened_pass/rejected_*）；通过时会**扣配额**。
      打招呼前必须先调这里；返回 allowed=false 就停手，绝不 gated_tap。
    """
    with SessionLocal() as session:
        jc = session.get(models.JobCandidate, jc_id)
        if jc is None:
            return {"error": f"job_candidate {jc_id} 不存在"}
        allowed, reason = hr_tools.may_greet(session, jc)
        return {"jc_id": jc.id, "allowed": allowed, "reason": reason}


@mcp.tool()
def has_out_greet(jc_id: int) -> dict:
    """这个人我们这边是否已经打过招呼（系统动作账本）。"""
    with SessionLocal() as session:
        jc = session.get(models.JobCandidate, jc_id)
        if jc is None:
            return {"error": f"job_candidate {jc_id} 不存在"}
        return {"jc_id": jc.id, "has_out_greet": hr_tools.has_out_greet(session, jc)}


@mcp.tool()
def risk_check() -> dict:
    """现在是否处于风控熔断（读不到 Redis 时 fail-closed 返回 True）。"""
    return {"risk_paused": hr_tools.risk_check(), "reason": hr_tools.risk_reason()}


@mcp.tool()
def risk_set(reason: str = "") -> dict:
    """置上熔断：所有写动作立即停手，直到有人查清原因并手动清掉。"""
    hr_tools.risk_set(reason or "未注明原因")
    return {"ok": True, "risk_paused": True}


@mcp.tool()
def risk_clear() -> dict:
    """清除熔断。只在查清原因之后由人/Pi 显式调用。"""
    hr_tools.risk_clear()
    return {"ok": True, "risk_paused": hr_tools.risk_check()}


@mcp.tool()
def quota_status() -> dict:
    """当日打招呼额度：used_today / daily_cap / remaining。"""
    cap = get_settings().daily_greet_cap
    used = hr_tools.quota_used()
    return {"used_today": used, "daily_cap": cap, "remaining": max(0, cap - used)}


# ——— 写动作 ———

@mcp.tool()
def gated_tap(x: int, y: int, touch: bool = False) -> dict:
    """闸门 + 原子 tap：触达类点击的强制前置检查。

    touch=True（立即沟通/发送/索要等会触达候选人的点击）→ 先过风控熔断，
    熔断中**不执行 tap**。touch=False（浏览/进详情等读操作）直接执行。

    ★ 铁律：打招呼/发送/索要只走这里（touch=True），**绝不用 mobile_tap 发消息**。
      每日限额由 `greet_check` 的 `quota_acquire` 管，这里不重复扣。
    """
    return hr_tools.gated_tap(x, y, touch=touch)


# ——— 评分 ———

@mcp.tool()
def screen(jc_id: int) -> dict:
    """给一个还没评过分的 jc 打分并落库（status + match_score + judge_reason）。

    ★ 只在 `new` 上执行；已评分/已推进的不重复评。服务不可用 → error（fail-closed，不落库）。
    """
    with SessionLocal() as session:
        jc = session.get(models.JobCandidate, jc_id)
        if jc is None:
            return {"error": f"job_candidate {jc_id} 不存在"}
        if jc.status != CandidateStatus.NEW.value:
            return {"error": f"job_candidate {jc_id} 状态 {jc.status} 不是 new，不重复评分"}
        result = hr_tools.screen_candidate(session, jc)
        if result is None:
            return {"error": "评分服务不可用（fail-closed，未落库）"}
        return {
            "jc_id": jc.id,
            "status": jc.status,
            "score": result.score,
            "judge_reason": result.judge_reason,
        }


# ——— 记账 / 收尾 ———

@mcp.tool()
def interaction_log(jc_id: int, direction: str, msg_type: str, content: str = "") -> dict:
    """记一条系统动作流水。

    ★ 只记「系统对候选人做了什么」；对方说的话别往这里写（那是 messages 的事）。
    ★ out+greet 时**顺带推进状态**（screened_pass → greeted，照 api.py 的
      ingest_interactions 同款）；推进失败则报错，别假装成功。
    """
    with SessionLocal() as session:
        jc = session.get(models.JobCandidate, jc_id)
        if jc is None:
            return {"error": f"job_candidate {jc_id} 不存在"}
        if msg_type not in models.INTERACTION_MSG_TYPES:
            return {"error": f"msg_type 必须是 {models.INTERACTION_MSG_TYPES} 之一"}
        row = hr_tools.interaction_log(session, jc, direction, msg_type, content)
        if direction == "out" and msg_type == "greet":
            try:
                hr_tools.apply_transition(
                    session, jc, StateEvent.GREET, TransitionContext(source=RECOMMENDED)
                )
            except InvalidTransition as exc:
                return {
                    "error": f"互动已落账，但推进 greet 失败：{exc}",
                    "interaction_id": row.id,
                }
        return {"interaction_id": row.id, "jc_id": jc.id, "status": jc.status}


@mcp.tool()
def tasklog_add(
    task_id: str,
    round_name: str,
    outcome: str,
    attempt: int = 0,
    tokens: int = 0,
    cost: float = 0.0,
    duration: float = 0.0,
    note: str = "",
) -> dict:
    """记一条轮次成本/结果（BOSS 无 account_id，用 round_name 归口）。"""
    with SessionLocal() as session:
        row = hr_tools.tasklog_add(
            session, task_id=task_id, round_name=round_name, outcome=outcome,
            attempt=attempt, tokens=tokens, cost=cost, duration=duration, note=note,
        )
        return {"tasklog_id": row.id}


@mcp.tool()
def daily_sweep() -> dict:
    """日常收尾：72h 无人回应的 awaiting_resume 关闭 + 给没分的 new 补评分（上限 20）。"""
    with SessionLocal() as session:
        return hr_tools.daily_sweep(session)


if __name__ == "__main__":
    mcp.run()
