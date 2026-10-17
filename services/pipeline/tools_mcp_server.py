"""确定性工具 MCP server（B 方案 P1）：Pi 内核调用的状态/数据/一人一消息工具。

复用 pipeline 现有模块（models / state_machine / messaging / db），
工具方法全部确定性（无 LLM 决策）——Pi 是唯一调用方，负责决策与编排。

启动：<pipeline venv python> tools_mcp_server.py（stdio）
"""
from __future__ import annotations

import html
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 必须先锚定工作目录再导入 app.*：pydantic-settings 的 env_file=".env" 是**按 CWD**
# 解析的，而本 server 由 Pi 拉起，Pi 的工作目录不是我们说了算。赌错的症状很隐蔽——
# 工具照样能跑，只是 DATABASE_URL/REDIS_URL 静默退回到默认值，连到本机那个空库。
# 与 login_watch.py 同一处理。本文件其余路径全是绝对的（/sdcard、__file__、tempdir），
# 所以 chdir 不会碰坏别的东西。
os.chdir(Path(__file__).resolve().parents[2])

import redis as redis_lib
from mcp.server.mcpserver import MCPServer
from PIL import Image, ImageChops
from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.keys import msg_touch_key, risk_pause_key
from app.login_request import (
    PHASE_AWAITING_CODE,
    PHASE_DISPATCHED,
    PHASE_FAILED,
    PHASE_SUCCESS,
    PHASE_VERIFYING,
    RedisLoginRequest,
    mark_account_logged_in,
)
from app.messaging import OneMessagePerCandidateError, ensure_no_out_message
from app.job_sync import MIN_JD_CHARS, norm_title
from app.models import (
    JOB_STATUS_ACTIVE,
    JOB_STATUS_DRAFT,
    Account,
    Candidate,
    Interaction,
    Job,
    JobCandidate,
    TaskLog,
)
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

def _resolve_adb() -> str:
    """定位 adb 可执行文件。

    原先是把 Windows 的 `%LOCALAPPDATA%\\Android\\Sdk\\...` 硬编码进常量——在 Mac 上
    那个路径根本不展开，只能回退到 PATH 里的 `adb`。而 launchd 拉起时 PATH 未必带
    Homebrew（run_round._resolve_pi_cli() 已经踩过同一个坑）。按「部署时的实际位置」
    逐個探测，比赌 PATH 稳。

    可用 HR_ADB 覆盖（换机器/换安装方式时不必改代码）。
    """
    candidates = [
        os.environ.get("HR_ADB"),
        # macOS + brew cask（当前部署形态：/opt/homebrew/bin/adb 是符号链接）
        "/opt/homebrew/bin/adb",
        # macOS 官方 SDK 默认位置
        str(Path.home() / "Library/Android/sdk/platform-tools/adb"),
        # Windows（旧的部署形态，保留兼容）
        os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
        # 云机执行端（.66）把 platform-tools 解压在这里（2026-10-16 实测补）
        r"C:\platform-tools\adb.exe",
        shutil.which("adb"),
    ]
    for exe in candidates:
        if exe and Path(exe).exists():
            return str(exe)
    return "adb"  # 交给 PATH，失败时报错更直白


ADB = _resolve_adb()

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


class NoActiveAccountError(RuntimeError):
    """没有生效的猎聘账号。所有工具都会提前拒掉——见 _active_account。"""


def _active_account_id(session) -> int:
    """当前生效账号的 id（多账号隔离的锚点）。

    每个工具进来先解析账号，再把它加进查询条件。**不修改任何工具的签名**：
    `agent/system-prompt.md` 与 `agent/rounds.md` 里写死了工具名与参数，
    改签名会连带改提示词、增加 Pi 跑偏的风险。

    取不到账号时直接抛错而不是猜一个——猜错会把候选人档案与配额记到别的号头上。
    """
    acct = session.execute(
        select(Account.id)
        .where(Account.is_active.is_(True), Account.deleted_at.is_(None))
        .order_by(Account.id)
    ).scalars().first()
    if acct is None:
        raise NoActiveAccountError("没有生效的猎聘账号，请先在前端账号页选择一个")
    return acct


def _current_account_id() -> int:
    """纯 Redis 工具（风控/配额）用的账号解析——它们不进 DB，没有现成 session。"""
    with SessionLocal() as session:
        return _active_account_id(session)


def _has_attachment_evidence(jc: JobCandidate) -> bool:
    """该 jc 的候选人是否已有真实附件归档证据（capture_attachment_pages 写入）。

    receive_resume / interaction_log(attachment) 守卫用：在线简历截图
    （snapshot_put）不写 attachment_keys，故不会被误判为「已收到附件简历」。
    """
    cand = jc.candidate
    return bool(cand is not None and cand.attachment_keys)


@mcp.tool()
def candidate_get_or_create(
    liepin_user_id: str, name: str = "", source: str = "inbound"
) -> dict:
    """按 liepin_user_id 幂等取/建候选人（source: inbound|recommended）。仅当前账号。"""
    # ★ 空 ID 直接拒绝。2026-10-17 实测：Pi 把返回字段 `candidate_id` 误当成
    #   `candidate.id`，用一个空值建出一条 liepin_user_id="" 的孤儿候选人——
    #   没有 jc、没有互动，前端看不到，却永久污染 candidates 表（而且没有删除工具）。
    #   空 ID 没有任何合法语义：按数据契约它应当是 ts_YYYYMMDD_HHMMSS。
    if not (liepin_user_id or "").strip():
        return {"error": "liepin_user_id 不能为空（用抓取时间戳 ts_YYYYMMDD_HHMMSS）"}
    with SessionLocal() as session:
        acct = _active_account_id(session)
        cand = session.execute(
            select(Candidate).where(
                Candidate.account_id == acct,
                Candidate.liepin_user_id == liepin_user_id,
            )
        ).scalar_one_or_none()
        if cand is None:
            cand = Candidate(
                account_id=acct,
                liepin_user_id=liepin_user_id,
                name=name or liepin_user_id,
                online_resume_minimal={},
                source=source,
            )
            session.add(cand)
            session.commit()
            # 安全网：完整隔离下每个号有独立的候选人库，同一人可能被两个号
            # 各打一次招呼。此处不阻断（那是用户选定的语义），只把事实带回去，
            # 让 Pi 在轮次汇报里带出来供人工判断。
            other = session.execute(
                select(Candidate.account_id)
                .where(
                    Candidate.liepin_user_id == liepin_user_id,
                    Candidate.account_id != acct,
                )
                .limit(1)
            ).scalars().first()
            out = {"candidate_id": cand.id, "created": True}
            if other is not None:
                out["cross_account_warning"] = (
                    f"该候选人在账号 {other} 下已有档案——两个号可能重复触达同一人"
                )
            return out
        return {"candidate_id": cand.id, "created": False}


@mcp.tool()
def candidate_update_snapshot(
    liepin_user_id: str, name: str, resume: dict, source: str | None = None
) -> dict:
    """刷新候选人姓名/在线简历快照（resume 为 MinimalResume 7 字段 dict）。仅当前账号。"""
    with SessionLocal() as session:
        acct = _active_account_id(session)
        cand = session.execute(
            select(Candidate).where(
                Candidate.account_id == acct,
                Candidate.liepin_user_id == liepin_user_id,
            )
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
    """幂等取/建 (job, candidate) 关系行。仅当前账号——跨账号的 id 一律拒绝。"""
    with SessionLocal() as session:
        acct = _active_account_id(session)
        cand = session.get(Candidate, candidate_id)
        if cand is None or cand.account_id != acct:
            return {"error": f"candidate {candidate_id} 不存在"}
        job = session.get(Job, job_id)
        if job is None or job.account_id != acct:
            return {"error": f"job {job_id} 不存在"}
        jc = session.execute(
            select(JobCandidate).where(
                JobCandidate.account_id == acct,
                JobCandidate.job_id == job_id,
                JobCandidate.candidate_id == candidate_id,
            )
        ).scalar_one_or_none()
        if jc is None:
            jc = JobCandidate(
                account_id=acct, job_id=job_id, candidate_id=candidate_id
            )
            jc.candidate = cand  # 状态机路径消歧读 jc.candidate.source
            session.add(jc)
            session.commit()
            return {"jc_id": jc.id, "created": True}
        return {"jc_id": jc.id, "created": False}


@mcp.tool()
def jc_status(jc_id: int) -> dict:
    """读 jc 状态。仅当前账号——跨账号的 jc 按不存在处理。"""
    with SessionLocal() as session:
        acct = _active_account_id(session)
        jc = session.get(JobCandidate, jc_id)
        if jc is None or jc.account_id != acct:
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
    守卫（附件证据）：receive_resume 要求该候选人已有真实附件归档证据
    （capture_attachment_pages 写入的 attachment_keys），否则拒绝——在线简历
    截图不得把候选人标成 resume_received。
    锚点自动落库：greet/request_resume → last_touch_at；await_resume →
    resume_requested_at（72h 关闭锚点）+ last_touch_at；receive_resume →
    resume_downloaded_at。
    """
    with SessionLocal() as session:
        acct = _active_account_id(session)
        jc = session.get(JobCandidate, jc_id)
        if jc is None or jc.account_id != acct:
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
        if ev is StateEvent.RECEIVE_RESUME and not _has_attachment_evidence(jc):
            return {
                "error": (
                    f"receive_resume 拒绝：job_candidate {jc_id} 无附件归档证据"
                    "（当前只有在线简历截图）。在线简历≠附件简历；无附件请走索要"
                    "简历路径（interaction_log(out) → state_transition(request_resume/await_resume)）"
                )
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
    """一人一消息检查：jc 已有 out 消息则返回 error（拒绝二次发送）。仅当前账号。"""
    with SessionLocal() as session:
        acct = _active_account_id(session)
        jc = session.get(JobCandidate, jc_id)
        if jc is None or jc.account_id != acct:
            return {"error": f"job_candidate {jc_id} 不存在"}
        try:
            ensure_no_out_message(session, jc)
        except OneMessagePerCandidateError as exc:
            return {"error": str(exc)}
        return {"ok": True, "jc_id": jc_id}


@mcp.tool()
def candidate_has_out_message(candidate_id: int) -> dict:
    """候选人级一人一消息检查（跨岗位）：任意 jc 已有 out 即 has_out=true。仅当前账号。"""
    with SessionLocal() as session:
        acct = _active_account_id(session)
        exists = (
            session.execute(
                select(Interaction.id)
                .join(JobCandidate, JobCandidate.id == Interaction.job_candidate_id)
                .where(
                    JobCandidate.account_id == acct,
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
    direction=in + msg_type=attachment 守卫：要求该候选人已有附件归档证据
    （capture_attachment_pages 写入），否则拒绝——在线简历截图不得记为附件。
    """
    with SessionLocal() as session:
        acct = _active_account_id(session)
        jc = session.get(JobCandidate, jc_id)
        if jc is None or jc.account_id != acct:
            return {"error": f"job_candidate {jc_id} 不存在"}
        if direction == "out":
            try:
                ensure_no_out_message(session, jc)
            except OneMessagePerCandidateError as exc:
                return {"error": str(exc)}
        if (
            direction == "in"
            and msg_type == "attachment"
            and not _has_attachment_evidence(jc)
        ):
            return {
                "error": (
                    f"attachment 互动拒绝：job_candidate {jc_id} 无附件归档证据"
                    "（在线简历不得记为 attachment；无附件时不要落 attachment 流水）"
                )
            }
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
    """任务成本落账。outcome: success|failed_retryable|failed_needs_manual。

    带上当前账号供归因（/manual-queue 会按它过滤）。取不到账号时记 NULL 而不是
    报错——这是系统级的成本账，不该因为账号没选而丢失。
    """
    with SessionLocal() as session:
        try:
            acct: int | None = _active_account_id(session)
        except NoActiveAccountError:
            acct = None
        session.add(
            TaskLog(
                task_id=task_id,
                account_id=acct,
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
    """读岗位（title/jd_text/hard_rules/template_msgs/scoring_prefs/llm_threshold/status）。仅当前账号。"""
    with SessionLocal() as session:
        acct = _active_account_id(session)
        job = session.get(Job, job_id)
        if job is None or job.account_id != acct:
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
    """列 active 岗位。仅当前账号——手机上登的是哪个号，看到的就是哪个号的职位。"""
    with SessionLocal() as session:
        acct = _active_account_id(session)
        jobs = (
            session.execute(
                select(Job).where(Job.account_id == acct, Job.status == "active")
            )
            .scalars()
            .all()
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
    """按 title 幂等建岗位：已存在返回 {created: false}，不存在则创建返回 {created: true}。

    仅当前账号内按 title 判重——两个号可以有同名职位，各建各的。

    ★ **没有像样的 JD 就不许 active**（2026-10-15 补的守卫）：签名默认
      `jd_text=""` + `status="active"`，也就是说原先可以建出一个「没有 JD 但在招」的
      职位，然后被 `job_list_active()` 选中、拿去给**真实候选人**打分。这不是假想——
      `agent/rounds.md:29` 里就有一条「jd_text 匹配不到时 `job_upsert(title=沟通职位,
      jd_text="")`」的指示。现在这种情况自动落到 `draft`，并在返回值里说明原因。
    """
    downgraded = status == JOB_STATUS_ACTIVE and len((jd_text or "").strip()) < MIN_JD_CHARS
    if downgraded:
        status = JOB_STATUS_DRAFT
    with SessionLocal() as session:
        acct = _active_account_id(session)
        # ★ 按**归一标题**判重，不是精确相等：猎聘上的标题用全角括号、库里手工建的
        #   可能是半角，精确比会同一个职位建两行（2026-10-15 实测撞上）。
        #   一个账号的职位就那么几个，取回来在 Python 侧比，代价可忽略。
        key = norm_title(title)
        job = next(
            (
                j
                for j in session.execute(
                    select(Job).where(Job.account_id == acct)
                ).scalars()
                if norm_title(j.title) == key
            ),
            None,
        )
        if job is not None:
            return {"id": job.id, "created": False}
        job = Job(
            account_id=acct,
            title=title,
            jd_text=jd_text,
            hard_rules=hard_rules or {},
            scoring_prefs=scoring_prefs or {},
            status=status,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        result = {"id": job.id, "created": True}
        if downgraded:
            result["status"] = status
            result["notice"] = (
                f"JD 不足 {MIN_JD_CHARS} 字，已按 draft 建。"
                "该职位不会参与打招呼，补完 JD 在前端启用后才生效。"
            )
        return result


@mcp.tool()
def rate_acquire(key: str, rate_per_window: int, window_seconds: int) -> dict:
    """固定窗口令牌桶：成功 {allowed: true} 并占 1 令牌；超限 {allowed: false} 不占。"""
    allowed = _bucket().acquire(key, rate_per_window, window_seconds)
    return {"allowed": allowed, "key": key}


@mcp.tool()
def risk_check() -> dict:
    """当前账号的风控熔断查询。paused=true 时所有触达类操作应停止。

    按账号分片：熔断描述的是「手机上那个号」被猎聘限流了。
    """
    return {"paused": _redis().get(risk_pause_key(_current_account_id())) is not None}


@mcp.tool()
def risk_set() -> dict:
    """写全局风控熔断标志（检测到风控/安全验证页时调用，其余任务零操作）。"""
    _redis().set(risk_pause_key(_current_account_id()), "1")
    return {"ok": True, "paused": True}


@mcp.tool()
def risk_clear() -> dict:
    """清除风控熔断标志（人工完成安全验证后调用）。"""
    _redis().delete(risk_pause_key(_current_account_id()))
    return {"ok": True, "paused": False}


# —— 登录（批次 2）——
#
# 跨机器：前端在 CT104 发起 → 写 Redis → Mac 上的 login_watch.py 拉起 login_round
# → Pi 用下面这几个工具读写进度。手机号从 MySQL 取（PII 不进 Redis），密码走
# login_fill_password()，明文永不进 LLM 上下文。


@mcp.tool()
def adb_selftest() -> dict:
    """自检：本进程能不能真的驱动手机。

    存在意义：命令行里 `adb devices` 跑得通，**不代表 Pi 拉起的 MCP 进程里也跑得通**
    ——PATH、工作目录、adb 路径探测都可能不同。换号这类不可逆操作开跑之前，先用它
    确认这条路是通的，避免「探测阶段全绿、真动手时才发现连不上」。
    """
    try:
        out = subprocess.run(
            [ADB, "devices"], capture_output=True, text=True, encoding="utf-8", timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "adb_path": ADB, "devices": [], "reason": type(exc).__name__}
    devices = [
        line.split("\t")[0]
        for line in out.stdout.splitlines()[1:]
        if "\t" in line and line.split("\t")[1].strip() == "device"
    ]
    return {
        "ok": bool(devices),
        "adb_path": ADB,
        "devices": devices,
        "reason": "" if devices else (out.stdout + out.stderr).strip()[:200],
    }


def _ui_edittexts(xml: str) -> list[dict]:
    """界面上所有 EditText，带文字与边界。"""
    out = []
    for node in re.findall(r"<node\b[^>]*>", xml):
        if 'class="android.widget.EditText"' not in node:
            continue
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', node))
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
        if not match:
            continue
        x1, y1, x2, y2 = map(int, match.groups())
        out.append(
            {
                "text": html.unescape(attrs.get("text", "")),
                "focused": attrs.get("focused") == "true",
                "bounds": (x1, y1, x2, y2),
                "center": ((x1 + x2) // 2, (y1 + y2) // 2),
            }
        )
    return out


def _clear_button_near(xml: str, field: dict) -> tuple[int, int] | None:
    """输入框右侧那个清除小×。

    它是 Flutter 的 ImageView，**无文字且 clickable=false**——不能按 clickable 过滤，
    只能按「与输入框垂直重叠、且在其右边缘附近」来找。
    """
    x1, y1, x2, y2 = field["bounds"]
    for node in re.findall(r"<node\b[^>]*>", xml):
        if "ImageView" not in node:
            continue
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', node))
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
        if not match:
            continue
        a, b, c, d = map(int, match.groups())
        cx, cy = (a + c) // 2, (b + d) // 2
        if x1 < cx <= x2 + 130 and y1 - 40 <= cy <= y2 + 40:
            return (cx, cy)
    return None


def _clear_focused_field() -> bool:
    """清空当前焦点输入框；返回是否确实空了。

    **不能用连发的退格。** 猎聘是 Flutter，实测一次发 40 个 `KEYCODE_DEL` 只生效 7 个，
    连发 15 个则**一个都不生效**——而单个退格是有效的。原因就是 Flutter 丢弃快速连续
    的按键。（这条在原生 EditText 的「设置」搜索框上验不出来：那里没有这个问题，
    所以最早那版「一次发 40 个」的写法看起来是好的。）
    所以：优先点输入框自带的清除小×，找不到才逐个慢速退格。
    """
    for _ in range(3):
        xml = _ui_dump()
        fields = [f for f in _ui_edittexts(xml) if f["focused"]]
        if not fields:
            return False
        field = fields[0]
        if not field["text"]:
            return True
        clear = _clear_button_near(xml, field)
        if clear:
            _adb_shell("input", "tap", str(clear[0]), str(clear[1]))
            time.sleep(1.0)
            continue
        for _ in range(14):
            _adb_shell("input", "keyevent", "KEYCODE_DEL")
            time.sleep(0.3)
    fields = [f for f in _ui_edittexts(_ui_dump()) if f["focused"]]
    return bool(fields) and not fields[0]["text"]


def _type_into_focused(text: str) -> dict:
    """清空当前焦点框，再把文本敲进去。

    **必须先清空**：`input text` 是追加不是覆盖，而猎聘登录页的手机号是**预填**的
    （App 记住了上次的号）。直接敲就会变成两个号粘在一起——而且不会报错，
    只会在点下一步之后才以「账号不存在」的形式暴露。

    文本走 shlex.quote：`adb shell` 会把命令字符串交给设备端的 sh 重新解析，
    裸拼就是命令注入。空格要先换成 `%s`，那是 `input text` 自己的转义；
    不换的话设备端 sh 会把它当成参数分隔符，`input text` 只拿到第一个词。
    """
    try:
        if not _clear_focused_field():
            return {"ok": False, "reason": "clear_failed"}
        result = subprocess.run(
            [ADB, "shell", "input", "text", shlex.quote(text.replace(" ", "%s"))],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "reason": "input_failed"}
    if result.returncode != 0:
        return {"ok": False, "reason": "input_failed"}
    return {"ok": True}


def _focused_edittext_text() -> str | None:
    """当前焦点输入框里的文本；找不到返回 None。

    只认 EditText：dump 里 SearchView 排在它前面，而 SearchView 的 `text`
    **永远是空串**——按类名一起匹配会永远读到空（实测踩过这个坑）。
    """
    fields = [f for f in _ui_edittexts(_ui_dump()) if f["focused"]]
    return fields[0]["text"] if fields else None


def _login_account(account_id: int | None) -> tuple[int | None, str]:
    """解析要操作的账号，返回 (id, 错误原因)。

    ★ 省略 account_id 时只认**在途**的请求：终态请求是上一轮的残留，
      拿它的账号去敲手机号 = **把旧号的号敲进手机**（这个函数的下游就是
      `login_fill_phone`，真的会往输入框里打字）。
    """
    if account_id is None:
        req = RedisLoginRequest().active()
        if req is None:
            return None, "no_active_request"
        account_id = req["account_id"]
    with SessionLocal() as session:
        acct = session.get(Account, account_id)
        if acct is None:
            return None, "no_account"
        return acct.id, ""


@mcp.tool()
def login_fill_phone(account_id: int | None = None) -> dict:
    """把账号绑定的手机号敲进手机**当前焦点的输入框**。

    先自己 tap 到手机号输入框，再调本工具。省略 account_id 时用当前登录请求
    指向的账号。手机号是纯数字，能回读校验。
    """
    acct_id, err = _login_account(account_id)
    if err:
        return {"ok": False, "reason": err}
    with SessionLocal() as session:
        phone = session.get(Account, acct_id).phone or ""
    if not phone:
        return {"ok": False, "reason": "no_phone"}
    typed = _type_into_focused(phone)
    if not typed["ok"]:
        return {"ok": False, "reason": typed["reason"]}
    # 数字框的 text 可读，回读校验是真的校验
    return {"ok": True, "typed_len": len(phone), "verified": _focused_edittext_text() == phone}


@mcp.tool()
def login_req_get() -> dict:
    """读当前登录请求。**每轮开头先调这个**，按 phase 与 method 决定做什么。

    返回 {phase, method, account_id, account_name, phone, resend_seq, attempts, notice}。
    phase:
      pending       → 按 method 操作手机：password 走账号密码页；code 走到「验证码已发送」
      awaiting_code → 调 login_req_claim_code() 取人工填的码，填进去并提交
    其它相位（dispatched/verifying/success/failed/…）都不该由本工具驱动。
    method:
      password → 全自动；失败要**在同一轮内降级**到 code 路径并写明原因
      code     → 需要人工填码
    """
    with SessionLocal() as session:
        req = RedisLoginRequest().get()
        if req is None:
            return {"phase": "idle"}
        acct = session.get(Account, req["account_id"])
        return {
            "phase": req["phase"],
            "method": req["method"],
            "account_id": req["account_id"],
            "account_name": acct.name if acct else None,
            "phone": acct.phone if acct else None,
            "has_password": bool(acct and acct.password_enc),
            "resend_seq": req["resend_seq"],
            "attempts": req["attempts"],
            "notice": req["notice"] or None,
        }


@mcp.tool()
def login_req_set(phase: str, note: str = "") -> dict:
    """回报登录进度。phase 取值：
      dispatched     已开始操作手机
      awaiting_code  已点「获取验证码」，等人工在前端填码
      verifying      已拿到码，正在提交
      success        登录成功（本工具会顺带把该账号写为登录态并设为生效账号）
      failed         失败，note 里写可读原因
    只有这几个值合法——写别的会被拒，避免提示词跑偏写出谁也不认识的相位。

    `note` 按相位分流：`failed` 时它是 error（红条，结论），其余相位它是 notice
    （黄条，过程说明）。「密码登录失败已改用验证码」就属于后者——**不静默换路**。
    """
    allowed = {
        PHASE_DISPATCHED,
        PHASE_AWAITING_CODE,
        PHASE_VERIFYING,
        PHASE_SUCCESS,
        PHASE_FAILED,
    }
    if phase not in allowed:
        return {"ok": False, "error": f"phase 必须是 {sorted(allowed)} 之一"}
    store = RedisLoginRequest()
    # ★ 只认**在途**的请求（非终态且未过期）。这里以前用的是 `store.get()`，
    #   它把终态/过期的请求也照样返回——而下面那句 `mark_account_logged_in()`
    #   会**改生效账号**。于是：任何人在登录成功后 30 分钟内（hash TTL 1800s）
    #   拿一条旧请求来报「成功」，就能把 is_active 静默翻回旧号，且没有任何报错。
    #   今天实测就是靠这个，三个账号的登录态能同时为 true。
    req = store.active()
    if req is None:
        return {"ok": False, "error": "当前没有**在途**的登录请求（已结束或已超时），拒绝写相位"}
    if phase == PHASE_SUCCESS:
        # 登录成功 = 手机上现在登的就是这个号：写登录态 + 置为生效账号。
        # 实现抽在 login_request.mark_account_logged_in —— 确定性驱动路径也要走同一步。
        mark_account_logged_in(req["account_id"])
    store.set_phase(phase, note)
    return {"ok": True, "phase": phase, "account_id": req["account_id"]}


@mcp.tool()
def login_req_claim_code() -> dict:
    """原子取走人工填的验证码。取过一次就取不到第二次（防同一枚码被重复提交）。

    返回 {code} 或 {code: None, reason}。手动关闭提示后**不要**重试本工具，
    直接 login_req_set('failed', ...) 交回人工。
    """
    code = RedisLoginRequest().claim_code()
    if code:
        return {"code": code}
    return {"code": None, "reason": "没有可用的验证码（未填、已用过或已过期）"}


def _login_map_path() -> Path:
    """找锚点表。**按设备选文件**，与 `agent/flows.map_path()` 同一条规则。

    不用数 parents 层数：数错一层不会报错，只会静默返回 map_missing，
    到时候排查的是「探查没做」而不是「路径写错了」。

    ★ 云手机（`ANDROID_SERIAL` 形如 `host:port`）用的是另一份表
      `login_map.cloud.json`——分辨率与 App 版本都不同，坐标不通用。
      这条规则**必须与 agent 侧保持一致**（`agent/flows.map_path()` 是同一份逻辑的
      另一半，改一处要改两处），否则 Pi 读到的锚点和执行端实际点的不是同一份。
    """
    explicit = os.environ.get("HR_LOGIN_MAP")
    if explicit:
        return Path(explicit)
    name = (
        "login_map.cloud.json"
        if ":" in (os.environ.get("ANDROID_SERIAL") or "")
        else "login_map.json"
    )
    for base in Path(__file__).resolve().parents:
        candidate = base / "agent" / name
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parents[2] / "agent" / name


LOGIN_MAP_STALE_DAYS = 90


@mcp.tool()
def login_map_get() -> dict:
    """读登录界面的实测锚点表（`agent/login_map.json`）。

    锚点存文件而不是写进指令：猎聘一升级锚点就会变，改 JSON 不该等于改提示词。
    返回值里带 `age_days` 与 `stale`——**stale 为 true 时不要照着点**，先重新探查。
    """
    try:
        data = json.loads(_login_map_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "reason": "map_missing"}
    age_days = None
    try:
        probed = datetime.fromisoformat(data["probe"]["at"])
        age_days = (datetime.now() - probed).days
    except (KeyError, ValueError, TypeError):
        pass
    data["ok"] = True
    data["age_days"] = age_days
    data["stale"] = age_days is not None and age_days > LOGIN_MAP_STALE_DAYS
    return data


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
            acct = _active_account_id(session)
            jc = session.get(JobCandidate, jc_id)
            if jc is not None and jc.account_id == acct:
                jc.match_score = result.score
                jc.judge_reason = result.judge_reason
                session.commit()
    return result.model_dump(mode="json")


DAILY_TOUCH_CAP = 100


@mcp.tool()
def quota_status() -> dict:
    """当日触达额度：查 Redis 每日令牌桶计数（与 gated_tap 同源），返回 {used_today, daily_cap, remaining}。"""
    key = msg_touch_key(_current_account_id(), f"{datetime.now():%Y%m%d}")
    used = int(_redis().get(key) or 0)
    return {"used_today": used, "daily_cap": DAILY_TOUCH_CAP, "remaining": max(0, DAILY_TOUCH_CAP - used)}


@mcp.tool()
def job_quota_status() -> dict:
    """按职位均分当日打招呼额度：quota = floor(100/N)，used_today 从互动流水按职位统计今日 out。

    N 与流水都只看当前账号的职位——额度是按号算的。
    """
    with SessionLocal() as session:
        acct = _active_account_id(session)
        jobs = (
            session.execute(
                select(Job).where(Job.account_id == acct, Job.status == "active")
            )
            .scalars()
            .all()
        )
        n = len(jobs)
        if n == 0:
            return {"quota_each": 0, "jobs": []}
        quota_each = DAILY_TOUCH_CAP // n
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = session.execute(
            select(JobCandidate.job_id, func.count(Interaction.id))
            .join(Interaction, Interaction.job_candidate_id == JobCandidate.id)
            .where(
                JobCandidate.account_id == acct,
                Interaction.direction == "out",
                Interaction.sent_at >= today_start,
            )
            .group_by(JobCandidate.job_id)
        ).all()
        used_by_job = {job_id: cnt for job_id, cnt in rows}
        return {
            "quota_each": quota_each,
            "jobs": [
                {
                    "job_id": j.id,
                    "title": j.title,
                    "used_today": used_by_job.get(j.id, 0),
                    "quota": quota_each,
                }
                for j in jobs
            ],
        }


@mcp.tool()
def gated_tap(x: int, y: int, touch: bool = False, daily_cap: int = DAILY_TOUCH_CAP, counts_daily: bool = True) -> dict:
    """闸门 + 原子 tap（硬化：触达类点击的强制前置检查）。

    touch=True（立即沟通/发送/索要等会触达候选人的点击）→ 先过风控熔断；
    counts_daily=True（主动打招呼，outbound）再扣每日令牌桶（默认 100 次/天），
    任一不过则**不执行 tap**。inbound 的回复/索要属响应性触达，传 counts_daily=False
    （只过风控熔断、不计入每日 100）。touch=False（浏览/进详情等读操作）直接执行。"""
    if touch:
        _acct = _current_account_id()
        if _redis().get(risk_pause_key(_acct)) is not None:
            return {"executed": False, "reason": "risk_paused（全局熔断中，拒绝触达）"}
        if counts_daily:
            key = msg_touch_key(_acct, f"{datetime.now():%Y%m%d}")
            if not _bucket().acquire(key, daily_cap, 86400):
                return {"executed": False, "reason": f"rate_limited（每日 {daily_cap} 次已满，拒绝触达）"}
    result = subprocess.run(
        [ADB, "shell", "input", "tap", str(int(x)), str(int(y))],
        capture_output=True,
        text=True,
        encoding="utf-8",
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
        acct = _active_account_id(session)
        cand = session.execute(
            select(Candidate).where(
                Candidate.account_id == acct,
                Candidate.liepin_user_id == liepin_user_id,
            )
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
        acct = _active_account_id(session)
        closed = _close(session, acct)
        session.commit()
        return {"closed": closed}


@mcp.tool()
def list_awaiting() -> dict:
    """awaiting_resume 状态的 jc 列表（sweep 轮次用）。仅当前账号。"""
    with SessionLocal() as session:
        acct = _active_account_id(session)
        rows = (
            session.execute(
                select(JobCandidate).where(
                    JobCandidate.account_id == acct,
                    JobCandidate.status == CandidateStatus.AWAITING_RESUME.value,
                )
            )
            .scalars()
            .all()
        )
        return {"jc_ids": [r.id for r in rows]}


# 已废弃：有附件一律走 download_attachment 下载真实 PDF，不再逐页截图。
# 保留函数体仅供极端兜底/手动调用，不再注册为 MCP 工具（agent 不可见）。
def capture_attachment_pages(liepin_user_id: str, max_pages: int = 10) -> dict:
    """把当前屏幕（附件 PDF 查看器）逐页截图归档到 MinIO。

    前提：Pi 已导航到附件 PDF 查看器。循环 screencap → 上滑 → screencap，
    连续两张几乎相同判定「到底」即停；max_pages 兜底。每页 PNG 归档到
    MinIO，首页键回写 candidate.snapshot_object_key（HR 直接看截图）。
    """
    keys: list[str] = []
    prev: bytes | None = None
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
        if i < max_pages - 1:
            _swipe_up()
            time.sleep(1.0)
    if keys:
        with SessionLocal() as session:
            acct = _active_account_id(session)
            cand = session.execute(
                select(Candidate).where(
                    Candidate.account_id == acct,
                    Candidate.liepin_user_id == liepin_user_id,
                )
            ).scalar_one_or_none()
            if cand is not None:
                cand.snapshot_object_key = keys[0]  # 首页键回写（前端 snapshot_url 依赖）
                cand.attachment_keys = keys  # 附件归档证据（receive_resume 守依赖）
                session.commit()
    return {
        "object_keys": keys,
        "page_count": len(keys),
        "snapshot_object_key": keys[0] if keys else None,
    }


def _screencap() -> bytes:
    result = subprocess.run(
        [ADB, "exec-out", "screencap", "-p"], capture_output=True, timeout=20
    )
    if result.returncode != 0:
        raise RuntimeError(f"screencap 失败：{result.stderr[:200]!r}")
    return result.stdout


# ★★ 下面这一整批坐标（滑屏起止、PDF 翻页/转发按钮）都是 **1080x2244 的真机**测出来的。
#   **它们只服务真机那条「转发 → UC」的老路**；云机走的是「直取内部目录」（见
#   `_fetch_via_internal_dir`），完全不需要这些坐标，也不需要 UC。
#   换设备时如果走的是云机，就别再照抄这些坐标——它们是设备专属的。
#   （2026-10-16：原本这里写「云机要走路之前必须先重探」，后来发现云机有更短的路。）
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

# 附件 PDF 查看器「转发」按钮（右上角）；仅「头像→资料页→划到底→附件简历→PDF」路径有。
# 会话里直接点「附件简历」打开的 PDF 没有此按钮。
PDF_FORWARD = (1008, 151)

# UC 浏览器收到分享的 PDF 后，会把它缓存到外部可读目录（无 root 也能 adb pull）。
UC_CACHE_DIR = "/sdcard/Android/data/com.UCMobile/cache"


def _pdf_tap(x: int, y: int) -> None:
    subprocess.run(
        [ADB, "shell", "input", "tap", str(x), str(y)],
        capture_output=True,
        timeout=20,
    )


def _adb_shell(*args: str) -> str:
    """跑 adb shell 命令，返回 stdout 文本（stderr 丢弃）。"""
    result = subprocess.run([ADB, "shell", *args], capture_output=True, timeout=30)
    return result.stdout.decode("utf-8", errors="replace")


def _ui_dump() -> str:
    """dump 当前屏幕 UI 层级（uiautomator 会临时触发 Flutter 语义），返回 XML 文本。"""
    _adb_shell("uiautomator", "dump", "/sdcard/hr_u.xml")
    return _adb_shell("cat", "/sdcard/hr_u.xml")


def _uc_cache_pdfs() -> list[str]:
    """UC 外部缓存目录里现有的 .pdf 文件名列表（非递归）。"""
    out = _adb_shell("ls", UC_CACHE_DIR)
    names = []
    for line in out.splitlines():
        name = line.strip()
        if name.lower().endswith(".pdf"):
            names.append(name)
    return names


def _clear_uc_cache() -> None:
    """清空 UC 缓存目录下所有 .pdf。

    用 glob（`rm -f <dir>/*.pdf`）而非逐个 rm，避免含空格/中文文件名被
    adb shell 按空格拆散导致删不掉。
    """
    _adb_shell("rm", "-f", f"{UC_CACHE_DIR}/*.pdf")


# ——— 云机路径：直取猎聘内部附件目录（2026-10-16 实测）———

#: ★ 为什么云机不需要走 UC：真机的 adb 是 `uid=2000(shell)`，读 `/data/data/` 直接
#:   Permission denied，所以才要「转发给 UC → 从 UC 外部缓存 pull」绕一圈。
#:   云机的 adb 是 `uid=0(root)`，能直接读猎聘自己的目录——实测 `adb pull` 拿到的
#:   就是真 PDF（%PDF-1.7）。这条路短得多，也不依赖 UC 装没装。


def _direct_attachment_dir() -> str | None:
    """云机的「直接取件目录」；真机返回 None（真机表里显式写着 null）。

    ★ 判断**跟着锚点表走**：表本来就按设备分文件（`_login_map_path()` 看
      `ANDROID_SERIAL`），所以「这是不是云机」这件事已经有唯一来源，不再另造
      一个环境变量判断——两个来源迟早会不一致，而不一致就是静默点错。
    ★ 真机表里那一项**显式写 null** 而不是缺键：缺失与「有意没有」要能分辨，
      否则将来有人补上 UC 之外的路径时，会不知道 null 是漏配还是设计。
    """
    try:
        data = json.loads(_login_map_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    section = data.get("附件简历") or {}
    return section.get("直接取件目录") or None


def _adb_rooted() -> bool:
    """设备的 adb 是不是 root。

    ★ 必须显式判断：`_adb_shell` 丢弃 stderr，没有 root 时 `ls /data/data/...`
      返回**空 stdout**——看起来和「这个目录是空的」一模一样，于是会被静默
      当成「没有附件」。云机是多云有 root 开关的，关掉就退化成真机那种权限。
    """
    return _adb_shell("id", "-u").strip() == "0"


def _direct_attachments(directory: str) -> list[dict]:
    """附件目录里的 .pdf，按 mtime 从新到旧。每项 {name, size, mtime}。

    `ls -t -1` 一行一名（含空格/中文不会被拆开），`stat -c "%s %Y"` 一次拿大小与
    时间戳——大小用来判「写完了没」，时间戳用来判「是不是这一轮的」。

    ★ 格式串**必须一起 quote**：`adb shell` 是把参数用空格拼起来交给设备 sh 的，
      裸传 `"%s %Y"` 会变成 `stat -c %s %Y <路径>`，`%Y` 被当成另一个操作数，
      stdout 只剩大小 → 解析不出 → **整份文件被跳过，目录看起来是空的**
      （2026-10-16 实测踩到：明明 `ls` 能看到文件，这个函数却返回 []）。
    """
    quoted = shlex.quote(directory)
    fmt = shlex.quote("%s %Y")
    out: list[dict] = []
    for line in _adb_shell("ls", "-t", "-1", quoted).splitlines():
        name = line.strip()
        if not name.lower().endswith(".pdf"):
            continue
        meta = _adb_shell("stat", "-c", fmt, shlex.quote(f"{directory}/{name}")).split()
        if len(meta) != 2 or not all(v.isdigit() for v in meta):
            continue
        out.append({"name": name, "size": int(meta[0]), "mtime": int(meta[1])})
    return out


def _fetch_via_internal_dir(
    directory: str, name: str = "", *, max_wait: float = 30.0, max_age: float = 600.0
) -> tuple[str, bytes]:
    """云机取件：等最新的 PDF 落盘 → `adb pull` → 返回 `(文件名, 字节)`。

    ★ 与 UC 路径的 before/after diff 不同：那边是**先取快照再点转发**，文件必然
      在工具调用之后才出现；而这边 Pi 是**先把附件点开**才调工具，文件可能已经在
      盘上了。所以判据改成「最新一份 + mtime 足够新」，而不是「新出现的」。

    ★ `name` 是候选人姓名，用来**优先挑文件名的**：附件原始文件名通常含姓名
      （实测「莱顿大学-孔佳悦-销售.pdf」），这一条能挡住「取到别的候选人的简历」
      这种最坏的错——那也是这类工具最贵的错。

    ★ 落盘是**异步**的，光判「存在」不够——半写的文件也存在。所以要求
      「大小非 0」且「连续两次探得大小一致」才敢 pull。

    ★ 实测（2026-10-16）：猎聘会在**离开 PDF 查看器后清空这个目录**（我 17:38
      拉走的那份，17:41 就没了）。所以文件是**易失的**——点完要尽快取，别拖。
    """
    if not _adb_rooted():
        raise RuntimeError(
            f"设备 adb 不是 root（id -u != 0），读不了 {directory}。"
            "云机的 root 开关被关了？这条路走不通就得退回真机那种「转发给 UC」的取法。"
        )
    if _adb_shell("test", "-d", shlex.quote(directory), "&&", "echo", "yes").strip() != "yes":
        raise RuntimeError(f"附件目录不存在：{directory}（Pi 真的把附件打开了吗？）")

    started = time.time()
    deadline = time.monotonic() + max_wait
    pick: dict | None = None
    pending: tuple[str, int] | None = None
    seen: list[str] = []
    while time.monotonic() < deadline:
        items = _direct_attachments(directory)
        if items:
            seen = [i["name"] for i in items]
            named = [i for i in items if name and name in i["name"]]
            pool = named or [i for i in items if started - i["mtime"] <= max_age]
            if pool:
                top = max(pool, key=lambda i: i["mtime"])
                if top["size"] > 0:
                    if pending == (top["name"], top["size"]):
                        pick = top  # 连续两次大小一致 → 认为写完了
                        break
                    pending = (top["name"], top["size"])
        time.sleep(1.0)

    if pick is None:
        raise RuntimeError(
            f"等不到新的附件 PDF（{max_wait:.0f}s）。目录里现有：{seen or '（空）'}"
            f"，候选人是「{name or '未提供'}」——要么 Pi 没真的点开附件，"
            "要么落盘比预期慢（猎聘会在离开查看器后清空该目录，拖久了就没了）。"
        )

    remote = f"{directory}/{pick['name']}"
    local = Path(tempfile.gettempdir()) / pick["name"]
    try:
        # ★ remote 用**裸字面量**：`adb pull` 走 sync 协议、不经设备 sh，
        #   套上 shlex.quote 反而会把单引号写进路径。（实测中文+括号的文件名裸传可行。）
        pull = subprocess.run(
            [ADB, "pull", remote, str(local)], capture_output=True, timeout=60
        )
        if pull.returncode != 0 or not local.exists():
            raise RuntimeError(f"adb pull 失败：{pull.stderr[:200]!r}")
        got = local.stat().st_size
        if got != pick["size"]:
            raise RuntimeError(f"pull 回来的大小不符：设备 {pick['size']}、本地 {got}")
        data = local.read_bytes()
    finally:
        local.unlink(missing_ok=True)

    if data[:4] != b"%PDF":
        raise RuntimeError(
            f"拉到的不是 PDF（文件头 {data[:8]!r}）：{pick['name']}——别把它当简历归档"
        )

    # 只删**本次取走的这一份**，且 best-effort：不删则目录无界增长、下一轮更难判新旧；
    # 但删失败不该让整件事失败（简历已经拿到手了）。不删整个目录——可能删掉别人刚落的。
    _adb_shell("rm", "-f", shlex.quote(remote))
    return pick["name"], data


def _share_target_center(xml: str, keyword: str) -> tuple[int, int] | None:
    """在分享面板 XML 里找 text/content-desc 含 keyword 的目标，返回其标签中心坐标。

    分享面板（Android ChooserActivity，系统 UI）目标结构 = [标签 TextView(有
    bounds)] + [副标题 TextView(text 含 keyword, bounds=[0,0])]，副标题紧跟标签
    之后。这里取「keyword 节点前面最近一个有真实 bounds 的兄弟节点」中心；找不到
    返回 None。
    """
    nodes = re.findall(r"<node\b[^>]*>", xml)

    def _attr(nd: str, a: str) -> str:
        m = re.search(a + r'="([^"]*)"', nd)
        return m.group(1) if m else ""

    for i, nd in enumerate(nodes):
        hay = _attr(nd, "text") + "\n" + _attr(nd, "content-desc")
        if keyword in hay:
            # 先看自身是否有真实 bounds（keyword 就在标签上）
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", _attr(nd, "bounds"))
            if m and int(m.group(3)) > int(m.group(1)):
                return (
                    (int(m.group(1)) + int(m.group(3))) // 2,
                    (int(m.group(2)) + int(m.group(4))) // 2,
                )
            # 否则（keyword 是 bounds=[0,0] 的副标题）向前找最近有 bounds 的标签
            for j in range(i - 1, max(-1, i - 6), -1):
                b = _attr(nodes[j], "bounds")
                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
                if m and int(m.group(3)) > int(m.group(1)):
                    return (
                        (int(m.group(1)) + int(m.group(3))) // 2,
                        (int(m.group(2)) + int(m.group(4))) // 2,
                    )
    return None


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


def _fetch_via_uc() -> tuple[str, bytes]:
    """真机取件：点「转发」→ 分享面板选 UC → 轮询 UC 缓存 → pull → `(文件名, 字节)`。

    前置：Pi 已导航到附件 PDF 查看器（**「头像→资料页→划到底→附件简历→PDF」**路径，
    右上角才有「转发」；会话里直接点「附件简历」打开的那个没有）。

    ★ 真机为什么非要绕这一圈：它的 adb 是 `uid=2000(shell)`，读不了 `/data/data/`，
      拿不到猎聘自己的附件目录。云机有 root，走 `_fetch_via_internal_dir`，不经过这里。
    """
    # 0. 先清空 UC 缓存，避免「该 PDF 已被缓存」导致后续轮询等不到新文件
    _clear_uc_cache()
    before = set(_uc_cache_pdfs())

    # 1. 点「转发」弹系统分享面板
    _pdf_tap(*PDF_FORWARD)
    time.sleep(2.5)

    # 2. 分享面板目标会按最近使用重排，必须现场 dump；只认「PDF阅读器」（UC 的推荐目标）
    xml = _ui_dump()
    center = _share_target_center(xml, "PDF阅读器")
    if center is None:
        raise RuntimeError("未在分享面板找到 UC/PDF阅读器 目标")
    _pdf_tap(*center)

    # 3. 轮询 UC 外部缓存，等新 PDF 落盘（UC 打开 PDF 阅读器后即缓存）
    new_name = None
    for _ in range(30):
        time.sleep(1.0)
        diff = set(_uc_cache_pdfs()) - before
        if diff:
            new_name = sorted(diff)[0]
            break
    if new_name is None:
        raise RuntimeError(f"UC 缓存未出现新 PDF（原已有 {sorted(before)}）")

    # 4. adb pull 到本地临时文件
    local = Path(tempfile.gettempdir()) / new_name
    try:
        pull = subprocess.run(
            [ADB, "pull", f"{UC_CACHE_DIR}/{new_name}", str(local)],
            capture_output=True,
            timeout=60,
        )
        if pull.returncode != 0 or not local.exists():
            raise RuntimeError(f"pull 失败：{pull.stderr[:200]!r}")
        data = local.read_bytes()
    finally:
        # ★ 放进 finally：从前这行在最后、异常路径上不执行，临时文件会留在盘上
        local.unlink(missing_ok=True)

    # 7'. 清空 UC 缓存（清空全部 .pdf，避免含空格文件名删不掉）
    _clear_uc_cache()
    return new_name, data


def _archive_attachment(
    job_id: int,
    liepin_user_id: str,
    name: str,
    job_title: str,
    filename: str,
    data: bytes,
) -> str:
    """把取到的 PDF 归档 MinIO 并回写附件证据。**不含任何 adb**，所以可以单测。

    ★ 查不到 candidate 时**必须抛**（2026-10-16 修）：从前这里是 `if cand is not None:`
      静默跳过、**仍然返回成功**——结果是 MinIO 里躺着一个孤儿对象，而
      `candidate.attachment_keys` 没有证据，`receive_resume` 照样被拒。
      查不到通常意味着「设备驱动的那个账号」与「当前生效账号」不是同一个
      （例如拿 dev 库去跑生产账号上的候选人）。
    """
    key = _store().put_resume(
        job_id, liepin_user_id, name, job_title, datetime.now().date(), data
    )

    with SessionLocal() as session:
        acct = _active_account_id(session)
        cand = session.execute(
            select(Candidate).where(
                Candidate.account_id == acct,
                Candidate.liepin_user_id == liepin_user_id,
            )
        ).scalar_one_or_none()
        if cand is None:
            raise RuntimeError(
                f"已归档 {key}（{filename}，{len(data)} 字节），但在生效账号 {acct} 下"
                f"找不到候选人 liepin_user_id={liepin_user_id}——设备驱动的账号与当前"
                "生效账号很可能不是同一个。MinIO 里会留下一个孤儿对象，需人工清理。"
            )
        cand.attachment_keys = [key]
        jc = session.execute(
            select(JobCandidate).where(
                JobCandidate.account_id == acct,
                JobCandidate.candidate_id == cand.id,
                JobCandidate.job_id == job_id,
            )
        ).scalar_one_or_none()
        if jc is not None:
            jc.minio_object_key = key
            jc.resume_downloaded_at = datetime.now()
        session.commit()
    return key


@mcp.tool()
def download_attachment(
    job_id: int,
    liepin_user_id: str,
    name: str,
    job_title: str,
) -> dict:
    """把候选人发来的附件简历（真 PDF）取回来并归档 MinIO。

    前置：Pi 已把附件 PDF **打开**（右上角有没有「转发」不重要）。

    取件方式**按设备自动分派**（跟着锚点表走）：
    - **云手机**（adb 是 root）：直接 `adb pull` 猎聘内部附件目录里最新的那份——
      不需要转发、不需要 UC、不需要点任何坐标；
    - **真机**（adb 非 root，读不了 `/data/data`）：走「点转发 → 分享面板选 UC →
      轮询 UC 缓存 → pull」。真机必须走**「头像→资料页→划到底→附件简历」**那条路，
      因为只有那条打开出来的 PDF 右上角才有「转发」。

    成功后归档到 `resumes/` 键，并回写 `candidate.attachment_keys`
    （`receive_resume` 的唯一证据）与 `jc.minio_object_key`。

    返回 {"object_key", "name", "bytes"}；任一步失败抛 RuntimeError。
    """
    directory = _direct_attachment_dir()
    if directory:
        filename, data = _fetch_via_internal_dir(directory, name)
    else:
        filename, data = _fetch_via_uc()
    key = _archive_attachment(job_id, liepin_user_id, name, job_title, filename, data)
    return {"object_key": key, "name": filename, "bytes": len(data)}


@mcp.tool()
def list_backfill_candidates() -> dict:
    """列出需回填真实 PDF 的候选人：有附件截图（attachment_keys 含 .png）但尚无 PDF（minio_object_key 为空）。

    按 (姓名, 岗位) 去重，返回最新 liepin_user_id，供回填轮次逐个导航下载真 PDF。
    """
    with SessionLocal() as session:
        acct = _active_account_id(session)
        rows = session.execute(
            select(JobCandidate).where(
                JobCandidate.account_id == acct,
                JobCandidate.minio_object_key.is_(None),
            )
        ).scalars().all()
        best: dict[tuple[str, int], dict] = {}
        for jc in rows:
            cand = jc.candidate
            ak = cand.attachment_keys or []
            if not any(".png" in (k or "") for k in ak):
                continue
            key = (cand.name, jc.job_id)
            if key not in best or cand.liepin_user_id > best[key]["uid"]:
                job = session.get(Job, jc.job_id)
                best[key] = {"uid": cand.liepin_user_id, "title": job.title if job else ""}
        candidates = [
            {"name": name, "liepin_user_id": v["uid"], "job_id": job_id, "job_title": v["title"]}
            for (name, job_id), v in sorted(best.items(), key=lambda kv: kv[1]["uid"], reverse=True)
        ]
        return {"count": len(candidates), "candidates": candidates}


if __name__ == "__main__":
    mcp.run()
