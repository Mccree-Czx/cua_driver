"""安全联锁 + 状态落库。

## 为什么不复刻猎聘的 MCP 工具层

猎聘有 **32 个 MCP 工具**（`services/pipeline/tools_mcp_server.py`），那是为
「**Pi 是唯一决策者**」服务的——每一步点哪里都交给 LLM 现算。

BOSS 这边不一样：只读流程与打招呼**都是确定性的**（`agent_boss/flows.py`，
已在真机上验证过，不依赖 LLM）。再套一层 MCP + LLM，**只增成本与故障面**。

所以这里把猎聘 32 个工具里真正有价值的那部分——**安全联锁与状态落库**——
抽成**普通 Python 函数**，谁都能调（服务端直调、执行端经 HTTP 调）。

## 与猎聘工具的对照

| 猎聘 MCP 工具 | BOSS | 说明 |
|---|---|---|
| `gated_tap` | `may_greet()` + `quota_acquire()` | 触达的唯一闸门 |
| `risk_check` / `risk_set` / `risk_clear` | 同名 | 风控熔断 |
| `ensure_one_message` / `candidate_has_out_message` | `has_out_greet()` | 一人一消息 |
| `state_transition` | `apply_transition()` | 推进 + 落库 |
| `interaction_log` | 同名 | 动作流水 |
| `tasklog_add` | 同名 | 轮次成本 |
| `job_list_active` | 同名 | 非 active 的职位在这里就看不见 |
| `login_fill_phone` / `login_req_*` / `login_map_get` | —— | ❌ 不复刻：BOSS 登录是人过的关卡 |
| 全部带 `account_id` 的 | —— | ❌ 不复刻：BOSS 单账号 |
"""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from typing import Any

import redis
from hr_workbuddy import CandidateStatus, MinimalResume, ScreenRequest, ScreeningResult
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import keys, models
from app.config import get_settings
from app.screening_client import ScreeningClient
from app.state_machine import RECOMMENDED, StateEvent, TransitionContext, transition

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


def screen_candidate(session: Session, jc: models.JobCandidate) -> ScreeningResult | None:
    """给一个**还没评过分的 jc** 打分并落库（status + match_score + judge_reason）。

    ★ 只在 `status == new` 时执行——已评分 / 已推进的不再重复评（幂等）。
    ★ 服务不可用（网络 / HTTP 错）→ 返回 None、**不落库、不迁移**，调用方 fail-closed。

    评分数据来源：列表卡 `summary`（形如「10年以上｜本科｜面议」，按 ｜ best-effort 拆）
    + `tags`。详情页字段（degree/description 等）没接采集，有就优先用，没有退回拆出来的。
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
    apply_transition(session, jc, _VERDICT_EVENT[result.status], TransitionContext(source=RECOMMENDED))
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
