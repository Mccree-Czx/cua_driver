"""确定性工具 MCP server（B 方案 P1）：Pi 内核调用的状态/数据/一人一消息工具。

复用 pipeline 现有模块（models / state_machine / messaging / db），
工具方法全部确定性（无 LLM 决策）——Pi 是唯一调用方，负责决策与编排。

启动：<pipeline venv python> tools_mcp_server.py（stdio）
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import redis as redis_lib
from mcp.server.mcpserver import MCPServer
from PIL import Image, ImageChops
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.messaging import OneMessagePerCandidateError, ensure_no_out_message
from app.models import Candidate, Interaction, Job, JobCandidate, TaskLog
from app.screening_client import ScreeningClient
from app.state_machine import (
    InvalidTransition,
    StateEvent,
    TransitionContext,
    transition,
)
from app.storage import ObjectStore, snapshot_object_key
from hr_workbuddy import CandidateStatus, MinimalResume, ScreenRequest
from hr_workbuddy.rate_limit import TokenBucket

mcp = MCPServer("hr-tools")

RISK_PAUSE_KEY = "cua:risk:paused"
# 优先用 mobile-use 同款硬编码位置（已部署），否则回退 PATH
_ADB_SDK = os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe")
ADB = _ADB_SDK if os.path.exists(_ADB_SDK) else "adb"

_RESUME_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "zip": "application/zip",
}


@lru_cache
def _redis() -> redis_lib.Redis:
    return redis_lib.Redis.from_url(get_settings().redis_url, decode_responses=True)


@lru_cache
def _bucket() -> TokenBucket:
    return TokenBucket(_redis())


@lru_cache
def _screening() -> ScreeningClient:
    return ScreeningClient()


@lru_cache
def _store() -> ObjectStore:
    return ObjectStore()


@mcp.tool()
def candidate_get_or_create(
    liepin_user_id: str, name: str = "", source: str = "inbound"
) -> dict:
    """按 liepin_user_id 幂等取/建候选人（source: inbound|recommended）。"""
    with SessionLocal() as session:
        cand = session.execute(
            select(Candidate).where(Candidate.liepin_user_id == liepin_user_id)
        ).scalar_one_or_none()
        if cand is None:
            cand = Candidate(
                liepin_user_id=liepin_user_id,
                name=name or liepin_user_id,
                online_resume_minimal={},
                source=source,
            )
            session.add(cand)
            session.commit()
            return {"candidate_id": cand.id, "created": True}
        return {"candidate_id": cand.id, "created": False}


@mcp.tool()
def candidate_update_snapshot(
    liepin_user_id: str, name: str, resume: dict, source: str | None = None
) -> dict:
    """刷新候选人姓名/在线简历快照（resume 为 MinimalResume 7 字段 dict）。"""
    with SessionLocal() as session:
        cand = session.execute(
            select(Candidate).where(Candidate.liepin_user_id == liepin_user_id)
        ).scalar_one_or_none()
        if cand is None:
            return {"error": f"候选人 {liepin_user_id} 不存在"}
        cand.name = name
        cand.online_resume_minimal = resume
        if source is not None:
            cand.source = source
        session.commit()
        return {"ok": True, "candidate_id": cand.id}


@mcp.tool()
def jc_get_or_create(job_id: int, candidate_id: int) -> dict:
    """幂等取/建 (job, candidate) 关系行。"""
    with SessionLocal() as session:
        jc = session.execute(
            select(JobCandidate).where(
                JobCandidate.job_id == job_id,
                JobCandidate.candidate_id == candidate_id,
            )
        ).scalar_one_or_none()
        if jc is None:
            cand = session.get(Candidate, candidate_id)
            if cand is None:
                return {"error": f"candidate {candidate_id} 不存在"}
            jc = JobCandidate(job_id=job_id, candidate_id=candidate_id)
            jc.candidate = cand  # 状态机路径消歧读 jc.candidate.source
            session.add(jc)
            session.commit()
            return {"jc_id": jc.id, "created": True}
        return {"jc_id": jc.id, "created": False}


@mcp.tool()
def jc_status(jc_id: int) -> dict:
    """读 jc 状态。"""
    with SessionLocal() as session:
        jc = session.get(JobCandidate, jc_id)
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


_TOUCH_EVENTS = frozenset(
    {StateEvent.GREET, StateEvent.REQUEST_RESUME, StateEvent.AWAIT_RESUME}
)


@mcp.tool()
def state_transition(
    jc_id: int,
    event: str,
    source: str | None = None,
    target_status: str | None = None,
) -> dict:
    """推进状态机（11 态边集，非法迁移拒绝）。

    event 取值：screen_pass/reject_hard/reject_llm/greet/request_resume/
    await_resume/receive_resume/no_response/hr_review/close/override。
    路径相关边需 source（inbound|recommended）；override 需 target_status。

    守卫（先落账后推进）：触达类事件（greet/request_resume/await_resume）
    要求该 jc 已有 direction=out 的互动流水，否则拒绝推进。
    锚点自动落库：greet/request_resume → last_touch_at；await_resume →
    resume_requested_at（72h 关闭锚点）+ last_touch_at；receive_resume →
    resume_downloaded_at。
    """
    with SessionLocal() as session:
        jc = session.get(JobCandidate, jc_id)
        if jc is None:
            return {"error": f"job_candidate {jc_id} 不存在"}
        try:
            ev = StateEvent(event)
        except ValueError as exc:
            return {"error": f"非法参数：{exc}"}
        if ev in _TOUCH_EVENTS:
            has_out = session.execute(
                select(Interaction.id)
                .where(
                    Interaction.job_candidate_id == jc_id,
                    Interaction.direction == "out",
                )
                .limit(1)
            ).first()
            if has_out is None:
                return {
                    "error": f"触达类事件 {event} 要求先落 direction=out 互动流水（先落账后推进，拒绝）"
                }
        try:
            ctx = TransitionContext(
                source=source,
                target_status=(
                    CandidateStatus(target_status) if target_status else None
                ),
            )
            updated = transition(jc, ev, ctx)
        except InvalidTransition as exc:
            return {"error": f"非法迁移：{exc}"}
        old = jc.status
        jc.status = updated.status
        now = datetime.now()
        if ev in (StateEvent.GREET, StateEvent.REQUEST_RESUME):
            jc.last_touch_at = now
        if ev is StateEvent.AWAIT_RESUME:
            jc.last_touch_at = now
            jc.resume_requested_at = now  # 72h 关闭锚点
        if ev is StateEvent.RECEIVE_RESUME:
            jc.resume_downloaded_at = now
        session.commit()
        return {
            "jc_id": jc_id,
            "old_status": old,
            "new_status": jc.status,
            "last_touch_at": jc.last_touch_at.isoformat() if jc.last_touch_at else None,
            "resume_requested_at": (
                jc.resume_requested_at.isoformat() if jc.resume_requested_at else None
            ),
            "resume_downloaded_at": (
                jc.resume_downloaded_at.isoformat() if jc.resume_downloaded_at else None
            ),
        }


@mcp.tool()
def ensure_one_message(jc_id: int) -> dict:
    """一人一消息检查：jc 已有 out 消息则返回 error（拒绝二次发送）。"""
    with SessionLocal() as session:
        jc = session.get(JobCandidate, jc_id)
        if jc is None:
            return {"error": f"job_candidate {jc_id} 不存在"}
        try:
            ensure_no_out_message(session, jc)
        except OneMessagePerCandidateError as exc:
            return {"error": str(exc)}
        return {"ok": True, "jc_id": jc_id}


@mcp.tool()
def candidate_has_out_message(candidate_id: int) -> dict:
    """候选人级一人一消息检查（跨岗位）：任意 jc 已有 out 即 has_out=true。"""
    with SessionLocal() as session:
        exists = (
            session.execute(
                select(Interaction.id)
                .join(JobCandidate, JobCandidate.id == Interaction.job_candidate_id)
                .where(
                    JobCandidate.candidate_id == candidate_id,
                    Interaction.direction == "out",
                )
                .limit(1)
            ).first()
            is not None
        )
        return {"candidate_id": candidate_id, "has_out": exists}


@mcp.tool()
def interaction_log(
    jc_id: int,
    direction: str,
    msg_type: str,
    content: str = "",
    sent_at: str | None = None,
) -> dict:
    """落互动流水。direction: out|in；msg_type: greet_request|reply|attachment|direct_request。

    direction=out 守卫：该 jc 已有 out 流水则拒绝（一人一消息，拒绝第二条）。
    """
    with SessionLocal() as session:
        jc = session.get(JobCandidate, jc_id)
        if jc is None:
            return {"error": f"job_candidate {jc_id} 不存在"}
        if direction == "out":
            try:
                ensure_no_out_message(session, jc)
            except OneMessagePerCandidateError as exc:
                return {"error": str(exc)}
        session.add(
            Interaction(
                job_candidate_id=jc_id,
                direction=direction,
                msg_type=msg_type,
                content=content or None,
                sent_at=datetime.fromisoformat(sent_at) if sent_at else datetime.now(),
            )
        )
        session.commit()
        return {"ok": True, "jc_id": jc_id}


@mcp.tool()
def tasklog_add(
    task_id: str,
    outcome: str,
    attempt: int = 0,
    tokens: int = 0,
    cost: float = 0.0,
    duration: float = 0.0,
    note: str | None = None,
) -> dict:
    """任务成本落账。outcome: success|failed_retryable|failed_needs_manual。"""
    with SessionLocal() as session:
        session.add(
            TaskLog(
                task_id=task_id,
                outcome=outcome,
                attempt=attempt,
                tokens=tokens,
                cost=cost,
                duration=duration,
                note=note,
            )
        )
        session.commit()
        return {"ok": True}


@mcp.tool()
def job_get(job_id: int) -> dict:
    """读岗位（title/jd_text/hard_rules/template_msgs/scoring_prefs/llm_threshold/status）。"""
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            return {"error": f"job {job_id} 不存在"}
        return {
            "id": job.id,
            "title": job.title,
            "jd_text": job.jd_text,
            "hard_rules": job.hard_rules,
            "template_msgs": job.template_msgs,
            "scoring_prefs": job.scoring_prefs,
            "llm_threshold": job.llm_threshold,
            "status": job.status,
        }


@mcp.tool()
def job_list_active() -> dict:
    """列 active 岗位。"""
    with SessionLocal() as session:
        jobs = (
            session.execute(select(Job).where(Job.status == "active")).scalars().all()
        )
        return {
            "jobs": [
                {"id": j.id, "title": j.title, "llm_threshold": j.llm_threshold}
                for j in jobs
            ]
        }


@mcp.tool()
def job_upsert(
    title: str,
    jd_text: str = "",
    hard_rules: dict | None = None,
    scoring_prefs: dict | None = None,
    status: str = "active",
) -> dict:
    """按 title 幂等建岗位：已存在返回 {created: false}，不存在则创建返回 {created: true}。"""
    with SessionLocal() as session:
        job = session.execute(select(Job).where(Job.title == title)).scalar_one_or_none()
        if job is not None:
            return {"id": job.id, "created": False}
        job = Job(
            title=title,
            jd_text=jd_text,
            hard_rules=hard_rules or {},
            scoring_prefs=scoring_prefs or {},
            status=status,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return {"id": job.id, "created": True}


@mcp.tool()
def rate_acquire(key: str, rate_per_window: int, window_seconds: int) -> dict:
    """固定窗口令牌桶：成功 {allowed: true} 并占 1 令牌；超限 {allowed: false} 不占。"""
    allowed = _bucket().acquire(key, rate_per_window, window_seconds)
    return {"allowed": allowed, "key": key}


@mcp.tool()
def risk_check() -> dict:
    """全局风控熔断查询（cua:risk:paused）。paused=true 时所有触达类操作应停止。"""
    return {"paused": _redis().get(RISK_PAUSE_KEY) is not None}


@mcp.tool()
def risk_set() -> dict:
    """写全局风控熔断标志（检测到风控/安全验证页时调用，其余任务零操作）。"""
    _redis().set(RISK_PAUSE_KEY, "1")
    return {"ok": True, "paused": True}


@mcp.tool()
def risk_clear() -> dict:
    """清除风控熔断标志（人工完成安全验证后调用）。"""
    _redis().delete(RISK_PAUSE_KEY)
    return {"ok": True, "paused": False}


@mcp.tool()
def screen(
    job_id: int,
    resume: dict,
    jd_text: str,
    hard_rules: dict,
    jc_id: int | None = None,
    min_stars: int = 3,
    scoring_prefs: dict | None = None,
    llm_scoring: bool = True,
) -> dict:
    """硬规则 + LLM 评分判定（1-5 星）。resume 为 MinimalResume 7 字段 dict。
    返回 {hard_pass, hard_reasons, score(1-5), judge_reason, status, degraded}；
    status ∈ screened_pass|rejected_hard|rejected_llm。
    传 jc_id 时把 score/judge_reason 写回 job_candidate.match_score/judge_reason。"""
    minimal = MinimalResume.model_validate(resume)
    result = _screening().screen(
        ScreenRequest(
            job_id=job_id,
            resume=minimal,
            jd_text=jd_text,
            hard_rules=hard_rules,
            min_stars=min_stars,
            scoring_prefs=scoring_prefs or {},
            llm_scoring=llm_scoring,
        )
    )
    if jc_id is not None:
        with SessionLocal() as session:
            jc = session.get(JobCandidate, jc_id)
            if jc is not None:
                jc.match_score = result.score
                jc.judge_reason = result.judge_reason
                session.commit()
    return result.model_dump(mode="json")


DAILY_TOUCH_CAP = 100


@mcp.tool()
def quota_status() -> dict:
    """当日触达额度：查 Redis 每日令牌桶计数（与 gated_tap 同源），返回 {used_today, daily_cap, remaining}。"""
    key = f"msg-touch:{datetime.now():%Y%m%d}"
    used = int(_redis().get(key) or 0)
    return {"used_today": used, "daily_cap": DAILY_TOUCH_CAP, "remaining": max(0, DAILY_TOUCH_CAP - used)}


@mcp.tool()
def gated_tap(x: int, y: int, touch: bool = False, daily_cap: int = DAILY_TOUCH_CAP, counts_daily: bool = True) -> dict:
    """闸门 + 原子 tap（硬化：触达类点击的强制前置检查）。

    touch=True（立即沟通/发送/索要等会触达候选人的点击）→ 先过风控熔断；
    counts_daily=True（主动打招呼，outbound）再扣每日令牌桶（默认 100 次/天），
    任一不过则**不执行 tap**。inbound 的回复/索要属响应性触达，传 counts_daily=False
    （只过风控熔断、不计入每日 100）。touch=False（浏览/进详情等读操作）直接执行。"""
    if touch:
        if _redis().get(RISK_PAUSE_KEY) is not None:
            return {"executed": False, "reason": "risk_paused（全局熔断中，拒绝触达）"}
        if counts_daily:
            key = f"msg-touch:{datetime.now():%Y%m%d}"
            if not _bucket().acquire(key, daily_cap, 86400):
                return {"executed": False, "reason": f"rate_limited（每日 {daily_cap} 次已满，拒绝触达）"}
    result = subprocess.run(
        [ADB, "shell", "input", "tap", str(int(x)), str(int(y))],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return {"executed": False, "reason": result.stderr.strip()[:200]}
    return {"executed": True, "reason": None, "touch_checked": touch}


@mcp.tool()
def snapshot_put(liepin_user_id: str, file_path: str) -> dict:
    """在线简历截图归档 MinIO（snapshots/{liepin_user_id}/{YYYYMMDD}_{HHMMSS}.png），
    并把键回写到 candidate.snapshot_object_key。"""
    data = Path(file_path).read_bytes()
    key = snapshot_object_key(
        liepin_user_id, datetime.now().date(), datetime.now().strftime("%H%M%S")
    )
    _store().put_object(_store().bucket, key, data, "image/png")
    with SessionLocal() as session:
        cand = session.execute(
            select(Candidate).where(Candidate.liepin_user_id == liepin_user_id)
        ).scalar_one_or_none()
        if cand is not None:
            cand.snapshot_object_key = key
            session.commit()
    return {"object_key": key}


@mcp.tool()
def resume_put(
    job_id: int,
    liepin_user_id: str,
    name: str,
    job_title: str,
    file_path: str,
    ext: str = "pdf",
) -> dict:
    """附件简历归档 MinIO（resumes/ 键规范，重名自动加时间戳）。"""
    data = Path(file_path).read_bytes()
    ext = ext.lower().lstrip(".") or "pdf"
    if ext == "pdf":
        key = _store().put_resume(
            job_id, liepin_user_id, name, job_title, datetime.now().date(), data
        )
    else:
        from app.storage import resume_object_key

        base = resume_object_key(job_id, liepin_user_id, name, job_title, datetime.now().date())
        key = f"{base[:-len('.pdf')]}.{ext}"
        _store().put_object(
            _store().bucket,
            key,
            data,
            _RESUME_CONTENT_TYPES.get(ext, "application/octet-stream"),
        )
    return {"object_key": key}


@mcp.tool()
def close_stale_awaiting() -> dict:
    """72h 巡检：awaiting_resume 且 resume_requested_at 超 72h → no_response→closed。"""
    from app.sweep import close_stale_awaiting as _close

    with SessionLocal() as session:
        closed = _close(session)
        session.commit()
        return {"closed": closed}


@mcp.tool()
def list_awaiting() -> dict:
    """awaiting_resume 状态的 jc 列表（sweep 轮次用）。"""
    with SessionLocal() as session:
        rows = (
            session.execute(
                select(JobCandidate).where(
                    JobCandidate.status == CandidateStatus.AWAITING_RESUME.value
                )
            )
            .scalars()
            .all()
        )
        return {"jc_ids": [r.id for r in rows]}


@mcp.tool()
def capture_attachment_pages(liepin_user_id: str, max_pages: int = 10) -> dict:
    """把当前屏幕（附件 PDF 查看器）逐页截图归档，并异步生成 HTML 简历网页。

    前提：Pi 已导航到附件 PDF 查看器。循环 screencap → 上滑 → screencap，
    连续两张几乎相同判定「到底」即停；max_pages 兜底。每页 PNG 归档到
    MinIO；首页截图喂 flash 视觉提取结构化 JSON、渲染 HTML、存 MinIO 的
    慢步骤放后台线程异步执行（不阻塞 Pi），最终写回 resume_html_key。
    """
    keys: list[str] = []
    prev: bytes | None = None
    first_png: bytes | None = None
    # 回到顶部：向下滑 max_pages 次，确保从 PDF 顶部开始截
    for _ in range(max_pages):
        _swipe_down()
        time.sleep(0.5)
    for i in range(max_pages):
        png = _screencap()
        if prev is not None and not _images_differ(prev, png):
            break  # 到底：滚动后屏幕未变化
        key = snapshot_object_key(
            liepin_user_id,
            datetime.now().date(),
            f"p{i}_{datetime.now().strftime('%H%M%S')}",
        )
        _store().put_object(_store().bucket, key, png, "image/png")
        keys.append(key)
        prev = png
        if first_png is None:
            first_png = png
        if i < max_pages - 1:
            _swipe_up()
            time.sleep(1.0)
    if keys:
        with SessionLocal() as session:
            cand = session.execute(
                select(Candidate).where(Candidate.liepin_user_id == liepin_user_id)
            ).scalar_one_or_none()
            if cand is not None:
                cand.snapshot_object_key = keys[0]  # 首页键回写（前端 snapshot_url 依赖）
                session.commit()
    # flash → HTML → MinIO 放后台线程异步执行，Pi 立即返回继续处理下一个
    if first_png is not None:
        threading.Thread(
            target=_resume_html_async, args=(liepin_user_id, first_png), daemon=True
        ).start()
    return {
        "object_keys": keys,
        "page_count": len(keys),
        "snapshot_object_key": keys[0] if keys else None,
    }


def _resume_html_async(liepin_user_id: str, png: bytes) -> None:
    """后台线程：flash 视觉 → 结构化 JSON → HTML → MinIO → 写回 resume_html_key。"""
    try:
        resume = _flash_vision_extract(png)
        if not resume:
            return
        html = _render_resume_html(resume)
        key = f"resumes/{liepin_user_id}/resume.html"
        _store().put_object(_store().bucket, key, html.encode("utf-8"), "text/html")
        with SessionLocal() as session:
            cand = session.execute(
                select(Candidate).where(Candidate.liepin_user_id == liepin_user_id)
            ).scalar_one_or_none()
            if cand is not None:
                cand.resume_html_key = key
                session.commit()
    except Exception:  # noqa: BLE001  # 异步失败不影响主流程
        pass


def _llm_env(name: str, default: str = "") -> str:
    """读环境变量或向上遍历找到的 .env（flash 视觉用，不硬编码 key）。"""
    v = os.environ.get(name, "")
    if v:
        return v
    d = Path(__file__).resolve().parent
    for _ in range(5):
        env_file = d / ".env"
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith(name + "="):
                        return line.split("=", 1)[1].strip()
            except OSError:
                pass
            break
        d = d.parent
    return default


def _flash_vision_extract(png: bytes) -> dict:
    """deepseek-flash 视觉：简历截图 → 结构化 JSON（逐字忠实提取）。"""
    import openai

    api_key = _llm_env("SCREENING_LLM_API_KEY")
    if not api_key:
        return {}
    client = openai.OpenAI(
        base_url=_llm_env("SCREENING_LLM_BASE_URL", "https://api.deepseek.com"),
        api_key=api_key,
    )
    prompt = (
        "你是简历解析助手。把这张简历截图中的文字**逐字完整**提取成结构化 JSON。"
        "铁律：不要概括、不要省略、不要改写，原图每个字段都保留。"
        '键用中文，结构：{"姓名":..,"求职意向":..,"所在地":..,'
        '"联系方式":{"电话":..,"邮箱":..,"微信":..},'
        '"职业简介":..,"核心能力":[..],'
        '"工作经历":[{"公司":..,"职位":..,"时间":..,"地点":..,"描述":..}],'
        '"教育":..,"语言":..}。只输出 JSON，不要多余解释。'
    )
    resp = client.chat.completions.create(
        model=_llm_env("SCREENING_LLM_MODEL", "deepseek-flash"),
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64.b64encode(png).decode()}"}},
        ]}],
        timeout=120,
    )
    content = resp.choices[0].message.content
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return {}
        return {}


_RESUME_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>简历</title>
<style>
:root{--bg:#f4f5f7;--fg:#1f2937;--muted:#64748b;--card:#fff;--line:#e5e7eb;--accent:#2c5f8a;--accent-soft:#e8eff5}
@media(prefers-color-scheme:dark){:root{--bg:#10151c;--fg:#e5e7eb;--muted:#9ca3af;--card:#1a212b;--line:#2a333f;--accent:#7aa7cc;--accent-soft:#1c2c3d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.6;padding:24px 16px}
.page{max-width:760px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px;margin-bottom:14px}
.name{font-size:26px;font-weight:700;margin:0 0 4px}
.role{font-size:16px;font-weight:500;color:var(--accent);margin:0 0 10px}
.meta{display:flex;flex-wrap:wrap;gap:6px 14px;color:var(--muted);font-size:13px}
.meta b{color:var(--fg);font-weight:500}
h2{font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 12px}
.summary{font-size:14px;margin:0;white-space:pre-line}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{background:var(--accent-soft);color:var(--accent);font-size:13px;padding:5px 12px;border-radius:999px}
.timeline{position:relative;padding-left:20px}
.timeline:before{content:"";position:absolute;left:5px;top:6px;bottom:6px;width:2px;background:var(--line)}
.job{position:relative;padding-bottom:18px}
.job:last-child{padding-bottom:0}
.job:before{content:"";position:absolute;left:-20px;top:7px;width:12px;height:12px;border-radius:50%;background:var(--card);border:3px solid var(--accent)}
.job-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px}
.company{font-weight:700;font-size:15px}
.position{color:var(--accent);font-size:14px;font-weight:500}
.period{color:var(--muted);font-size:12px}
.location{color:var(--muted);font-size:12px;margin:2px 0 6px}
.desc{font-size:13.5px;margin:0;white-space:pre-line}
.edu{font-size:14px}
.lang{display:inline-block;font-size:13px;padding:4px 10px;border:1px solid var(--line);border-radius:8px;margin:0 8px 8px 0}
</style>
</head>
<body><div class="page" id="app"></div>
<script>
var R=__DATA__;
function el(t,c,x){var e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
var app=document.getElementById("app");
var h=el("header","card");h.appendChild(el("h1","name",R.姓名||""));h.appendChild(el("p","role",R.求职意向||""));
var m=el("div","meta");if(R.所在地){var l=el("span");l.appendChild(el("b",null,"所在地 "));l.appendChild(document.createTextNode(R.所在地));m.appendChild(l)}
var c=R.联系方式||{};for(var k in c){var s=el("span");s.appendChild(el("b",null,k+" "));s.appendChild(document.createTextNode(c[k]));m.appendChild(s)}h.appendChild(m);app.appendChild(h);
if(R.职业简介){var su=el("section","card");su.appendChild(el("h2",null,"职业简介"));su.appendChild(el("p","summary",R.职业简介));app.appendChild(su)}
if(R.核心能力){var sk=el("section","card");sk.appendChild(el("h2",null,"核心能力"));var cs=el("div","chips");(Array.isArray(R.核心能力)?R.核心能力:[R.核心能力]).forEach(function(x){cs.appendChild(el("span","chip",x))});sk.appendChild(cs);app.appendChild(sk)}
if(R.工作经历&&R.工作经历.length){var ex=el("section","card");ex.appendChild(el("h2",null,"工作经历"));var tl=el("div","timeline");R.工作经历.forEach(function(j){var b=el("div","job");var hd=el("div","job-head");hd.appendChild(el("span","company",j.公司||""));hd.appendChild(el("span","position",j.职位||""));hd.appendChild(el("span","period",j.时间||""));b.appendChild(hd);if(j.地点)b.appendChild(el("div","location",j.地点));b.appendChild(el("p","desc",j.描述||""));tl.appendChild(b)});ex.appendChild(tl);app.appendChild(ex)}
if(R.教育){var ed=el("section","card");ed.appendChild(el("h2",null,"教育背景"));var eduList=Array.isArray(R.教育)?R.教育:[R.教育];eduList.forEach(function(e){var t;if(typeof e==="string"){t=e}else if(e&&typeof e==="object"){t=[e.学校||e.school||"",e.专业||e.major||"",e.时间||e.period||""].filter(Boolean).join(" · ")}else{t=String(e)}if(t)ed.appendChild(el("div","edu",t))});app.appendChild(ed)}
if(R.语言){var lg=el("section","card");lg.appendChild(el("h2",null,"语言能力"));var lw=el("div");(Array.isArray(R.语言)?R.语言:[R.语言]).forEach(function(x){lw.appendChild(el("span","lang",x))});lg.appendChild(lw);app.appendChild(lg)}
</script>
</body></html>"""


def _render_resume_html(resume: dict) -> str:
    """结构化简历 JSON → 自包含 HTML 简历页（卡片式排版）。"""
    return _RESUME_HTML_TEMPLATE.replace("__DATA__", json.dumps(resume, ensure_ascii=False))


def _screencap() -> bytes:
    result = subprocess.run(
        [ADB, "exec-out", "screencap", "-p"], capture_output=True, timeout=20
    )
    if result.returncode != 0:
        raise RuntimeError(f"screencap 失败：{result.stderr[:200]!r}")
    return result.stdout


def _swipe_up() -> None:
    subprocess.run(
        [ADB, "shell", "input", "swipe", "540", "1800", "540", "400", "500"],
        capture_output=True,
        timeout=20,
    )


def _swipe_down() -> None:
    subprocess.run(
        [ADB, "shell", "input", "swipe", "540", "400", "540", "1800", "500"],
        capture_output=True,
        timeout=20,
    )


# PDF 查看器翻页按钮坐标（Flutter 渲染，uiautomator 不可见；真机截图测得）
PDF_PREV = (100, 2147)  # 上一页（左箭头）
PDF_NEXT = (980, 2147)  # 下一页（右箭头）


def _pdf_tap(x: int, y: int) -> None:
    subprocess.run(
        [ADB, "shell", "input", "tap", str(x), str(y)],
        capture_output=True,
        timeout=20,
    )


def _images_differ(a: bytes, b: bytes, threshold_ratio: float = 0.001) -> bool:
    """两张 PNG 是否「明显不同」：不同像素占比 > threshold_ratio。"""
    ia = Image.open(io.BytesIO(a)).convert("L")
    ib = Image.open(io.BytesIO(b)).convert("L")
    if ia.size != ib.size:
        return True
    diff = ImageChops.difference(ia, ib)
    if diff.getbbox() is None:
        return False  # 完全相同
    hist = diff.histogram()  # 256 桶灰度直方图
    changed = sum(hist[11:])  # 值 > 10 的像素数
    total = ia.size[0] * ia.size[1]
    return changed / total > threshold_ratio


if __name__ == "__main__":
    mcp.run()
