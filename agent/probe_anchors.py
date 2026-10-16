#!/usr/bin/env python3
"""按设备重探猎聘锚点（2026-10-16，为云手机而写）。

## 为什么是「人决定点哪，机器只负责看」

沿用 `probe_jobs.py` 的同一条原则：这是在**真实招聘账号**上操作，乱试的代价可能是
误发布职位、误发消息、误退出登录。所以本脚本**自己绝不主动点任何东西**——
只有你明确给 `--tap x,y` 它才点。

## 用途

换一台设备（分辨率、App 版本都不同）就得重探一套锚点。本脚本把当前屏按三类摊开：

1. **危险按钮**——只报警，绝不写进锚点表
2. **文字锚点候选**（`{"match": "..."}`）——跨设备可复用。云机语义走 `content-desc`，
   一律经 `phone.label()`（= text or content-desc）取，所以两边的写法是一致的
3. **坐标锚点候选**（`{"center": [x,y]}`）——设备专属，必须人工确认

顺带核对**设备标尺**（分辨率 + 猎聘 versionCode）与锚点表记的是否一致——
不一致就是「这份表该重探了」的信号。

## 用法

    python agent/probe_anchors.py --serial 127.0.0.1:63833   # 选定云机
    python agent/probe_anchors.py                            # dump 当前屏
    python agent/probe_anchors.py --tap 630,1223             # 点一下（**由你决定**）
    python agent/probe_anchors.py --back                     # BACK，再 dump
    python agent/probe_anchors.py --raw                      # 附原始 XML
    python agent/probe_anchors.py --out 片段.json            # 显式才写盘（默认只打印）

探完把候选填进对应的锚点表（真机 `login_map.json` / 云机 `login_map.cloud.json`），
并把该表的 `probe.at` 改成当天。**候选不是锚点**——哪几个该进表由你决定。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import flows  # noqa: E402
import phone  # noqa: E402

PKG = flows.PKG

#: 猎聘上「点错就出事」的文字。**只用来报警，绝不写进锚点表。**
DANGER_MARKERS = (
    "发布职位",
    "发职位",
    "关闭职位",
    "编辑职位",
    "刷新职位",
    "下线",
    "索要简历",
    "求简历",
    "打招呼",
    "立即沟通",
    "电话沟通",
    "邀请应聘",
    "退出登录",
    "注销账号",
)

#: 短到这个长度以内、且不含换行的文字，才可能是锚点。
#: 长文字是**内容**（候选人卡片、JD 正文），不是锚点。
_MAX_MATCH_LEN = 14


def _read_screen_size() -> str:
    try:
        out = phone.shell("wm", "size")
    except (phone.AdbError, OSError):
        return "?"
    found = re.search(r"Physical size: (\d+x\d+)", out)
    return found.group(1) if found else "?"


def _read_app_version() -> str:
    try:
        out = phone.shell("dumpsys", "package", PKG)
    except (phone.AdbError, OSError):
        return "?"
    found = re.search(r"versionCode=(\d+)", out)
    return found.group(1) if found else "?"


def _report_scale() -> dict[str, str]:
    """核对设备与锚点表是否对得上。返回 {"screen":..., "version_code":..., "map":...}。"""
    screen = _read_screen_size()
    version = _read_app_version()
    print("=== 设备标尺 ===")
    print(f"  设备     : {phone.current_device() or '(默认：交给 adb 挑唯一那台)'}")
    print(f"  分辨率   : {screen}")
    print(f"  猎聘版本 : {version}")

    try:
        app = flows.load_map()["app"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"  锚点表   : ✗ 读不到（{exc}）—— 这台设备的表还没建，正常")
        return {"screen": screen, "version_code": version, "map": ""}

    name = flows.map_path().name
    print(
        f"  锚点表   : {name} 记的是 {app.get('screen')} / versionCode {app.get('version_code')}"
    )
    if screen != "?" and app.get("screen") != screen:
        print("  ⚠ 分辨率不一致 → **纯坐标锚点全部失效**，必须逐页重探")
    if version != "?" and str(app.get("version_code")) != version:
        print("  ⚠ App 版本不一致 → 锚点可能已漂移，逐页核对文案与位置")
    return {"screen": screen, "version_code": version, "map": name}


def _classify(xml: str) -> dict[str, Any]:
    """把当前屏的节点分成：危险项、文字候选、坐标候选。"""
    texts = phone.texts(xml)
    joined = " ".join(texts)

    danger: list[str] = []
    for marker in DANGER_MARKERS:
        if marker in joined:
            danger.append(marker)

    matches: list[dict[str, Any]] = []
    centers: list[dict[str, Any]] = []
    for node in phone.nodes(xml):
        label = phone.label(node)
        x1, y1, x2, y2 = node["bounds"]
        if label and "\n" not in label and len(label) <= _MAX_MATCH_LEN:
            matches.append(
                {
                    "match": label,
                    "center": list(node["center"]),
                    "bounds": [x1, y1, x2, y2],
                    "clickable": node["clickable"],
                    "danger": label in DANGER_MARKERS,
                }
            )
        elif not label and node["clickable"]:
            # 无文字但可点：Flutter 图标常是这样（真机探「我的」页设置图标时漏了三轮）
            centers.append(
                {"center": list(node["center"]), "bounds": [x1, y1, x2, y2], "cls": node["cls"]}
            )
    return {
        "texts": texts,
        "joined": joined,
        "danger": danger,
        "matches": matches,
        "centers": centers,
    }


def _report(xml: str, scale: dict[str, str], *, raw: bool, out: str | None) -> None:
    info = _classify(xml)

    # ——— 危险警报置顶：它比什么都重要 ———
    form_hit = [m for m in flows._PUBLISH_FORM_MARKERS if m in info["joined"]]
    if form_hit:
        print()
        print("  " + "!" * 66)
        print(f"  !! 这是「发职位」的空白新建表单（看到 {form_hit}）")
        print("  !! **提交了就是真的对外发布一个职位**。要离开请按 `--back`。")
        print("  " + "!" * 66)
    risk = phone.risk_markers(xml)
    if risk:
        print(f"\n  ⚠ 风控标记：{risk} —— 停手，不要继续")
    if info["danger"]:
        print(f"\n  ⛔ 屏上有危险按钮 {info['danger']} —— **绝不写进锚点表**")

    # ——— 文字候选 ———
    print()
    print("=== 文字锚点候选（跨设备可复用）===")
    if not info["matches"]:
        print("  （无）")
    for item in info["matches"]:
        mark = "⛔" if item["danger"] else ("●" if item["clickable"] else "○")
        b = item["bounds"]
        print(
            f"  {mark} {{\"match\": {json.dumps(item['match'], ensure_ascii=False)}}}"
            f"  [{b[0]},{b[1]}][{b[2]},{b[3]}]"
        )
    print("  （○ = clickable=false，Flutter 图标常是这样但仍可点）")

    # ——— 坐标候选 ———
    print()
    print("=== 坐标锚点候选（设备专属，需人工确认）===")
    if not info["centers"]:
        print("  （无）")
    for item in info["centers"]:
        b = item["bounds"]
        print(
            f"  ? {{\"center\": {item['center']}}}  [{b[0]},{b[1]}][{b[2]},{b[3]}]"
            f"  ({item['cls']} 无文字)"
        )

    # ——— 可供粘贴的片段 ———
    fragment = {
        "app": {"package": PKG, "version_code": _as_int(scale["version_code"]), "screen": scale["screen"]},
        "probe": {
            "at": date.today().isoformat(),
            "by": "probe_anchors.py",
            "serial": phone.current_device() or "(默认设备)",
            "note": "候选由探针列出，进表的锚点须人工确认",
        },
        "_candidates": {
            "match": [m["match"] for m in info["matches"] if not m["danger"]],
            "center": [c["center"] for c in info["centers"]],
        },
    }
    print()
    print("=== 可粘贴片段（候选，不是锚点）===")
    print(json.dumps(fragment, ensure_ascii=False, indent=2))

    print()
    print("=== 屏上文字（去重）===")
    print("  " + " | ".join(t.replace("\n", "⏎")[:40] for t in info["texts"][:40]))

    if out:
        Path(out).write_text(json.dumps(fragment, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[probe] 片段已写到 {out}")
    if raw:
        print("\n=== 原始 XML ===")
        print(xml)


def _as_int(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def main() -> int:
    # Windows 控制台默认 GBK：中文会乱码，而 ✗/⚠/⛔ 这类符号在 GBK 里不存在，
    # 会直接抛 UnicodeEncodeError——**连异常处理里的 print 都会崩**，报错信息全丢。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="按设备重探猎聘锚点（只报告，不主动点）")
    parser.add_argument("--serial", help="设备序列号（如 127.0.0.1:63833）；不传则用默认设备")
    parser.add_argument("--tap", metavar="X,Y", help="点这个坐标（**由你决定**），再 dump")
    parser.add_argument("--back", action="store_true", help="按 BACK，再 dump")
    parser.add_argument("--raw", action="store_true", help="附带打印原始 XML")
    parser.add_argument("--out", metavar="PATH", help="把候选片段写成 JSON（默认只打印，不落盘）")
    args = parser.parse_args()

    if args.serial:
        phone.use_device(args.serial)
        if not phone.ensure_connected():
            print(f"  ✗ 连不上设备 {args.serial}（adb connect 失败或未授权）")
            return 1

    print(f"[probe] adb = {phone.ADB}")

    if args.back:
        print("[probe] BACK")
        phone.back()

    if args.tap:
        try:
            x_str, y_str = args.tap.split(",")
            point = (int(x_str), int(y_str))
        except ValueError:
            print(f"  ✗ --tap 格式应为 X,Y，收到 {args.tap!r}")
            return 2
        print(f"[probe] 点击 {point}")
        phone.tap(point, pause=3)

    scale = _report_scale()
    # ★ 用 dump_stable：云机慢，dump 会偶发失败，此时 cat 拿到的是**上一次的旧文件**
    #   ——不报错，但是错的页面。拿它做锚点判断等于在错的屏上标坐标。
    _report(phone.dump_stable(), scale, raw=args.raw, out=args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
