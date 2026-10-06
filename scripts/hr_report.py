#!/usr/bin/env python
"""HR 查看通道（M3 前凑合）+ 漏斗统计 + 归档拉取（只读脚本，不改库）。

用法：
    .venv/bin/python scripts/hr_report.py                 # 候选人清单（全岗位）
    .venv/bin/python scripts/hr_report.py --job 1         # 限定岗位
    .venv/bin/python scripts/hr_report.py --stats         # 每日漏斗（近 14 天）
    .venv/bin/python scripts/hr_report.py --stats --days 30
    .venv/bin/python scripts/hr_report.py --fetch all     # 拉快照+PDF 到 exports/
    .venv/bin/python scripts/hr_report.py --fetch 4       # 仅 jc id=4

数据面：DATABASE_URL / MINIO_* env（默认生产面）。
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]
EXPORTS = REPO / "exports"

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "mysql+pymysql://hr_user:hr_dev_pw@127.0.0.1:3306/hr_workbuddy"
)
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "127.0.0.1:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadmin")
BUCKET = os.environ.get("MINIO_BUCKET", "hr-workbuddy")


def connect():
    import pymysql

    parsed = urlparse(DATABASE_URL)
    return pymysql.connect(
        host=parsed.hostname or "127.0.0.1",
        port=parsed.port or 3306,
        user=parsed.username or "root",
        password=parsed.password or "",
        database=parsed.path.lstrip("/"),
        cursorclass=pymysql.cursors.DictCursor,
    )


def _cell(value) -> str:
    return "—" if value is None else str(value)


def cmd_list(args: argparse.Namespace) -> None:
    where = "WHERE jc.job_id = %s" if args.job else ""
    params: tuple = (args.job,) if args.job else ()
    sql = f"""
        SELECT jc.id, c.name, j.title AS job, jc.status, jc.match_score,
               COALESCE(jc.judge_reason, '') AS reason,
               c.snapshot_object_key, jc.minio_object_key, jc.last_touch_at
        FROM job_candidate jc
        JOIN candidates c ON c.id = jc.candidate_id
        JOIN jobs j ON j.id = jc.job_id
        {where}
        ORDER BY jc.id
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    print(f"### 候选人清单（{len(rows)} 行）\n")
    print("| jc | 姓名 | 岗位 | 状态 | 评分 | 截图 | PDF | 最近触达 | 理由摘要 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        reason = " ".join(r["reason"].split())[:48]
        print(
            f"| {r['id']} | {_cell(r['name'])} | {_cell(r['job'])} | {r['status']} "
            f"| {_cell(r['match_score'])} | {'有' if r['snapshot_object_key'] else '—'} "
            f"| {'有' if r['minio_object_key'] else '—'} | {_cell(r['last_touch_at'])} | {reason} |"
        )
    print("\n快照/PDF 拉取：`hr_report.py --fetch all`（导出到 exports/）")


def cmd_stats(args: argparse.Namespace) -> None:
    since = datetime.now() - timedelta(days=args.days)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DATE(sent_at) AS d, msg_type, COUNT(*) AS n
            FROM interactions
            WHERE direction = 'out' AND sent_at >= %s
            GROUP BY d, msg_type ORDER BY d
            """,
            (since,),
        )
        out_rows = cur.fetchall()
        cur.execute(
            """
            SELECT DATE(sent_at) AS d, COUNT(*) AS n
            FROM interactions
            WHERE direction = 'in' AND msg_type = 'attachment' AND sent_at >= %s
            GROUP BY d
            """,
            (since,),
        )
        received = {r["d"]: r["n"] for r in cur.fetchall()}
        cur.execute(
            """
            SELECT DATE(created_at) AS d, COUNT(*) AS n
            FROM job_candidate WHERE created_at >= %s GROUP BY d
            """,
            (since,),
        )
        new_jc = {r["d"]: r["n"] for r in cur.fetchall()}

    days: dict = {}
    for r in out_rows:
        day = days.setdefault(r["d"], {"direct_request": 0, "greet_request": 0, "reply": 0})
        if r["msg_type"] in day:
            day[r["msg_type"]] = r["n"]
    all_days = sorted(set(days) | set(received) | set(new_jc))
    print(f"### 每日漏斗（近 {args.days} 天）\n")
    print("| 日期 | 新增候选人 | 直索要 | 打招呼 | 回执 | 收到简历 | 索要→回传 |")
    print("|---|---|---|---|---|---|---|")
    totals = [0, 0, 0, 0, 0]
    for d in all_days:
        day = days.get(d, {"direct_request": 0, "greet_request": 0, "reply": 0})
        req = day["direct_request"] + day["greet_request"]
        recv = received.get(d, 0)
        conv = f"{recv / req * 100:.0f}%" if req else "—"
        print(
            f"| {d} | {new_jc.get(d, 0)} | {day['direct_request']} | {day['greet_request']} "
            f"| {day['reply']} | {recv} | {conv} |"
        )
        totals[0] += new_jc.get(d, 0)
        totals[1] += day["direct_request"]
        totals[2] += day["greet_request"]
        totals[3] += day["reply"]
        totals[4] += recv
    req_total = totals[1] + totals[2]
    conv_total = f"{totals[4] / req_total * 100:.0f}%" if req_total else "—"
    print(
        f"| **合计** | {totals[0]} | {totals[1]} | {totals[2]} | {totals[3]} "
        f"| {totals[4]} | {conv_total} |"
    )


def cmd_fetch(args: argparse.Namespace) -> None:
    from minio import Minio

    client = Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=False,
    )
    if args.fetch == "all":
        sql = """
            SELECT jc.id AS jc_id, c.name, c.snapshot_object_key, jc.minio_object_key
            FROM job_candidate jc JOIN candidates c ON c.id = jc.candidate_id
            ORDER BY jc.id
        """
        params: tuple = ()
    else:
        sql = """
            SELECT jc.id AS jc_id, c.name, c.snapshot_object_key, jc.minio_object_key
            FROM job_candidate jc JOIN candidates c ON c.id = jc.candidate_id
            WHERE jc.id = %s
        """
        params = (int(args.fetch),)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    fetched = missing = 0
    for r in rows:
        out_dir = EXPORTS / f"jc{r['jc_id']}_{r['name']}"
        for kind, key in (
            ("snapshot", r["snapshot_object_key"]),
            ("resume", r["minio_object_key"]),
        ):
            if not key:
                continue
            target = out_dir / f"{kind}_{Path(key).name}"
            try:
                client.fget_object(BUCKET, key, str(target))
                fetched += 1
                print(f"ok  jc={r['jc_id']} {kind}: {key} → {target.relative_to(REPO)}")
            except Exception as exc:  # noqa: BLE001
                missing += 1
                print(f"缺失/失败  jc={r['jc_id']} {kind}: {key}（{exc}）")
    print(f"\n完成：成功 {fetched}，缺失/失败 {missing}；目录 {EXPORTS.relative_to(REPO)}/")


def main() -> int:
    parser = argparse.ArgumentParser(description="HR 查看通道（清单/统计/拉取）")
    parser.add_argument("--job", type=int, help="限定岗位 id（默认全岗位）")
    parser.add_argument("--stats", action="store_true", help="输出每日漏斗统计")
    parser.add_argument("--days", type=int, default=14, help="统计天数（默认 14）")
    parser.add_argument("--fetch", help="拉取归档：all 或 jc id")
    args = parser.parse_args()

    if args.fetch:
        cmd_fetch(args)
    elif args.stats:
        cmd_stats(args)
    else:
        cmd_list(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
