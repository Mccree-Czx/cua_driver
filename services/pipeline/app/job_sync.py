"""把手机扫描到的职位落库（2026-10-15）。

**为什么不复用 `tools_mcp_server.job_upsert`**：它按「当前生效账号」判重
（`_active_account_id()`），而扫描与写库之间隔着一次跨机器往返——中间用户若又切了号，
A 号的职位会被静默记到 B 号头上。这里全部显式吃 `account_id`。

**为什么新职位是 `draft` 而不是 `active`**：扫到的只是一个名字 + 地点学历薪资，
**没有 JD**。而 JD 是打分的唯一依据——让没有 JD 的职位 active，等于让 LLM 凭空挑候选人
然后给真人发招呼。`job_list_active` / `job_quota_status` 都硬过滤 active，
所以 draft 的职位**在工具层就看不见**：联锁在代码里，不在提示词里。

**为什么永不删除、永不关闭**：`truncated=True` 时列表本就不全（滑动没到底），
拿一次不完整的扫描去关职位，会把正常在招的职位误关掉。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from app.db import SessionLocal
from app.models import JOB_STATUS_ACTIVE, JOB_STATUS_DRAFT, Account, Job

# 一个职位能被启用（active）所需的最短 JD。低于这个长度基本等于没有内容——
# 而「没有 JD 的 active 职位」会让 LLM 凭空判断谁合适，然后给真实候选人发招呼。
# 三处共用：本模块、前端 PATCH、MCP 的 job_upsert。
MIN_JD_CHARS = 30

# 分类顺序有讲究：薪资要先判，否则「25-45k」里的连字符会被当成地点。
_SALARY_RE = re.compile(r"\d+\s*[-~]\s*\d+\s*[kK]|\d+\s*[kK]")
_EXPERIENCE_RE = re.compile(r"\d+\s*[-~]?\s*\d*\s*年|应届|经验不限")
_EDUCATION_WORDS = ("大专", "本科", "硕士", "博士", "中专", "高中", "初中", "学历不限")
# 地点有两种写法：「上海-黄浦区」（强特征）和光秃秃的「北京」。后者只能靠
# 「是个纯中文词」来认，所以放在第二遍兜底——不然卡片上任意一个中文词
# （「急招」之类）都可能被误当成地点。
_STRONG_LOCATION_RE = re.compile(r"^[一-龥]{2,12}(-[一-龥]{2,12})*$")
_PLAIN_CJK_RE = re.compile(r"^[一-龥]{2,12}$")


def parse_details(details: str) -> dict[str, str]:
    """把职位卡的详情行拆成 location / education / experience / salary。

    卡片原文形如「上海-黄浦区 本科 5年以上 25-45k」。

    **宽容解析**：认不出的词直接丢掉，缺项留空字符串（调用方再转成 NULL）。
    App 改一次版式不该让整次扫描失败——职位名已经拿到了，详情少一两项不影响入库，
    前端也允许手工补。
    """
    out = {"location": "", "education": "", "experience": "", "salary": ""}
    leftover: list[str] = []
    for token in details.replace(" ", " ").split():
        # 顺序有讲究：薪资先判，否则「25-45k」里的连字符会被当成地点。
        if not out["salary"] and _SALARY_RE.search(token):
            out["salary"] = token
        elif not out["experience"] and _EXPERIENCE_RE.search(token):
            out["experience"] = token
        elif not out["education"] and any(w in token for w in _EDUCATION_WORDS):
            out["education"] = token
        elif not out["location"] and "-" in token and _STRONG_LOCATION_RE.match(token):
            out["location"] = token
        else:
            leftover.append(token)
    if not out["location"]:
        out["location"] = next((t for t in leftover if _PLAIN_CJK_RE.match(t)), "")
    return out


def sync_scanned_jobs(
    account_id: int,
    items: list[dict[str, Any]],
    *,
    truncated: bool = False,
    jds: dict[str, str] | None = None,
    complete: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """把一次扫描结果落库。`items` 形如 `[{"title": ..., "details": ...}]`。

    `truncated` 只用于回报（说明这次没扫全），不影响写入行为——因为本函数从不删改状态。

    `jds` 是**另一次调用**补上来的（从职位详情页读到的 JD），所以 `items` 可以是空的。
    `complete[title]` 表示那段 JD **确认读完整了**（没被截断）。

    ★ 只有 `complete` 且长度达标的 JD 才会把职位转成 `active`。抓半截的 JD 比空 JD
      **更危险**——它看起来像有依据，LLM 会拿它当真去筛真实候选人。
    """
    created = updated = unchanged = 0
    jd_filled = jd_activated = 0
    skipped: list[str] = []

    with SessionLocal() as session:
        # 去重键与 job_upsert 保持一致：(account_id, title) 精确相等。
        # 两个号可以有同名职位，各建各的。
        existing = {
            job.title: job
            for job in session.execute(
                select(Job).where(Job.account_id == account_id)
            ).scalars()
        }

        for item in items:
            title = str(item.get("title") or "").strip()
            if not title:
                skipped.append("(空标题)")
                continue

            # 解析不到就存 NULL，不存空字符串：空串在 SQL 里是「有这个值，值是空」，
            # 而语义是「没读到」。前端也靠 NULL 判断「这一项缺失」。
            fields = {
                k: (v or None) for k, v in parse_details(str(item.get("details") or "")).items()
            }
            job = existing.get(title)

            if job is None:
                job = Job(
                    account_id=account_id,
                    title=title,
                    jd_text="",
                    status=JOB_STATUS_DRAFT,
                    **fields,
                )
                session.add(job)
                existing[title] = job  # 同一次扫描里的重名卡片只建一条
                created += 1
                continue

            # 已存在的职位只补详情，**不动 status / jd_text**：
            # 用户可能已经补好 JD 并启用了，重扫一次不该把它打回 draft。
            changed = False
            for key, value in fields.items():
                if value and getattr(job, key) != value:
                    setattr(job, key, value)
                    changed = True
            if changed:
                updated += 1
            else:
                unchanged += 1

        # 补 JD。可能是同一次调用的尾巴，也可能是 items=[] 的第二次调用。
        for title, text in (jds or {}).items():
            job = existing.get(title)
            if job is None:
                continue  # 库里没这个职位就不硬塞
            if (job.jd_text or "").strip():
                continue  # ★ 永不覆盖非空 JD：用户可能已经在前端改过了
            job.jd_text = text
            jd_filled += 1
            if (complete or {}).get(title) and len(text.strip()) >= MIN_JD_CHARS:
                job.status = JOB_STATUS_ACTIVE
                jd_activated += 1

        session.commit()

    return {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "scanned": len(items),
        "skipped": skipped,
        "truncated": truncated,
        "jd_filled": jd_filled,
        "jd_activated": jd_activated,
    }


def active_job_count() -> int:
    """当前生效账号名下的 active 职位数。

    供 `agent/run_round.py` 用——它**只跑标准库**，读不了库，只能 shell 出来问一句
    （与 `_write_token_summary` 同一个手法）。职位数决定这轮有没有活可干。
    """
    with SessionLocal() as session:
        account_id = session.scalar(select(Account.id).where(Account.is_active.is_(True)))
        if account_id is None:
            return 0
        return len(
            session.execute(
                select(Job.id).where(
                    Job.account_id == account_id, Job.status == JOB_STATUS_ACTIVE
                )
            ).all()
        )


if __name__ == "__main__":
    print(active_job_count())
