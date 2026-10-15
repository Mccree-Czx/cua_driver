"""职位同步测试（2026-10-15）。

背景：切到新账号后 `Job` 表里那个号名下 0 个职位 → `job_list_active()` 返空 →
`job_quota_status()` 返 `{"quota_each":0,"jobs":[]}` → Pi 无事可做 →
`inbound=0 outbound=0` → `empty_streak` 涨到 2 → 后续 30 分钟的 main_round 全被退避跳过。

这个模块是「切号后自动扫职位」的**落库侧**。它出错的方式都很安静，所以每条都要钉死：

- 职位记到别的号头上（扫描与写库之间用户又切了号）
- 重扫一次，把用户已经补好的 JD / 已启用的状态打回 draft
- `has_active_jobs()` 的 fail-open 写反 → 一次 DB 抖动静默停掉整个系统
"""

import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app import models
from app.job_sync import active_job_count, parse_details, sync_scanned_jobs
from conftest import DEFAULT_ACCOUNT_ID

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "agent"))

import run_round  # noqa: E402

# 猎聘职位卡实测格式（2026-10-11）
CARD = "机械结构工程师\n上海-黄浦区 本科 5年以上 25-45k"


def _title(prefix: str = "职位") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _other_account(session) -> models.Account:
    acct = models.Account(name=f"号-{uuid.uuid4().hex[:8]}")
    session.add(acct)
    # ★ commit 而不是 flush：sync_scanned_jobs 开另一个会话往 jobs 插行，
    #   而 MySQL 的 FK 检查要去锁 accounts 里那一行——本会话若还扣着未提交的
    #   排他锁，那边就会「Lock wait timeout exceeded」。
    session.commit()
    return acct


# ——— 卡片解析（纯函数，不碰库）———


def test_parse_details_splits_real_card():
    """实测卡片「上海-黄浦区 本科 5年以上 25-45k」要拆成四项。

    分类顺序有讲究：薪资必须先判，否则「25-45k」里的连字符会被当成地点。
    """
    got = parse_details("上海-黄浦区 本科 5年以上 25-45k")
    assert got == {
        "location": "上海-黄浦区",
        "education": "本科",
        "experience": "5年以上",
        "salary": "25-45k",
    }


def test_parse_details_tolerates_unknown_tokens():
    """认不出的词丢掉、缺项留空——App 改版式不该让整次扫描失败。"""
    got = parse_details("北京 硕士 3-5年 30-50k·14薪 急招")
    assert got["education"] == "硕士"
    assert got["salary"] == "30-50k·14薪"
    assert got["location"] == "北京"
    assert parse_details("")["location"] == ""
    assert parse_details("随便什么")["salary"] == ""


# ——— 落库 ———


def test_new_jobs_land_as_draft(session):
    """扫到的职位一律 draft——**没有 JD 的职位不许 active**。

    这是整套设计的核心联锁：`job_list_active` / `job_quota_status` 都硬过滤 active，
    所以 draft 的职位在工具层就看不见，Pi 不会拿空 JD 去挑真实候选人。
    """
    title = _title()
    result = sync_scanned_jobs(
        DEFAULT_ACCOUNT_ID, [{"title": title, "details": "上海-黄浦区 本科 5年以上 25-45k"}]
    )
    assert result["created"] == 1

    job = session.execute(
        select(models.Job).where(models.Job.title == title)
    ).scalar_one()
    assert job.status == models.JOB_STATUS_DRAFT
    assert job.jd_text == ""
    assert (job.location, job.education, job.experience, job.salary) == (
        "上海-黄浦区",
        "本科",
        "5年以上",
        "25-45k",
    )


def test_rescan_is_idempotent(session):
    """同一次扫描里的重名卡片、以及后续重扫，都只留一条。"""
    title = _title()
    item = {"title": title, "details": "上海-黄浦区 本科 5年以上 25-45k"}
    first = sync_scanned_jobs(DEFAULT_ACCOUNT_ID, [item, item])
    assert (first["created"], first["updated"]) == (1, 0)

    second = sync_scanned_jobs(DEFAULT_ACCOUNT_ID, [item])
    assert (second["created"], second["unchanged"]) == (0, 1)

    rows = session.execute(select(models.Job).where(models.Job.title == title)).scalars().all()
    assert len(rows) == 1


def test_rescan_does_not_reset_status_or_jd(session):
    """★ 最重要的一条：用户补好 JD 并启用之后，重扫**不能**把它打回 draft。

    切号、定时扫描都会重扫同一个号。如果重扫覆盖 status/jd_text，
    用户每次补完 JD 隔一轮就白补——而且完全没有报错。
    """
    title = _title()
    sync_scanned_jobs(DEFAULT_ACCOUNT_ID, [{"title": title, "details": "上海 本科 3-5年 20-30k"}])

    job = session.execute(select(models.Job).where(models.Job.title == title)).scalar_one()
    job.jd_text = "负责机械结构设计，熟悉 Creo / SolidWorks，五年以上相关经验。"
    job.status = models.JOB_STATUS_ACTIVE
    # 必须 commit 而不是 flush：sync_scanned_jobs 开的是**另一个会话**，
    # flush 出去的东西它看不见，那样这条测试就变成了空转——代码真把 status
    # 重置回 draft 它也会绿。
    session.commit()

    sync_scanned_jobs(DEFAULT_ACCOUNT_ID, [{"title": title, "details": "上海 本科 3-5年 20-30k"}])

    session.refresh(job)
    assert job.status == models.JOB_STATUS_ACTIVE
    assert job.jd_text.startswith("负责机械结构设计")


def test_rescan_fills_missing_details_only(session):
    """重扫补上缺的详情，但不会把已有的非空值抹成空。"""
    title = _title()
    sync_scanned_jobs(DEFAULT_ACCOUNT_ID, [{"title": title, "details": "上海-黄浦区"}])
    job = session.execute(select(models.Job).where(models.Job.title == title)).scalar_one()
    assert job.location == "上海-黄浦区"
    assert job.salary is None

    result = sync_scanned_jobs(
        DEFAULT_ACCOUNT_ID, [{"title": title, "details": "上海-黄浦区 本科 5年以上 25-45k"}]
    )
    assert result["updated"] == 1
    # ★ 必须先结束当前事务：MySQL 默认 REPEATABLE READ，本会话在上面那次 select
    #   就把快照固定住了，sync 那条 commit 它**看不见**——直接 refresh 读回来还是旧值。
    session.commit()
    session.refresh(job)
    assert (job.education, job.salary) == ("本科", "25-45k")


def test_same_title_in_two_accounts_stays_separate(session):
    """★ 两个号可以有同名职位，各建各的。

    这也是「必须显式吃 account_id、不能用 job_upsert 的『当前生效账号』」的原因：
    扫描与写库之间隔着一次跨机器往返，中间切了号就会把 A 号的职位记到 B 号头上。
    """
    title = _title()
    other = _other_account(session)

    sync_scanned_jobs(DEFAULT_ACCOUNT_ID, [{"title": title, "details": "上海 本科"}])
    sync_scanned_jobs(other.id, [{"title": title, "details": "北京 硕士"}])

    rows = session.execute(select(models.Job).where(models.Job.title == title)).scalars().all()
    assert {r.account_id for r in rows} == {DEFAULT_ACCOUNT_ID, other.id}
    assert {r.location for r in rows} == {"上海", "北京"}


def test_truncated_scan_never_deletes(session):
    """扫描没到底（truncated）时列表本就不全，绝不能拿它去关职位。"""
    keep = _title()
    sync_scanned_jobs(DEFAULT_ACCOUNT_ID, [{"title": keep, "details": "上海 本科"}])

    result = sync_scanned_jobs(DEFAULT_ACCOUNT_ID, [], truncated=True)
    assert result["truncated"] is True
    assert result["created"] == 0

    still = session.execute(select(models.Job).where(models.Job.title == keep)).scalar_one()
    assert still.status == models.JOB_STATUS_DRAFT


def test_blank_title_is_skipped(session):
    result = sync_scanned_jobs(DEFAULT_ACCOUNT_ID, [{"title": "  ", "details": "上海 本科"}])
    assert (result["created"], result["skipped"]) == (0, ["(空标题)"])


# ——— 空转闸门 ———


def test_active_job_count_counts_only_active(session):
    """draft 不算数——它正是「这个号还不能干活」的表达。"""
    before = active_job_count()
    title = _title()
    sync_scanned_jobs(DEFAULT_ACCOUNT_ID, [{"title": title, "details": "上海 本科"}])
    assert active_job_count() == before

    job = session.execute(select(models.Job).where(models.Job.title == title)).scalar_one()
    job.status = models.JOB_STATUS_ACTIVE
    session.commit()  # active_job_count 开的是另一个会话，flush 它看不见
    assert active_job_count() == before + 1


@pytest.mark.parametrize("boom", ["missing-python", "subprocess-error", "nonzero-exit"])
def test_has_active_jobs_fails_open(monkeypatch, boom):
    """★ fail-open 必须钉死：查不到一律当成「有职位」。

    反过来写的话，一次 DB 抖动会让每一轮 main_round 都静默跳过——
    而它的表现和「正常跳过」一模一样，没人会发现系统已经停了。
    """
    import subprocess

    if boom == "missing-python":
        # 找不到带依赖的解释器 → 放行（不是拦下来）
        monkeypatch.setattr(run_round, "_job_query_python", lambda: None)
    elif boom == "subprocess-error":
        def _raise(*a, **k):
            raise subprocess.TimeoutExpired(cmd="x", timeout=1)

        monkeypatch.setattr(run_round.subprocess, "run", _raise)
    else:
        monkeypatch.setattr(
            run_round.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="boom"),
        )
    assert run_round.has_active_jobs() is True
