"""HR 面 API（M3 最小可用 + M4 可观测最小；2026-10-07）：/api/hr/*——本机使用无鉴权。

- 只读：overview / candidates / 两级详情（预签名 ≤15min）/ daily / manual-queue / alerts
- 动作：review 复核推翻（OVERRIDE + ReviewOverride 流水）、rerun 手动重跑（任务派发）、
  manual-queue ack 注记、PATCH jobs（阈值回流最小形态）
- 依赖复用：get_session / get_store / get_queue / get_login_state；任务派发走
  TaskQueue.enqueue（派发登记 + arq 入队，与内部面同源）。
"""

from __future__ import annotations

import string
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import redis as redis_lib
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.crypto import CredentialCryptoError, encrypt_secret
from app.deps import (
    get_active_account,
    get_login_request,
    get_login_state,
    get_queue,
    get_session,
    get_store,
)
from app.job_sync import MIN_JD_CHARS
from app.keys import risk_pause_key
from app.state_machine import InvalidTransition, StateEvent, TransitionContext, transition
from hr_workbuddy import CandidateStatus

router = APIRouter(prefix="/api/hr", tags=["hr"])

PRESIGN_MINUTES = 15  # spec §6：预签名 URL ≤15 分钟
DAILY_MAX_DAYS = 90
MANUAL_QUEUE_MAX = 200
REVIEW_FROM = {CandidateStatus.RESUME_RECEIVED.value, CandidateStatus.HR_REVIEWED.value}

# 能敲进手机的密码字符集。实测依据见 _encrypt_password 的 docstring——
# 唯一被排除的可打印 ASCII 是双引号。
TYPEABLE_PASSWORD_CHARS = frozenset(
    string.ascii_letters + string.digits + string.punctuation.replace('"', "") + " "
)


def _today_start() -> datetime:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def _score_to_stars(score: int) -> int:
    """1-5 星直取；旧 0-100 数据映射折算（<40→1,40-59→2,60-79→3,80-89→4,≥90→5）。"""
    if 1 <= score <= 5:
        return score
    if score < 40:
        return 1
    if score < 60:
        return 2
    if score < 80:
        return 3
    if score < 90:
        return 4
    return 5


def _presign(store: Any, key: str | None) -> str | None:
    """对象键 → 预签名 URL（≤15min）；键缺失或对象异常返回 None（不阻塞详情）。"""
    if not key:
        return None
    try:
        return store.presigned_get_url(
            store.bucket, key, expires=timedelta(minutes=PRESIGN_MINUTES)
        )
    except Exception:  # noqa: BLE001  # 预签名失败不阻塞详情
        return None


# —— 账号（多账号支持，2026-10-14）——


def _mask_phone(phone: str | None) -> str | None:
    """手机号打码。前端只需能区分「是哪个号」，不需要完整号码。"""
    if not phone:
        return None
    if len(phone) < 7:
        return "***"
    return f"{phone[:3]}****{phone[-4:]}"


def _account_out(acct: models.Account) -> dict[str, Any]:
    """账号的对外形状。**绝不包含 password_enc**——密文同样不外传。"""
    return {
        "id": acct.id,
        "name": acct.name,
        "is_active": acct.is_active,
        "phone_masked": _mask_phone(acct.phone),
        "has_password": bool(acct.password_enc),
    }


def _clean_phone(raw: str) -> str:
    """规范化手机号并做基本校验。

    App 的输入框只收数字，系统还要拿这个号跟手机上读到的号比对，所以在这里
    挡掉明显的手误（字母、位数不对）比事后排查便宜得多。不强制 11 位与前缀 1：
    那是大陆号段的形状，不该成为拒绝一个真实账号的理由。
    """
    phone = raw.strip().replace(" ", "").replace("-", "")
    if not phone:
        raise HTTPException(status_code=400, detail="手机号不能为空")
    if not phone.isdigit() or not (6 <= len(phone) <= 20):
        raise HTTPException(status_code=400, detail="手机号须为 6-20 位数字")
    return phone


def _encrypt_password(raw: str) -> str:
    """校验并加密密码。

    **字符集限制是实测出来的物理限制，不是稳妥起见**：手机是 Android 10 + 华为，
    `adb shell input text` 是唯一可用的输入通道（用户选择不装 ADBKeyboard）。逐个
    字符实测（2026-10-14）的结论是——`string.punctuation` 里除双引号外的 28 个符号
    都能原样敲进去，**双引号无论是单引号包裹、双引号包裹还是反斜杠转义，都是
    返回码 0、无报错、静默丢弃**；中文等非 ASCII 完全不行；空格可以，但要靠 `%s`
    转义（所以密码里出现字面量 `%s` 会被当成空格）。

    不合规的密码存进库也永远敲不进手机，换号会在最后一步失败——所以宁可在创建时
    就拒绝，并把原因直接写给用户看。
    """
    if not raw:
        raise HTTPException(status_code=400, detail="密码不能为空")
    if len(raw) > 128:
        raise HTTPException(status_code=400, detail="密码过长（上限 128 字符）")
    bad = sorted({c for c in raw if c not in TYPEABLE_PASSWORD_CHARS})
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"密码含系统敲不进手机的字符：{''.join(bad)}"
            "（双引号与中文都不行）。换号时这些字符会导致登录失败，请换一个密码。",
        )
    if "%s" in raw:
        raise HTTPException(
            status_code=400, detail="密码不能包含 %s——输入到手机时它会被当成空格"
        )
    try:
        return encrypt_secret(raw)
    except CredentialCryptoError as exc:
        # 多半是 ACCOUNT_SECRET_KEY 没配。给可读的 503，别让 Fernet 的异常冒成 500。
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/accounts")
def hr_accounts(session: Session = Depends(get_session)) -> dict[str, Any]:
    """账号列表 + 哪个生效。

    唯一不依赖 get_active_account 的端点——它正是前端用来发现「没有生效账号」
    并提示用户去选的地方，所以自己不能先被 503 挡住。
    """
    rows = session.execute(select(models.Account).order_by(models.Account.id)).scalars().all()
    active = next((a.id for a in rows if a.is_active), None)
    return {"active_id": active, "items": [_account_out(a) for a in rows]}


class AccountCreate(BaseModel):
    name: str
    phone: str = ""
    password: str = ""


@router.post("/accounts")
def hr_account_create(
    body: AccountCreate, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """新建账号。不自动设为生效——切号是人工在手机上做完之后才点的动作。

    手机号与密码都必填：这个端点承担的是「把账号登记进系统」，而账号的身份
    就是手机号。留空的账号系统无法核验，也无从在换号失败时回滚。
    """
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="账号名不能为空")
    acct = models.Account(
        name=name,
        phone=_clean_phone(body.phone),
        password_enc=_encrypt_password(body.password),
        credential_updated_at=datetime.now(),
    )
    session.add(acct)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="该手机号已登记过账号") from exc
    session.refresh(acct)
    return _account_out(acct)


class AccountPatch(BaseModel):
    name: str | None = None
    phone: str | None = None
    password: str | None = None


@router.patch("/accounts/{account_id}")
def hr_account_patch(
    account_id: int, body: AccountPatch, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """补录 / 修改账号凭据。只更新显式传入的字段；password 传空串表示清除。

    存在的理由：存量那个「默认账号」（id=1）没有手机号，而 1845 条候选人挂在
    它名下——不能新建一个号把数据搬过去，只能就地补录。
    """
    acct = session.get(models.Account, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")
    data = body.model_dump(exclude_unset=True)
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="账号名不能为空")
        acct.name = name
    if "phone" in data:
        acct.phone = _clean_phone(data["phone"] or "")
    if "password" in data:
        raw = data["password"] or ""
        acct.password_enc = _encrypt_password(raw) if raw else None
    acct.credential_updated_at = datetime.now()
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="该手机号已登记过账号") from exc
    session.refresh(acct)
    return _account_out(acct)


# ★ `POST /accounts/{id}/activate` 已于 2026-10-15 **删除**。
#
# 它只翻库里的 `is_active`，**不碰手机**（原来的 docstring 自己写着这句）。
# 而系统里所有其他地方都假设「库里的生效账号 == 手机上登的号」——
# 这个端点一被调用，两者立刻不一致，下一轮 Pi 的 `job_list_active()`、
# 候选人与配额就全按新号算，而手机还是旧号。前端下拉框的 onChange 直接调它，
# 也就是说**一次误触就能制造这种不一致**，且没有任何确认。
#
# 换号现在只有一条路：`POST /accounts/{id}/switch`（或 `/login`），
# 它们会真的去手机上退出旧号、登新号，成功之后才改 `is_active`。


# —— 登录（批次 2：系统代操作手机）——
#
# 跨机器：这里只写 Redis，Mac 上的 login_watch.py 轮询到就拉起 Pi 操作手机。
# CT104 连不到 Mac（Mac 在 NAT 后），Redis 是两边唯一的会面点。
# 状态机本体见 app/login_request.py。


def _login_payload(
    req: dict[str, Any] | None,
    account: models.Account | None = None,
    from_account: models.Account | None = None,
) -> dict[str, Any]:
    """给前端的登录进度。**不含验证码本身**——前端刚提交过，回显没有意义。"""
    if req is None:
        return {"phase": "idle"}
    return {
        "request_id": req.get("request_id"),
        "account_id": req.get("account_id"),
        "account_name": account.name if account else None,
        "phone_masked": _mask_phone(account.phone) if account else None,
        "kind": req.get("kind"),
        # 切换失败时前端要靠它给出「登回 <旧号名>」入口——手机上没号了，
        # 光说「失败」用户不知道下一步该干什么。
        "from_account_id": req.get("from_account_id") or None,
        "from_account_name": from_account.name if from_account else None,
        "method": req.get("method"),
        "phase": req.get("phase"),
        "resend_seq": req.get("resend_seq", 0),
        "error": req.get("error") or None,
        "notice": req.get("notice") or None,
        "expires_at": req.get("expires_at"),
    }


def _accounts_in_request(
    session: Session, req: dict[str, Any]
) -> tuple[models.Account | None, models.Account | None]:
    target = session.get(models.Account, req["account_id"])
    source = (
        session.get(models.Account, req["from_account_id"])
        if req.get("from_account_id")
        else None
    )
    return target, source


@router.get("/accounts/login-request")
def hr_login_request_get(
    session: Session = Depends(get_session),
    store: Any = Depends(get_login_request),
) -> dict[str, Any]:
    """当前登录请求 + 进度。前端轮询这个。"""
    req = store.get()
    if req is None:
        return {"phase": "idle"}
    target, source = _accounts_in_request(session, req)
    return _login_payload(req, target, source)


class LoginStart(BaseModel):
    """force=True 时覆盖在途请求（比如上一次卡住了，用户主动重启）。"""

    force: bool = False
    method: Literal["password", "code"] = "code"


def _reject_if_in_flight(store: Any, force: bool) -> None:
    in_flight = store.active()
    if in_flight is not None and not force:
        raise HTTPException(
            status_code=409,
            detail=f"已有一个登录在进行中（账号 {in_flight['account_id']}，"
            f"阶段 {in_flight['phase']}）——一部手机同一时刻只能登一个号",
        )


def _reject_disabled_method(method: str, account: models.Account) -> None:
    if method == "password":
        # 2026-10-15 实测：猎聘的「账号密码登录」填完点下去会弹滑块验证码，走不通。
        # 这里给明确原因，而不是让前端猜为什么按钮点了没反应。
        raise HTTPException(
            status_code=400,
            detail="猎聘已不支持账号密码登录（点登录会弹滑块验证，实测走不通），请改用验证码登录",
        )
    if not account.phone:
        raise HTTPException(
            status_code=400, detail="该账号没有登记手机号，无法登录——请先在账号管理里补录"
        )


@router.post("/accounts/{account_id}/login")
def hr_login_start(
    account_id: int,
    body: LoginStart | None = None,
    session: Session = Depends(get_session),
    store: Any = Depends(get_login_request),
) -> dict[str, Any]:
    """登录：手机**已经在登录页**时用这个。

    前置条件由前端确认（手机上得先退出旧号），这里只校验数据面。
    """
    opts = body or LoginStart()
    account = session.get(models.Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")
    _reject_disabled_method(opts.method, account)
    _reject_if_in_flight(store, opts.force)
    req = store.start(account_id, opts.method, kind="login")
    return _login_payload(req, account)


@router.post("/accounts/{account_id}/switch")
def hr_account_switch(
    account_id: int,
    body: LoginStart | None = None,
    session: Session = Depends(get_session),
    store: Any = Depends(get_login_request),
) -> dict[str, Any]:
    """一键切换：**先退出手机上登着的号，再登录目标号**。

    这是**不可逆**动作——退出成功而登录失败，手机上就没有账号了。所以：
    - 前端必须先强制确认
    - 失败态必须给出「登回 <旧号名>」入口（payload 里的 from_account_*）
    """
    opts = body or LoginStart()
    target = session.get(models.Account, account_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")
    if target.is_active:
        # 切到自己会白退出一次、还白费一条短信——这条先判，与目标有没有手机号无关
        raise HTTPException(
            status_code=400,
            detail=f"「{target.name}」已经是当前生效账号了，不需要切换",
        )
    _reject_disabled_method(opts.method, target)
    current = (
        session.execute(select(models.Account).where(models.Account.is_active.is_(True)))
        .scalars()
        .first()
    )
    if current is None:
        raise HTTPException(
            status_code=400,
            detail="当前没有生效账号，没有可退出的旧号——请直接用「登录」",
        )
    _reject_if_in_flight(store, opts.force)
    req = store.start(
        account_id, opts.method, kind="switch", from_account_id=current.id
    )
    return _login_payload(req, target, current)


class LoginCode(BaseModel):
    code: str


@router.post("/accounts/login-request/code")
def hr_login_submit_code(
    body: LoginCode,
    session: Session = Depends(get_session),
    store: Any = Depends(get_login_request),
) -> dict[str, Any]:
    """提交人工从自己手机上读到的验证码。"""
    code = body.code.strip().replace(" ", "")
    if not code.isdigit() or not (4 <= len(code) <= 8):
        raise HTTPException(status_code=400, detail="验证码应为 4-8 位数字")
    req = store.active()
    if req is None:
        raise HTTPException(status_code=409, detail="当前没有进行中的登录")
    store.submit_code(code)
    account = session.get(models.Account, req["account_id"])
    return _login_payload(store.get(), account)


@router.post("/accounts/login-request/resend")
def hr_login_resend(
    session: Session = Depends(get_session),
    store: Any = Depends(get_login_request),
) -> dict[str, Any]:
    """重发验证码。**只是记一个请求**，真正去点「重发」由下一轮 Pi 执行——
    禁止 Pi 自己循环点获取验证码，那是风控画像里最显眼的动作之一。"""
    req = store.active()
    if req is None:
        raise HTTPException(status_code=409, detail="当前没有进行中的登录")
    store.bump_resend()
    account = session.get(models.Account, req["account_id"])
    return _login_payload(store.get(), account)


@router.post("/accounts/login-request/cancel")
def hr_login_cancel(store: Any = Depends(get_login_request)) -> dict[str, Any]:
    """取消：清掉请求，Mac 侧的轮询器随之不再拉起轮次。"""
    store.clear()
    return {"phase": "idle"}


# —— 只读端点 ——


@router.get("/overview")
def hr_overview(
    job_id: int | None = None,
    session: Session = Depends(get_session),
    acct: models.Account = Depends(get_active_account),
) -> dict[str, Any]:
    """漏斗概览：状态计数 / 评分分布 / 今日触达与回传。仅当前账号。"""
    status_stmt = (
        select(models.JobCandidate.status, func.count())
        .where(models.JobCandidate.account_id == acct.id)
        .group_by(models.JobCandidate.status)
    )
    score_stmt = select(models.JobCandidate.match_score).where(
        models.JobCandidate.account_id == acct.id
    )
    if job_id is not None:
        status_stmt = status_stmt.where(models.JobCandidate.job_id == job_id)
        score_stmt = score_stmt.where(models.JobCandidate.job_id == job_id)
    status_counts = {s: n for s, n in session.execute(status_stmt).all()}
    scores = [s for (s,) in session.execute(score_stmt).all() if s is not None]
    buckets = {"1星": 0, "2星": 0, "3星": 0, "4星": 0, "5星": 0, "未评分": 0}
    for score in scores:
        buckets[f"{_score_to_stars(score)}星"] += 1
    buckets["未评分"] = sum(status_counts.values()) - len(scores)
    start = _today_start()
    touches_stmt = (
        select(func.count())
        .select_from(models.Interaction)
        .join(models.JobCandidate, models.JobCandidate.id == models.Interaction.job_candidate_id)
        .where(
            models.JobCandidate.account_id == acct.id,
            models.Interaction.direction == "out",
            models.Interaction.sent_at >= start,
        )
    )
    received_stmt = (
        select(func.count())
        .select_from(models.Interaction)
        .join(models.JobCandidate, models.JobCandidate.id == models.Interaction.job_candidate_id)
        .where(
            models.JobCandidate.account_id == acct.id,
            models.Interaction.direction == "in",
            models.Interaction.msg_type == "attachment",
            models.Interaction.sent_at >= start,
        )
    )
    if job_id is not None:
        touches_stmt = touches_stmt.where(models.JobCandidate.job_id == job_id)
        received_stmt = received_stmt.where(models.JobCandidate.job_id == job_id)
    touches = session.scalar(touches_stmt)
    received = session.scalar(received_stmt)
    # manual：task_logs 无 job 关联，按账号过滤（轮次落账时带账号）
    manual = session.scalar(
        select(func.count())
        .select_from(models.TaskLog)
        .where(
            models.TaskLog.account_id == acct.id,
            models.TaskLog.outcome == "failed_needs_manual",
            models.TaskLog.created_at >= start,
        )
    )
    job = (
        session.get(models.Job, job_id)
        if job_id is not None
        else None
    )
    # 跨账号的 job_id 一律按不存在处理，不泄露其它账号的职位信息
    if job is not None and job.account_id != acct.id:
        job = None
    return {
        "job": {"id": job.id, "title": job.title, "llm_threshold": job.llm_threshold, "scoring_prefs": job.scoring_prefs} if job else None,
        "status_counts": status_counts,
        "score_buckets": buckets,
        "today": {
            "touches_out": touches or 0,
            "received": received or 0,
            "received_target": get_settings().daily_resume_target,
            "manual": manual or 0,
        },
    }


@router.get("/candidates")
def hr_candidates(
    job_id: int | None = None,
    status: str | None = None,
    q: str | None = None,
    # ★ 量纲是**星级 1–5**，不是 0–100。`match_score` 存的就是 screening 给的
    #   `llm_score`，而 screening 只做 `llm_score >= min_stars` 判断（星级量纲）。
    #   这里原先写 `ge=0, le=100`，是第一代 0–100 量纲的残留——传 50 能通过校验，
    #   却永远匹配不到任何人。契约写得比实际宽，是会让调用方白调试的那种错。
    min_score: int | None = Query(None, ge=1, le=5, description="仅看达标分档（星级 >= 此值，1-5）"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    acct: models.Account = Depends(get_active_account),
) -> dict[str, Any]:
    """候选人列表（筛选 + 分页；新到在前）。min_score：达标分档视图（回收目标口径）。

    仅当前账号。列表与计数两条语句共用同一个 account 过滤——漏掉计数那条
    会让分页总数串到别的账号。
    """
    stmt = (
        select(models.JobCandidate, models.Candidate, models.Job)
        .join(models.Candidate, models.Candidate.id == models.JobCandidate.candidate_id)
        .join(models.Job, models.Job.id == models.JobCandidate.job_id)
        .where(models.JobCandidate.account_id == acct.id)
        .order_by(models.JobCandidate.id.desc())
    )
    count_stmt = (
        select(func.count())
        .select_from(models.JobCandidate)
        .join(models.Candidate, models.Candidate.id == models.JobCandidate.candidate_id)
        .where(models.JobCandidate.account_id == acct.id)
    )
    if job_id is not None:
        stmt = stmt.where(models.JobCandidate.job_id == job_id)
        count_stmt = count_stmt.where(models.JobCandidate.job_id == job_id)
    if status:
        stmt = stmt.where(models.JobCandidate.status == status)
        count_stmt = count_stmt.where(models.JobCandidate.status == status)
    if q:
        like = f"%{q}%"
        condition = models.Candidate.name.like(like) | models.Candidate.liepin_user_id.like(like)
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    if min_score is not None:
        stmt = stmt.where(models.JobCandidate.match_score >= min_score)
        count_stmt = count_stmt.where(models.JobCandidate.match_score >= min_score)
    total = session.scalar(count_stmt) or 0
    rows = session.execute(stmt.limit(limit).offset(offset)).all()
    jc_ids = [jc.id for jc, _, _ in rows]
    latest_map = (
        dict(
            session.execute(
                select(models.Interaction.job_candidate_id, func.max(models.Interaction.sent_at))
                .where(models.Interaction.job_candidate_id.in_(jc_ids))
                .group_by(models.Interaction.job_candidate_id)
            ).all()
        )
        if jc_ids
        else {}
    )
    items = [
        {
            "jc_id": jc.id,
            "job_id": jc.job_id,
            "job_title": job.title,
            "name": candidate.name,
            "liepin_user_id": candidate.liepin_user_id,
            "source": candidate.source,
            "status": jc.status,
            "match_score": jc.match_score,
            "judge_reason": jc.judge_reason,
            "has_snapshot": bool(candidate.snapshot_object_key),
            "has_pdf": bool(jc.minio_object_key),
            "last_touch_at": latest_map[jc.id].isoformat() if jc.id in latest_map else None,
        }
        for jc, candidate, job in rows
    ]
    return {"total": total, "items": items}


@router.get("/candidates/{jc_id}")
def hr_candidate_detail(
    jc_id: int,
    session: Session = Depends(get_session),
    store: Any = Depends(get_store),
    acct: models.Account = Depends(get_active_account),
) -> dict[str, Any]:
    """两级详情：初筛（截图预签名 + 7 字段 + 评分理由）+ 二筛（PDF 预签名）+ 互动时间线。

    其它账号的 jc_id 一律按 404 处理（不区分「不存在」与「不属于你」，
    避免通过状态码探测别的账号有哪些 jc）。
    """
    jc = session.get(models.JobCandidate, jc_id)
    if jc is None or jc.account_id != acct.id:
        raise HTTPException(status_code=404, detail=f"job_candidate {jc_id} 不存在")
    candidate = session.get(models.Candidate, jc.candidate_id)
    job = session.get(models.Job, jc.job_id)
    interactions = (
        session.execute(
            select(models.Interaction)
            .where(models.Interaction.job_candidate_id == jc.id)
            .order_by(models.Interaction.id)
        )
        .scalars()
        .all()
    )
    snapshot_urls: list[str] = []
    if candidate is not None and candidate.liepin_user_id:
        try:
            objs = store.client.list_objects(
                store.bucket, prefix=f"snapshots/{candidate.liepin_user_id}/", recursive=True
            )
            snap_keys = sorted(o.object_name for o in objs)
            snapshot_urls = [_presign(store, k) for k in snap_keys]
        except Exception:  # noqa: BLE001  # 列出失败不阻塞详情
            snapshot_urls = []
    return {
        "job": {"id": job.id, "title": job.title} if job else None,
        "jc": {
            "id": jc.id,
            "status": jc.status,
            "match_score": jc.match_score,
            "judge_reason": jc.judge_reason,
            "resume_requested_at": jc.resume_requested_at.isoformat() if jc.resume_requested_at else None,
            "resume_downloaded_at": jc.resume_downloaded_at.isoformat() if jc.resume_downloaded_at else None,
            "last_touch_at": jc.last_touch_at.isoformat() if jc.last_touch_at else None,
        },
        "candidate": {
            "name": candidate.name if candidate else None,
            "liepin_user_id": candidate.liepin_user_id if candidate else None,
            "online_resume_minimal": candidate.online_resume_minimal if candidate else None,
        },
        "snapshot_url": _presign(store, candidate.snapshot_object_key if candidate else None),
        "snapshot_urls": snapshot_urls,
        "resume_url": _presign(store, jc.minio_object_key),
        "resume_html_url": _presign(store, candidate.resume_html_key if candidate else None),
        "interactions": [
            {
                "direction": row.direction,
                "msg_type": row.msg_type,
                "content": row.content,
                "sent_at": row.sent_at.isoformat() if row.sent_at else None,
            }
            for row in interactions
        ],
    }


@router.get("/daily")
def hr_daily(
    days: int = Query(14, ge=1, le=DAILY_MAX_DAYS),
    session: Session = Depends(get_session),
    acct: models.Account = Depends(get_active_account),
) -> dict[str, Any]:
    """日报：逐日漏斗（新增/触达分型/回传/转化率）+ token 成本 + 转人工数。

    漏斗与转人工按当前账号；**token 成本是全系统口径**——Pi 的推理开销不分
    账号，按账号拆反而看不出真实花费。
    """
    since = _today_start() - timedelta(days=days - 1)
    new_jc = {
        str(d): n
        for d, n in session.execute(
            select(func.date(models.JobCandidate.created_at), func.count())
            .where(
                models.JobCandidate.account_id == acct.id,
                models.JobCandidate.created_at >= since,
            )
            .group_by(func.date(models.JobCandidate.created_at))
        ).all()
    }
    out_rows = session.execute(
        select(func.date(models.Interaction.sent_at), models.Interaction.msg_type, func.count())
        .join(
            models.JobCandidate,
            models.JobCandidate.id == models.Interaction.job_candidate_id,
        )
        .where(
            models.JobCandidate.account_id == acct.id,
            models.Interaction.direction == "out",
            models.Interaction.sent_at >= since,
        )
        .group_by(func.date(models.Interaction.sent_at), models.Interaction.msg_type)
    ).all()
    received = {
        str(d): n
        for d, n in session.execute(
            select(func.date(models.Interaction.sent_at), func.count())
            .join(
                models.JobCandidate,
                models.JobCandidate.id == models.Interaction.job_candidate_id,
            )
            .where(
                models.JobCandidate.account_id == acct.id,
                models.Interaction.direction == "in",
                models.Interaction.msg_type == "attachment",
                models.Interaction.sent_at >= since,
            )
            .group_by(func.date(models.Interaction.sent_at))
        ).all()
    }
    costs = {
        str(d): (int(t or 0), float(c or 0.0))
        for d, t, c in session.execute(
            select(
                func.date(models.TaskLog.created_at),
                func.sum(models.TaskLog.tokens),
                func.sum(models.TaskLog.cost),
            )
            .where(models.TaskLog.created_at >= since)
            .group_by(func.date(models.TaskLog.created_at))
        ).all()
    }
    manual = {
        str(d): n
        for d, n in session.execute(
            select(func.date(models.TaskLog.created_at), func.count())
            .where(
                models.TaskLog.account_id == acct.id,
                models.TaskLog.outcome == "failed_needs_manual",
                models.TaskLog.created_at >= since,
            )
            .group_by(func.date(models.TaskLog.created_at))
        ).all()
    }
    day_map: dict[str, dict[str, int]] = {}
    for d, msg_type, n in out_rows:
        day = day_map.setdefault(str(d), {"direct_request": 0, "greet_request": 0})
        if msg_type in day:
            day[msg_type] = n
    all_days = sorted(set(new_jc) | set(received) | set(costs) | set(day_map) | set(manual))
    totals = {"new_jc": 0, "requests": 0, "received": 0, "tokens": 0, "cost": 0.0, "manual": 0}
    days_out = []
    for d in all_days:
        day = day_map.get(d, {"direct_request": 0, "greet_request": 0})
        requests = day["direct_request"] + day["greet_request"]
        recv = received.get(d, 0)
        tokens, cost = costs.get(d, (0, 0.0))
        days_out.append(
            {
                "date": d,
                "new_jc": new_jc.get(d, 0),
                "direct_request": day["direct_request"],
                "greet_request": day["greet_request"],
                "received": recv,
                "conversion": round(recv / requests * 100, 1) if requests else None,
                "tokens": tokens,
                "cost": round(cost, 6),
                "manual": manual.get(d, 0),
            }
        )
        totals["new_jc"] += new_jc.get(d, 0)
        totals["requests"] += requests
        totals["received"] += recv
        totals["tokens"] += tokens
        totals["cost"] = round(totals["cost"] + cost, 6)
        totals["manual"] += manual.get(d, 0)
    totals["conversion"] = (
        round(totals["received"] / totals["requests"] * 100, 1) if totals["requests"] else None
    )
    return {"days": days_out, "totals": totals}


# —— M4：人工队列 / 告警 ——


@router.get("/manual-queue")
def hr_manual_queue(
    limit: int = Query(50, ge=1, le=MANUAL_QUEUE_MAX),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
    queue: Any = Depends(get_queue),
    acct: models.Account = Depends(get_active_account),
) -> dict[str, Any]:
    """转人工队列：failed_needs_manual 落账 + 派发登记反查上下文（72h 内）。

    必须按账号过滤：条目里会带出候选人姓名，不过滤就是跨账号泄露。
    计数与列表共用同一条件，否则总数会串到别的账号。
    """
    total = session.scalar(
        select(func.count())
        .select_from(models.TaskLog)
        .where(
            models.TaskLog.account_id == acct.id,
            models.TaskLog.outcome == "failed_needs_manual",
        )
    )
    rows = (
        session.execute(
            select(models.TaskLog)
            .where(
                models.TaskLog.account_id == acct.id,
                models.TaskLog.outcome == "failed_needs_manual",
            )
            .order_by(models.TaskLog.id.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    items = []
    for log in rows:
        dispatched = None
        try:
            dispatched = queue.get_dispatched(UUID(log.task_id))
        except Exception:  # noqa: BLE001  # 登记过期/非本机派发：按缺失处理
            dispatched = None
        name = status = None
        if dispatched is not None and dispatched.job_candidate_id is not None:
            jc = session.get(models.JobCandidate, dispatched.job_candidate_id)
            # 双重保险：派发登记可能来自别的账号（或历史数据），只认本账号的 jc
            if jc is not None and jc.account_id == acct.id:
                status = jc.status
                candidate = session.get(models.Candidate, jc.candidate_id)
                name = candidate.name if candidate else None
        items.append(
            {
                "task_id": log.task_id,
                "task_type": dispatched.type.value if dispatched else None,
                "job_candidate_id": dispatched.job_candidate_id if dispatched else None,
                "candidate_name": name,
                "jc_status": status,
                "attempt": log.attempt,
                "note": log.note,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
        )
    return {"total": total or 0, "items": items}


class AckRequest(BaseModel):
    note: str = "人工已处理"


@router.post("/manual-queue/{task_id}/ack")
def hr_manual_ack(
    task_id: str,
    body: AckRequest,
    session: Session = Depends(get_session),
    acct: models.Account = Depends(get_active_account),
) -> dict[str, Any]:
    """人工队列忽略注记（追加 TaskLog.note，保留原注记）。仅限本账号的条目。"""
    log = session.execute(
        select(models.TaskLog)
        .where(
            models.TaskLog.task_id == task_id,
            models.TaskLog.account_id == acct.id,
            models.TaskLog.outcome == "failed_needs_manual",
        )
        .order_by(models.TaskLog.id.desc())
    ).scalars().first()
    if log is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 无待处理落账")
    suffix = f"{body.note}(acked)"
    log.note = f"{log.note}｜{suffix}"[:255] if log.note else suffix[:255]
    session.commit()
    return {"ok": True}


@router.get("/alerts")
def hr_alerts(
    session: Session = Depends(get_session),
    login_state: Any = Depends(get_login_state),
    acct: models.Account = Depends(get_active_account),
) -> dict[str, Any]:
    """告警可观测（最小）：登录态 / 本账号限流标志 / 今日配额用量 / 今日转人工。

    登录态与风控标志都按账号分片读取——它们描述的是「手机上那个号」的状态。
    """
    try:
        login = login_state.get(acct.id)
    except Exception:  # noqa: BLE001  # 登录态键缺失按未知处理
        login = None
    risk_paused = False
    try:
        client = redis_lib.Redis.from_url(get_settings().redis_url, decode_responses=True)
        try:
            risk_paused = client.get(risk_pause_key(acct.id)) is not None
        finally:
            client.close()
    except Exception:  # noqa: BLE001
        risk_paused = False
    start = _today_start()
    touches = session.scalar(
        select(func.count())
        .select_from(models.Interaction)
        .join(
            models.JobCandidate,
            models.JobCandidate.id == models.Interaction.job_candidate_id,
        )
        .where(
            models.JobCandidate.account_id == acct.id,
            models.Interaction.direction == "out",
            models.Interaction.sent_at >= start,
        )
    )
    manual = session.scalar(
        select(func.count())
        .select_from(models.TaskLog)
        .where(
            models.TaskLog.account_id == acct.id,
            models.TaskLog.outcome == "failed_needs_manual",
            models.TaskLog.created_at >= start,
        )
    )
    return {
        "login": login,
        "risk_paused": risk_paused,
        "quota_used_today": touches or 0,
        "manual_today": manual or 0,
    }


@router.get("/tasklogs")
def hr_tasklogs(
    limit: int = Query(200, ge=1, le=1000),
    session: Session = Depends(get_session),
    acct: models.Account = Depends(get_active_account),
) -> dict[str, Any]:
    """最近的运行日志（Pi 经 hr-tools tasklog_add 落账，倒序）。仅当前账号。"""
    rows = session.execute(
        select(models.TaskLog)
        .where(models.TaskLog.account_id == acct.id)
        .order_by(models.TaskLog.id.desc())
        .limit(limit)
    ).scalars().all()
    return {
        "items": [
            {
                "id": t.id,
                "task_id": t.task_id,
                "outcome": t.outcome,
                "attempt": t.attempt,
                "tokens": t.tokens,
                "cost": t.cost,
                "duration": t.duration,
                "note": t.note,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in rows
        ]
    }


# —— 动作端点 ——


class ReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    note: str = ""
    operator: str = "hr"


@router.post("/candidates/{jc_id}/review")
def hr_review(
    jc_id: int,
    body: ReviewRequest,
    session: Session = Depends(get_session),
    acct: models.Account = Depends(get_active_account),
) -> dict[str, Any]:
    """复核推翻：approve→hr_reviewed / reject→closed（OVERRIDE + 流水落账）。

    跨账号的 jc_id 按 404 处理——复核是写动作，更不能落到别的账号上。
    """
    jc = session.get(models.JobCandidate, jc_id)
    if jc is None or jc.account_id != acct.id:
        raise HTTPException(status_code=404, detail=f"job_candidate {jc_id} 不存在")
    if jc.status not in REVIEW_FROM:
        raise HTTPException(
            status_code=409, detail=f"当前状态 {jc.status} 不支持复核（仅 {sorted(REVIEW_FROM)}）"
        )
    target = CandidateStatus.HR_REVIEWED if body.decision == "approve" else CandidateStatus.CLOSED
    old_status = jc.status
    try:
        jc.status = transition(
            jc, StateEvent.OVERRIDE, TransitionContext(target_status=target)
        ).status
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.add(
        models.ReviewOverride(
            job_candidate_id=jc.id,
            old_status=old_status,
            new_status=jc.status,
            operator=body.operator or "hr",
            reason=body.note or f"人工复核：{body.decision}",
        )
    )
    session.commit()
    return {"ok": True, "status": jc.status}


class JobPatch(BaseModel):
    llm_threshold: int | None = None
    min_stars: int | None = None
    scoring_prefs: dict[str, Any] | None = None
    # 「补 JD 并启用」这条路（2026-10-15）：扫描入库的职位带的是 status=draft，
    # 前端填完 JD 才能转 active。没有这两个字段的话，扫到的职位永远启用不了。
    jd_text: str | None = None
    status: str | None = None


@router.patch("/jobs/{job_id}")
def hr_patch_job(
    job_id: int,
    body: JobPatch,
    session: Session = Depends(get_session),
    acct: models.Account = Depends(get_active_account),
) -> dict[str, Any]:
    """岗位配置回流：JD 正文 / 启停 / LLM 阈值（遗留 0-100）/ 评分卡。

    其它账号的 job_id 一律按 404 处理（不区分「不存在」与「不属于你」，
    避免通过状态码探测别的账号有哪些职位）——与 `/candidates/{jc_id}` 同一套规矩。

    ★ 这个校验是 2026-10-15 补的：原先这里只 `session.get(Job, job_id)` 就改，
      可以跨账号读写**别人职位的 JD / status / llm_threshold / scoring_prefs**。
      同文件的 `/candidates/{jc_id}` 与 `/overview` 一直都做了，唯独这里漏了。
    """
    job = session.get(models.Job, job_id)
    if job is None or job.account_id != acct.id:
        raise HTTPException(status_code=404, detail=f"岗位 {job_id} 不存在")

    if body.jd_text is not None:
        job.jd_text = body.jd_text

    if body.status is not None:
        if body.status not in (models.JOB_STATUS_ACTIVE, models.JOB_STATUS_DRAFT):
            raise HTTPException(
                status_code=422,
                detail=f"status 只能是 {models.JOB_STATUS_ACTIVE} 或 {models.JOB_STATUS_DRAFT}",
            )
        # ★ 校验的是**生效后**的 JD：这次可能只传了 status、JD 是库里已有的那份。
        #   放一个没有 JD 的职位 active，等于让 LLM 凭空判断候选人合不合适。
        if (
            body.status == models.JOB_STATUS_ACTIVE
            and len((job.jd_text or "").strip()) < MIN_JD_CHARS
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"JD 不足 {MIN_JD_CHARS} 字，不能启用——"
                    "没有 JD 的职位会让系统凭空判断候选人是否合适，"
                    "然后给真实候选人发招呼。"
                ),
            )
        job.status = body.status

    if body.llm_threshold is not None:
        if not 0 <= body.llm_threshold <= 100:
            raise HTTPException(status_code=422, detail="llm_threshold 须在 0-100")
        job.llm_threshold = body.llm_threshold
    if body.scoring_prefs is not None:
        job.scoring_prefs = body.scoring_prefs
    elif body.min_stars is not None:
        if not 1 <= body.min_stars <= 5:
            raise HTTPException(status_code=422, detail="min_stars 须在 1-5")
        prefs = dict(job.scoring_prefs or {})
        prefs["min_stars"] = body.min_stars
        job.scoring_prefs = prefs
    session.commit()
    return {
        "ok": True,
        "id": job.id,
        "status": job.status,
        "jd_chars": len((job.jd_text or "").strip()),
        "llm_threshold": job.llm_threshold,
        "scoring_prefs": job.scoring_prefs,
    }
