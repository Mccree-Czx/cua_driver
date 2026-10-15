#!/usr/bin/env python3
"""职位管理页探查工具（2026-10-15）。

**用途只有一个：找「编辑职位」的入口。** JD（打分唯一依据）就在那个表单的
「职位描述」字段里，而入口至今没找到。三条已排除：点职位卡（进的是候选人数据页）、
点「发职位」（那是空白新建表单）、职位管理页本身。

## 为什么是「人决定点哪，机器只负责看」

这是在**真实招聘账号**上操作，乱试的代价可能是**误发布一个职位**。
所以本脚本**自己绝不主动点任何东西**——只有你明确给 `--tap x,y` 它才点。
每一步都 dump 给你看，点错了立刻 `--back` 退回来。

## 用法

    python agent/probe_jobs.py                # 导航到职位管理页并 dump 可点元素
    python agent/probe_jobs.py --no-nav       # 不导航，直接 dump 当前屏
    python agent/probe_jobs.py --tap 540,930  # 点一下，再 dump
    python agent/probe_jobs.py --back         # BACK，再 dump
    python agent/probe_jobs.py --raw          # 附带打印原始 XML（很长）
    python agent/probe_jobs.py --save /tmp/x  # 把原始 XML 存下来

找到入口后，把路径写进 `login_map.json` 的「职位扫描」段，并把 `probe.at` 改成当天。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import flows  # noqa: E402
import phone  # noqa: E402

PKG = flows.PKG


def _navigate() -> None:
    """把手机带到「职位管理 → 招聘中」。走的是 scan_jobs() 用的同一批锚点。"""
    m = flows.load_map()["职位扫描"]
    print("[probe] 打开猎聘并退到主界面…")
    result = phone.ensure_main_tabs(PKG)
    if not result["ok"]:
        print(f"  ✗ 退不回主界面：{result}")
        return

    xml = phone.dump()
    tab = phone.find_exact(xml, m["phone_tab"]["match"])
    if not tab:
        print("  ✗ 底部没有「职位」tab，屏上：" + str(phone.texts(xml)[:20]))
        return
    phone.tap_node(tab, pause=4.5)

    xml = phone.dump()
    flows._guard_publish_form(xml)  # 万一落到新建表单
    joined = " ".join(phone.texts(xml))
    if not all(marker in joined for marker in flows._JOB_PAGE_MARKERS):
        print("  ✗ 没进到职位管理页，屏上：" + str(phone.texts(xml)[:25]))
        return

    recruiting = flows._find_startswith(xml, "招聘中")
    if recruiting:
        phone.tap_node(recruiting, pause=3)
    print("[probe] 已在「职位管理 → 招聘中」")


def _report(xml: str, *, raw: bool, save: str | None) -> None:
    texts = phone.texts(xml)
    joined = " ".join(texts)

    # 危险页警报放在最前面——它比什么都重要
    hit = [m for m in flows._PUBLISH_FORM_MARKERS if m in joined]
    if hit:
        print()
        print("  " + "!" * 66)
        print(f"  !! 这是「发职位」的空白新建表单（看到 {hit}）")
        print("  !! **提交了就是真的对外发布一个职位**。要离开请按 `--back`。")
        print("  " + "!" * 66)

    risk = phone.risk_markers(xml)
    if risk:
        print(f"  ⚠ 风控标记：{risk} —— 停手，不要继续")

    print()
    print("=== 可点元素（文字 | 中心坐标 | 大小）===")
    for node in phone.nodes(xml):
        label = phone.label(node)
        x1, y1, x2, y2 = node["bounds"]
        mark = "●" if node["clickable"] else "○"  # ○ = clickable=false，Flutter 图标常是这样
        # ★ 不截断。曾经这里写死 [:58]，于是「职位介绍」那段 543 字的 JD 只显示
        #   前 58 字，看起来像被 uiautomator 截断了——差点据此得出「详情页读不全」
        #   的错误结论，还去试了有副作用的编辑表单。**探查工具必须如实呈现全部内容。**
        if label:
            shown = label.replace("\n", "⏎")
        else:
            shown = f"({node['cls']} 无文字)"
        if len(shown) > 58:
            print(f"  {mark} {shown[:58]}… (共 {len(shown)} 字)")
            print(f"      {shown}")
        else:
            print(f"  {mark} {shown:<60} ({node['center'][0]:>4},{node['center'][1]:>4})  {x2-x1}x{y2-y1}")

    print()
    print("=== 屏上文字（去重）===")
    print("  " + " | ".join(t.replace("\n", "⏎")[:40] for t in texts[:40]))

    if save:
        Path(save).write_text(xml, encoding="utf-8")
        print(f"\n[probe] 原始 XML 已存到 {save}")
    if raw:
        print("\n=== 原始 XML ===")
        print(xml)


def main() -> int:
    parser = argparse.ArgumentParser(description="职位管理页探查（只报告，不主动点）")
    parser.add_argument("--no-nav", action="store_true", help="不导航，直接 dump 当前屏")
    parser.add_argument("--tap", metavar="X,Y", help="点这个坐标（**由你决定**），再 dump")
    parser.add_argument("--back", action="store_true", help="按 BACK，再 dump")
    parser.add_argument("--raw", action="store_true", help="附带打印原始 XML")
    parser.add_argument("--save", metavar="PATH", help="把原始 XML 存到文件")
    args = parser.parse_args()

    print(f"[probe] adb = {phone.ADB}")
    if not args.no_nav and not args.tap and not args.back:
        _navigate()

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

    _report(phone.dump(), raw=args.raw, save=args.save)
    return 0


if __name__ == "__main__":
    sys.exit(main())
