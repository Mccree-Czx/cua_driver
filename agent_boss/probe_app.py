#!/usr/bin/env python3
"""BOSS直聘 App 探查工具（2026-10-16）。

**用途只有一个：把屏幕上有什么如实呈现出来，好把锚点写进 `boss_map.json`。**

## 为什么是「人决定点哪，机器只负责看」

这是在**真实招聘账号**上操作，而且 BOSS 的按钮后果比猎聘更重：
点「立即沟通」会给真实候选人发消息，点「开放职位」会让职位对外公开，
点「立即开启」还会扣直豆。所以本脚本**自己绝不主动点任何东西**——
只有你明确给 `--tap x,y` 它才点。每一步都 dump 给你看，
点错了立刻 `--back` 退回来。

## 用法

    python agent_boss/probe_app.py                  # dump 当前屏
    python agent_boss/probe_app.py --tab 牛人        # 切到某个底部 tab 再 dump
    python agent_boss/probe_app.py --tap 540,667    # 点一下（**由你决定**），再 dump
    python agent_boss/probe_app.py --back           # BACK，再 dump
    python agent_boss/probe_app.py --raw            # 附带打印原始 XML
    python agent_boss/probe_app.py --save /tmp/x    # 把原始 XML 存下来
    python agent_boss/probe_app.py --rid cl_tab_1   # 只看某个 rid 的节点

★ BOSS 的 dump 里带 `resource-id`（原生 Android），所以锚点**优先抄 rid**，
坐标只作兜底。这跟猎聘（Flutter，只能靠文字/坐标）是相反的。
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import phone  # noqa: E402


def _width(s: str) -> int:
    """显示宽度。★ 中文是双宽：按 len() 补齐会让含中文的行整体错位。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, n: int) -> str:
    return s + " " * max(0, n - _width(s))


def _report(xml: str, *, raw: bool, save: str | None, only_rid: str | None) -> None:
    # ——— 危险按钮警报放在最前面：它比什么都重要 ———
    dangers = phone.danger_nodes(xml)
    if dangers:
        print()
        print("  " + "!" * 68)
        print("  !! 这一屏上有「点了有真实后果」的按钮：")
        for node in dangers:
            rid = node["rid"].split("/")[-1] if node["rid"] else "(无 rid)"
            print(f"  !!   {_pad(phone.label(node)[:20], 22)} {rid}  中心{node['center']}")
        print("  !!     —— 立即沟通 = 给候选人发消息；开放职位 = 对外公开；")
        print("  !!        立即开启 = 扣直豆；编辑/关闭职位 = 改动在跑的职位。")
        print("  !!     **本工具不会替你点它们。**")
        print("  " + "!" * 68)

    risk = phone.risk_markers(xml)
    if risk:
        print(f"\n  ⚠ 风控标记：{risk} —— 铁律 3：停手，不要继续")

    print()
    print("=== 可点元素（rid | 文字 | 中心坐标 | 大小）===")
    for node in phone.nodes(xml):
        label = phone.label(node)
        rid = node["rid"].split("/")[-1] if node["rid"] else ""
        if only_rid and only_rid not in rid:
            continue
        if not (label or rid):
            continue
        x1, y1, x2, y2 = node["bounds"]
        mark = "●" if node["clickable"] else "○"  # ○ = clickable=false
        shown = label.replace("\n", "⏎") if label else f"({node['cls']} 无文字)"
        # ★ 不截断内容。猎聘那边曾写死 [:58]，于是 543 字的 JD 只显示前 58 字，
        #   看起来像被 uiautomator 截断了，差点据此得出「读不全」的错误结论。
        #   **探查工具必须如实呈现全部内容。**
        if _width(shown) > 46:
            print(f"  {mark} {_pad(rid, 24)} {shown[:30]}… ({_width(shown)} 宽)")
            print(f"      {shown}")
        else:
            pos = f"({node['center'][0]:>4},{node['center'][1]:>4})"
            print(f"  {mark} {_pad(rid, 24)} {_pad(shown, 48)} {pos}  {x2-x1}x{y2-y1}")

    print()
    print(f"=== 节点总数 {len(phone.nodes(xml))} ===")
    print("=== 屏上文字（去重，前 30）===")
    print("  " + " | ".join(t.replace("\n", "⏎")[:36] for t in phone.texts(xml)[:30]))

    if save:
        Path(save).write_text(xml, encoding="utf-8")
        print(f"\n[probe] 原始 XML 已存到 {save}")
    if raw:
        print("\n=== 原始 XML ===")
        print(xml)


def main() -> int:
    parser = argparse.ArgumentParser(description="BOSS App 探查（只报告，不主动点）")
    parser.add_argument("--serial", help="设备序列号（多台设备时用）")
    parser.add_argument("--tab", choices=sorted(phone.TAB_NAMES), help="切到这个底部 tab")
    parser.add_argument("--tap", metavar="X,Y", help="点这个坐标（**由你决定**），再 dump")
    parser.add_argument("--back", action="store_true", help="按 BACK，再 dump")
    parser.add_argument("--rid", help="只显示 rid 含此子串的节点")
    parser.add_argument("--raw", action="store_true", help="附带打印原始 XML")
    parser.add_argument("--save", metavar="PATH", help="把原始 XML 存到文件")
    args = parser.parse_args()

    if args.serial:
        phone.use_device(args.serial)
    print(f"[probe] adb = {phone.ADB}   设备 = {phone.current_device() or '(默认唯一那台)'}")

    devs = phone.list_devices()
    if not devs:
        print("  ✗ adb 看不到任何设备。先确认手机连上了（USB 或 adb connect <ip>:5555）。")
        return 2
    print(f"[probe] 已连接：{[(d['serial'], d.get('state')) for d in devs]}")

    if args.back:
        print("[probe] BACK")
        phone.back()
    if args.tab:
        print(f"[probe] 切到「{args.tab}」tab")
        phone.goto_tab(args.tab)
    if args.tap:
        try:
            x_str, y_str = args.tap.split(",")
            point = (int(x_str), int(y_str))
        except ValueError:
            print(f"  ✗ --tap 格式应为 X,Y，收到 {args.tap!r}")
            return 2
        print(f"[probe] 点击 {point}")
        phone.tap(point, pause=3)

    print(f"[probe] 当前 Activity: {phone.current_activity()}")
    # ★ 用 dump_stable：裸 dump() 会偶发失败并返回上一次的旧文件（不报错但是错的页面）。
    _report(phone.dump_stable(), raw=args.raw, save=args.save, only_rid=args.rid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
