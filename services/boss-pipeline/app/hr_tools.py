"""安全联锁 + 状态落库。

BOSS 现在和猎聘一样走 **Pi（LLM）+ MCP 控制层**：`agent_boss/run_round.py` 拉起 Pi，
Pi 调 `services/boss-pipeline/tools_mcp_server.py`。Pi 只负责「看和决定」，
**所有真机写动作仍走这里的联锁**——本模块是这些联锁的**唯一实现**，MCP 层只是薄封装。

## 与猎聘的差异（BOSS 语义）

- **单账号**：无 `account_id` / 无 `Account` 模型，Redis 键都 `boss:` 前缀。
- **候选人主键 = `boss_key`**：实测退回只用 `name`（`api.py::_boss_key`）。
- **评分闸门**：牛人推荐流先评分、`screened_pass` 才允许打招呼（`may_greet`）。
- **附件简历**走确定性路径（HTTP `/api/ingest/attachment`），不进 MCP/不进 Pi。
- **登录三道人工关卡**（易盾滑块/短信/人脸）不进 Pi。

## 与猎聘工具的对照

| 猎聘 MCP 工具 | BOSS | 说明 |
|---|---|---|
| `gated_tap` | 同名（本模块新增） | 触达点击的风控闸门；限额由 `may_greet` 扣 |
| `risk_check` / `risk_set` / `risk_clear` | 同名 | 风控熔断 |
| `ensure_one_message` / `candidate_has_out_message` | `has_out_greet()` | 一人一消息 |
| `state_transition` | `apply_transition()` | 推进 + 落库 |
| `interaction_log` | 同名 | 动作流水 |
| `tasklog_add` | 同名 | 轮次成本 |
| `job_list_active` | 同名 | 非 active 的职位在这里就看不见 |
| `candidate_get_or_create` | 同名（本模块新增） | 建档（boss_key，空值不盖已有） |
| `close_stale_awaiting` / 补评分 | `daily_sweep()`（本模块新增） | 72h 关闭 + 补评分 |
| `login_fill_phone` / `login_req_*` / `login_map_get` | —— | ❌ 不复刻：BOSS 登录是人过的关卡 |
| 全部带 `account_id` 的 | —— | ❌ 不复刻：BOSS 单账号 |
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import redis
from hr_workbuddy import CandidateStatus, MinimalResume, ScreenRequest, ScreeningResult
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import keys, models
from app.config import get_settings
from app.screening_client import ScreeningClient
from app.state_machine import (
    RECOMMENDED,
    InvalidTransition,
    StateEvent,
    TransitionContext,
    transition,
)

# ——— Redis ———

_client: redis.Redis | None = None


def redis_client() -> redis.Redis:
    """懒建连接。★ 不在这里 ping——没装 Redis 的环境（跑单测）不该在 import 期就炸。"""
    global _client
    if _client is None:
        _client = redis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


# ——— 风控熔断（铁律 3）———

def risk_check() -> bool:
    """现在是不是处于风控熔断。**取不到就返回 True（fail-closed）。**

    ★ 这里必须 fail-closed：读不到状态时**宁可什么都不做**。
      反过来的话，Redis 一抖，系统就会在风控页上继续点。
    """
    try:
        return bool(redis_client().exists(keys.risk_paused()))
    except redis.RedisError:
        return True


def risk_set(reason: str) -> None:
    """置上熔断。**所有写动作立即停手**，直到有人查清原因并手动清掉。"""
    redis_client().set(keys.risk_paused(), reason)


def risk_clear() -> None:
    redis_client().delete(keys.risk_paused())


def risk_reason() -> str:
    try:
        return redis_client().get(keys.risk_paused()) or ""
    except redis.RedisError:
        return "(读不到 Redis)"


# ——— 每日配额 ———

def quota_used(day: date | None = None) -> int:
    try:
        return int(redis_client().get(keys.msg_touch(day)) or 0)
    except (redis.RedisError, ValueError):
        return get_settings().daily_greet_cap  # 读不到就当用满了（fail-closed）


def quota_acquire(n: int = 1) -> bool:
    """**先扣额度再动作**。扣得到返回 True，用满返回 False。

    ★ 顺序不能反（照猎聘「先落账后推进」的铁律）：先动作后记账的话，
      动作成功、记账失败，这一条就永远记不上，配额形同虚设。
    """
    cap = get_settings().daily_greet_cap
    client = redis_client()
    key = keys.msg_touch()
    try:
        used = client.incrby(key, n)
        if used == n:                      # 今天第一次，设个过期时间（两天，跨零点也够）
            client.expire(key, 2 * 24 * 3600)
        if used > cap:
            client.decrby(key, n)          # 超了就退回去，别把计数越滚越大
            return False
        return True
    except redis.RedisError:
        return False                       # fail-closed


# ——— 触达写动作（adb）———

def _resolve_adb() -> str:
    """定位 adb 可执行文件。照猎聘 `tools_mcp_server._resolve_adb` 同一套探测。

    ★ 不赌 PATH：计划任务/服务拉起的 PATH 未必带 platform-tools，
      按「部署时的实际位置」逐个探测，可用 `HR_ADB` 显式覆盖。
    """
    candidates = [
        os.environ.get("HR_ADB"),
        "/opt/homebrew/bin/adb",
        str(Path.home() / "Library/Android/sdk/platform-tools/adb"),
        os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
        r"C:\platform-tools\adb.exe",
        shutil.which("adb"),
    ]
    for exe in candidates:
        if exe and Path(exe).exists():
            return str(exe)
    return "adb"                            # 交给 PATH，失败时报错更直白


ADB = _resolve_adb()


def gated_tap(x: int, y: int, touch: bool = False) -> dict[str, Any]:
    """闸门 + 原子 tap：触达类点击的强制前置检查。

    `touch=True`（立即沟通/发送/索要等会触达候选人的点击）→ 先过风控熔断，
    熔断中**不执行 tap**。`touch=False`（浏览/进详情等读操作）直接执行。

    ★ BOSS 单账号、每日限额由 `may_greet`/`greet_check` 的 `quota_acquire` 管，
      这里不重复扣（与猎聘的 `counts_daily` 不同——BOSS 的配额语义在 greet_check）。
    """
    if touch and risk_check():
        return {"executed": False, "reason": f"risk_paused（{risk_reason()}）"}
    try:
        result = subprocess.run(
            [ADB, "shell", "input", "tap", str(int(x)), str(int(y))],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"executed": False, "reason": f"adb 调用失败：{type(exc).__name__}"}
    if result.returncode != 0:
        return {"executed": False, "reason": (result.stderr or "").strip()[:200]}
    return {"executed": True, "reason": None, "touch_checked": touch}


# ——— 职位 ———

def job_list_active(session: Session) -> list[models.Job]:
    """**系统敢不敢用**的职位。

    ★ 三个条件缺一不可，而且**联锁焊在这里、不在提示词里**：
      1. `status == active` —— 有人启用过它
      2. `state == 开放中`   —— BOSS 那边确实在招
      3. `jd_complete`       —— **JD 读全了**

    第 3 条是照猎聘 `JOB_STATUS_DRAFT` 的教训：**一个没有 JD 的职位，
    拿去给真实候选人发消息是纯粹的浪费**，而且会让打分/匹配全都落空。
    """
    return list(session.scalars(
        select(models.Job)
        .where(models.Job.status == models.JOB_STATUS_ACTIVE)
        .where(models.Job.state == "开放中")
        .where(models.Job.jd_complete.is_(True))
        .order_by(models.Job.title)
    ))


# ——— 建档 ———

def candidate_get_or_create(
    session: Session,
    *,
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
) -> dict[str, Any]:
    """取或建候选人，并把详情页字段叠上去。

    ★ 主键 = `boss_key or name`（BOSS 实测退回只用 `name`——列表卡与详情页的
      公司串会不一样，别把公司串拼进主键）。Pi 建档时**留 `boss_key` 为空**即可。
    ★ **空值不盖已有**：列表页推上来的缺详情字段（degree/description/…），
      不能用空值冲掉详情页已采到的值（照 `api.ingest_candidates` 同一套规则）。
    ★ 给了 `job_title` 且职位存在 → 顺带建 jc，返回里带 `jc_id`。
    """
    if not name.strip():
        return {"error": "name 不能为空（BOSS 主键退回只用 name）"}
    key = boss_key.strip() or name.strip()
    cand = session.scalar(
        select(models.Candidate).where(models.Candidate.boss_key == key)
    )
    created = cand is None
    if cand is None:
        cand = models.Candidate(boss_key=key, name=name.strip())
        session.add(cand)
        session.flush()

    for field, value in (
        ("name", name.strip()),
        ("job_and_company", job_and_company),
        ("work_status", work_status),
        ("work_year", work_year),
        ("degree", degree),
        ("age", age),
        ("description", description),
        ("expect_job_city", expect_job_city),
        ("expect_salary", expect_salary),
        ("expect_industry", expect_industry),
        ("summary", summary),
        ("tags", tags),
        ("company_history", company_history),
        ("active", active),
    ):
        if value in (None, "", [], {}) and getattr(cand, field, None):
            continue
        setattr(cand, field, value)

    out: dict[str, Any] = {
        "candidate_id": cand.id, "boss_key": cand.boss_key, "created": created,
    }
    if job_title:
        job = session.scalar(select(models.Job).where(models.Job.title == job_title))
        if job is not None:
            jc = jc_get_or_create(session, job.id, cand.id)
            out["jc_id"] = jc.id
            out["job_id"] = job.id
        else:
            out["warning"] = f"职位 {job_title!r} 不存在——候选人已建档但没建 jc（先补职位）"
    session.commit()
    return out


# ——— 状态落库 ———

def jc_get_or_create(session: Session, job_id: int, candidate_id: int) -> models.JobCandidate:
    """取或建「某人 × 某职位」。唯一键撞了就取回来。"""
    row = session.scalar(
        select(models.JobCandidate)
        .where(models.JobCandidate.job_id == job_id)
        .where(models.JobCandidate.candidate_id == candidate_id)
    )
    if row is None:
        row = models.JobCandidate(job_id=job_id, candidate_id=candidate_id)
        session.add(row)
        session.flush()
    return row


def apply_transition(
    session: Session,
    jc: models.JobCandidate,
    event: StateEvent,
    ctx: TransitionContext | None = None,
) -> models.JobCandidate:
    """推进状态并落库。

    `state_machine.transition()` 是**纯函数**（不改传入对象、不落库），
    这里负责把结果写回**持久化的那一行**，并顺带打上时间锚点。
    """
    updated = transition(jc, event, ctx)       # 非法迁移在这里抛
    jc.status = updated.status
    now = datetime.now()
    if event is StateEvent.GREET:
        jc.last_touch_at = now
    elif event is StateEvent.REQUEST_RESUME:
        jc.resume_requested_at = now          # 72h 无人回应的锚点
    elif event is StateEvent.RECEIVE_RESUME:
        jc.resume_downloaded_at = now
    session.add(jc)
    session.commit()
    return jc


def interaction_log(
    session: Session,
    jc: models.JobCandidate,
    direction: str,
    msg_type: str,
    content: str = "",
) -> models.Interaction:
    """记一条**系统动作流水**。

    ★ 只记「系统对候选人做了什么」。**对方说的话不要往这里写**——
      那是 `messages` 的事。混进来会污染 `has_out_greet()` 的联锁判断。
    """
    if msg_type not in models.INTERACTION_MSG_TYPES:
        raise ValueError(f"msg_type 必须是 {models.INTERACTION_MSG_TYPES} 之一，收到 {msg_type!r}")
    row = models.Interaction(
        job_candidate_id=jc.id, direction=direction, msg_type=msg_type, content=content
    )
    session.add(row)
    session.commit()
    return row


# ——— 评分（复用猎聘 screening 服务）———


_VERDICT_EVENT = {
    CandidateStatus.SCREENED_PASS: StateEvent.SCREEN_PASS,
    CandidateStatus.REJECTED_HARD: StateEvent.REJECT_HARD,
    CandidateStatus.REJECTED_LLM: StateEvent.REJECT_LLM,
}


@lru_cache
def _screening() -> ScreeningClient:
    return ScreeningClient()


def screen_candidate(
    session: Session, jc: models.JobCandidate, source: str = RECOMMENDED
) -> ScreeningResult | None:
    """给一个**还没评过分的 jc** 打分并落库（status + match_score + judge_reason）。

    ★ 只在 `status == new` 时执行——已评分 / 已推进的不再重复评（幂等）。
    ★ 服务不可用（网络 / HTTP 错）→ 返回 None、**不落库、不迁移**，调用方 fail-closed。

    评分数据来源：列表卡 `summary`（形如「10年以上｜本科｜面议」，按 ｜ best-effort 拆）
    + `tags`。详情页字段（degree/description 等）没接采集，有就优先用，没有退回拆出来的。

    ★ `source`：路径判定（recommended|inbound）。推荐流打分默认 recommended；
      inbound 的「新招呼」求简历前打分传 `source=INBOUND`。
    """
    if jc.status != CandidateStatus.NEW.value:
        return None

    cand = jc.candidate
    job = jc.job
    parts = [p.strip() for p in (cand.summary or "").split("｜")]
    resume = MinimalResume(
        name=cand.name,
        liepin_user_id=cand.boss_key or f"boss:{cand.id}",
        education=cand.degree or (parts[1] if len(parts) > 1 else ""),
        years_of_experience=cand.work_year or (parts[0] if len(parts) > 0 else ""),
        city=cand.expect_job_city or "",
        salary=cand.expect_salary or (parts[2] if len(parts) > 2 else ""),
        experience_summary=cand.description or cand.summary or "",
    )
    prefs = job.scoring_prefs if isinstance(job.scoring_prefs, dict) else {}
    request = ScreenRequest(
        job_id=job.id,
        resume=resume,
        jd_text=job.jd_text or "",
        hard_rules={},
        min_stars=prefs.get("min_stars", 3),
        scoring_prefs=prefs,
        llm_scoring=True,
    )
    try:
        result = _screening().screen(request)
    except Exception:  # noqa: BLE001 —— 网络 / HTTP / 解析错都算「服务不可用」
        return None

    jc.match_score = result.score
    jc.judge_reason = result.judge_reason
    apply_transition(session, jc, _VERDICT_EVENT[result.status], TransitionContext(source=source))
    return result


# ——— 一人一消息 ———

def has_out_greet(session: Session, jc: models.JobCandidate) -> bool:
    """这个人**在我们这边**已经被打过招呼了吗。

    ★ 这是联锁的第一道（系统动作账本）。第二道在采数端：
      `agent_boss/.greeted.json`（本地账本）+ `greet()` 里的
      「读会话确认已有 out 消息」。

    三道都要，因为它们防的是不同的事：
    - 这一道防「换机器 / 重装后重复发」
    - 本地账本防「同一次运行里重复」
    - 读会话防「别人（或人工）已经打过招呼了」
    """
    return bool(session.scalar(
        select(func.count())
        .select_from(models.Interaction)
        .where(models.Interaction.job_candidate_id == jc.id)
        .where(models.Interaction.direction == "out")
        .where(models.Interaction.msg_type == "greet")
    ))


def may_greet(session: Session, jc: models.JobCandidate) -> tuple[bool, str]:
    """打招呼的总闸门。返回 `(能不能, 原因)`。

    ★ 顺序是刻意的：**先查熔断，再查重复，再评分，最后才扣配额**。
      扣配额是有副作用的（计数 +1），不能在一个注定要拒的场景里白扣；
      评分也有成本（一次 LLM），所以排在「已经打过招呼」之后、只在 `new` 上跑。
    """
    if risk_check():
        return False, f"风控熔断中（{risk_reason()}）——停手，等人处理"
    if has_out_greet(session, jc):
        return False, "这个人已经打过招呼了（系统账本里有）"
    if jc.status == CandidateStatus.NEW.value:
        if screen_candidate(session, jc) is None:
            return False, "评分服务不可用——停手，等人处理"
    if jc.status != CandidateStatus.SCREENED_PASS.value:
        return False, f"评分未通过（{jc.status}：{jc.judge_reason}）"
    if not quota_acquire():
        return False, "今天的打招呼配额用完了"
    return True, ""


# ——— 轮次成本 ———

def tasklog_add(
    session: Session,
    *,
    task_id: str,
    round_name: str,
    outcome: str,
    attempt: int = 0,
    tokens: int = 0,
    cost: float = 0.0,
    duration: float = 0.0,
    note: str = "",
) -> models.TaskLog:
    row = models.TaskLog(
        task_id=task_id, round_name=round_name, outcome=outcome, attempt=attempt,
        tokens=tokens, cost=cost, duration=duration, note=note,
    )
    session.add(row)
    session.commit()
    return row


def counts(session: Session) -> dict[str, Any]:
    """排障用的一把数。"""
    def n(model: Any, *where: Any) -> int:
        stmt = select(func.count()).select_from(model)
        for clause in where:
            stmt = stmt.where(clause)
        return session.scalar(stmt) or 0

    return {
        "jobs_active": len(job_list_active(session)),
        "jc": n(models.JobCandidate),
        "quota_used": quota_used(),
        "quota_cap": get_settings().daily_greet_cap,
        "risk_paused": risk_check(),
    }


# ——— 每日收尾 ———

def daily_sweep(session: Session) -> dict[str, int]:
    """日常收尾：72h 无人回应的 awaiting_resume 关闭 + 给没分的 new 补评分。

    照 `api.ingest_daily_sweep`（api.py:510-550）同一套逻辑，抽出来给 MCP 的
    `daily_round` 用。幂等：关闭只挑超 72h 的 awaiting_resume；补评分只挑没分的 new。
    """
    cutoff = datetime.now() - timedelta(hours=72)
    stale = list(session.scalars(
        select(models.JobCandidate)
        .where(models.JobCandidate.status == "awaiting_resume")
        .where(models.JobCandidate.resume_requested_at.is_not(None))
        .where(models.JobCandidate.resume_requested_at < cutoff)
    ))
    closed = 0
    for jc in stale:
        try:
            apply_transition(session, jc, StateEvent.NO_RESPONSE)
            apply_transition(session, jc, StateEvent.CLOSE)
            closed += 1
        except InvalidTransition:
            pass

    scored = 0
    pending = list(session.scalars(
        select(models.JobCandidate)
        .join(models.Candidate, models.Candidate.id == models.JobCandidate.candidate_id)
        .where(models.JobCandidate.status == "new")
        .where(models.JobCandidate.match_score.is_(None))
        .where((models.Candidate.summary != "") | models.Candidate.description.is_not(None))
        .limit(20)
    ))
    for jc in pending:
        if screen_candidate(session, jc) is not None:
            scored += 1
    return {"closed": closed, "scored": scored}
