"""adb 原语与锚点匹配（登录 / 切换账号专用）。

从 2026-10-15 的手动演练脚本提炼而来。**只做确定性操作**：看屏幕、找元素、点、
打字、回读校验。不做任何判断——「该点哪一步」属于 flows.py，「符不符预期」由
调用方决定，本模块只负责如实报告看到了什么。

只用标准库：它跑在 Mac 上，可能由 login_watch.py（仓库 venv）或裸 python3 拉起。

## 两条用血换来的经验（都在下面有对应实现）

1. **Flutter 输入框丢弃连发退格**：一次发 40 个 KEYCODE_DEL 只生效 7 个，连发 15 个
   一个都不生效；而**单个退格有效**。所以清空优先点输入框自带的清除小×。
   （这条在原生 EditText 的系统设置搜索框上验不出来——那里没这个问题，早期版本因此漏掉。）

2. **定位按钮必须精确匹配文字**：用「包含『登录』」会匹配到页面标题「账号密码登录」，
   点了标题、白等一轮。按钮一律用 find_exact。
"""

from __future__ import annotations

import html
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

# ——— adb 定位 ———
# 不能赌 PATH：launchd 拉起时 PATH 里没有 Homebrew（run_round._resolve_pi_cli 踩过同一个坑）。
_ADB_CANDIDATES = (
    os.environ.get("HR_ADB"),
    "/opt/homebrew/bin/adb",
    str(Path.home() / "Library/Android/sdk/platform-tools/adb"),
    os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
)


def resolve_adb() -> str:
    for exe in _ADB_CANDIDATES:
        if exe and Path(exe).exists():
            return str(exe)
    return shutil.which("adb") or "adb"


ADB = resolve_adb()

DUMP_PATH = "/sdcard/hr_login.xml"

# 出现这些字样就停手——铁律 3：风控页即停，绝不尝试绕过。
RISK_MARKERS = ("图形验证码", "滑块", "拖动", "账号行为异常", "安全中心", "Web 验证码")


class AdbError(RuntimeError):
    """adb 命令失败。"""


def adb(*args: str, timeout: int = 90) -> str:
    result = subprocess.run(
        [ADB, *args], capture_output=True, text=True, timeout=timeout, errors="replace"
    )
    if result.returncode != 0 and "error" in (result.stderr or "").lower():
        raise AdbError(result.stderr.strip()[:200])
    return result.stdout or ""


def shell(*args: str, timeout: int = 90) -> str:
    return adb("shell", *[str(a) for a in args], timeout=timeout)


def dump() -> str:
    """取当前界面的 uiautomator dump。"""
    shell("uiautomator", "dump", DUMP_PATH)
    return shell("cat", DUMP_PATH)


# ——— 节点解析 ———

def nodes(xml: str) -> list[dict[str, Any]]:
    """把 dump 解析成节点列表。

    注意 `focused`：没有它就没法确认输入框真的拿到了焦点——而「点完要确认聚焦，
    清空后要重新聚焦」是今天踩出来的（隔几次 dump 后输入框会失焦，字落在空处且不报错）。
    """
    out: list[dict[str, Any]] = []
    for raw in re.findall(r"<node\b[^>]*>", xml):
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', raw))
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", attrs.get("bounds", ""))
        if not match:
            continue
        x1, y1, x2, y2 = map(int, match.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        out.append(
            {
                "cls": attrs.get("class", "").split(".")[-1],
                "text": html.unescape(attrs.get("text", "")),
                "desc": html.unescape(attrs.get("content-desc", "")),
                "focused": attrs.get("focused") == "true",
                "password": attrs.get("password") == "true",
                "clickable": attrs.get("clickable") == "true",
                "bounds": (x1, y1, x2, y2),
                "center": ((x1 + x2) // 2, (y1 + y2) // 2),
            }
        )
    return out


def label(node: dict[str, Any]) -> str:
    return (node["text"] or node["desc"]).strip()


def texts(xml: str) -> list[str]:
    """屏上所有可见文字（去重保序）。给报错信息用——「你看到了什么」比「失败了」有用得多。"""
    seen: list[str] = []
    for node in nodes(xml):
        value = label(node)
        if value and value not in seen:
            seen.append(value)
    return seen


def find(xml: str, key: str) -> dict[str, Any] | None:
    """子串匹配（用于**读**内容，不要用它定位按钮）。"""
    for node in nodes(xml):
        if key in (node["text"] + node["desc"]):
            return node
    return None


def find_exact(xml: str, text: str) -> dict[str, Any] | None:
    """精确匹配文字。

    **按钮一律用这个**：子串匹配会把标题当按钮——「账号密码登录」含「登录」，
    早期用包含匹配去点「登录」，点到了标题。
    """
    for node in nodes(xml):
        if label(node) == text:
            return node
    return None


def edittexts(xml: str) -> list[dict[str, Any]]:
    return [n for n in nodes(xml) if n["cls"] == "EditText"]


def focused_field(xml: str) -> dict[str, Any] | None:
    for node in edittexts(xml):
        if node["focused"]:
            return node
    return None


def risk_markers(xml: str) -> list[str]:
    """命中即停手的风控/验证标记。"""
    return [marker for marker in RISK_MARKERS if marker in xml]


def current_package(xml: str) -> str:
    pkgs = re.findall(r'package="([^"]+)"', xml)
    return pkgs[0] if pkgs else ""


# ——— 动作 ———

def tap(center: tuple[int, int], pause: float = 1.5) -> None:
    shell("input", "tap", str(center[0]), str(center[1]))
    time.sleep(pause)


def tap_node(node: dict[str, Any], pause: float = 1.5) -> None:
    tap(node["center"], pause)


def key(code: str, pause: float = 0.3) -> None:
    shell("input", "keyevent", code)
    time.sleep(pause)


def back() -> None:
    """对对话框，BACK 等价于取消且没有坐标风险——比点「取消」安全。"""
    key("KEYCODE_BACK", pause=1.5)


def home() -> None:
    key("KEYCODE_HOME", pause=1.0)


def open_app(package: str, settle: float = 6.0) -> None:
    shell("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(settle)


# 底部导航的三个 tab。**出现两个以上**才算「在主界面」——只认一个会被
# 页面里碰巧出现的同名文字骗过。
MAIN_TAB_MARKERS = ("推荐", "职位", "我的")


def ensure_main_tabs(package: str, max_back: int = 5, settle: float = 6.0) -> dict[str, Any]:
    """退到带底部导航的主界面。

    固定序列都假设 App 在**主界面**上，但手机可能停在任何页面——会话页、候选人详情页、
    职位页……那是上一轮留下的，不可预测。

    2026-10-15 第一次真机运行就栽在这儿：`logout()` 上来就点「我的」tab 的坐标 (945,2122)，
    而 App 被上一轮留在了**会话页**，那里没有底部导航，那一下打到了别处。

    ★ **每一步都要确认还在目标 App 里**：登录页、协议页、验证码页**都没有底部导航**，
      盲按 BACK 会一路退出 App 退到桌面——那样「退不回主界面」这个结论就是假的，
      真相是「本来就不该退」。所以一旦发现离开了目标包，立刻重新打开并如实返回。
    """
    home()
    open_app(package, settle=settle)
    for attempt in range(max_back + 1):
        xml = dump()
        if package not in current_package(xml):
            # BACK 把 App 退掉了 —— 说明它原本就在一个没有底部导航的页面（登录页/协议页…）
            return {
                "ok": False,
                "left_app": True,
                "seen": texts(xml),
                "hint": "按 BACK 退出了 App，说明它本来就不在主界面（可能是登录页/协议页/验证码页）",
            }
        marks = [m for m in MAIN_TAB_MARKERS if find_exact(xml, m)]
        if len(marks) >= 2:
            return {"ok": True, "attempts": attempt, "marks": marks}
        back()
    return {"ok": False, "seen": texts(dump())}


def clear_button_near(xml: str, field: dict[str, Any]) -> tuple[int, int] | None:
    """输入框右侧那个清除小×。

    它是 Flutter 的 ImageView，**无文字且 clickable=false**，不能按 clickable 过滤，
    只能按「与输入框垂直重叠、且在右边缘附近」找。
    """
    x1, y1, x2, y2 = field["bounds"]
    for node in nodes(xml):
        if node["cls"] != "ImageView":
            continue
        cx, cy = node["center"]
        if x1 < cx <= x2 + 130 and y1 - 40 <= cy <= y2 + 40:
            return (cx, cy)
    return None


def field_at(xml: str, box: tuple[int, int]) -> dict[str, Any] | None:
    """坐标落在哪个输入框上。"""
    for f in edittexts(xml):
        x1, y1, x2, y2 = f["bounds"]
        if x1 <= box[0] <= x2 and y1 <= box[1] <= y2:
            return f
    return None


def clear_field(field_box: tuple[int, int]) -> bool:
    """清空输入框；返回是否确实空了。

    ★ 判「空」不能只看 text 非空：**有些输入框把占位提示渲染成 text**
      （Android 设置页的搜索框空着时 text='搜索设置项'）。所以还要看
      「清空动作有没有让文字变化」——删了一轮一个字都没少，那它就不是真内容。
      这类字段上如果死盯着「清到空」，会永远清不完，后面的输入根本走不到。
    """
    prev: str | None = None
    for _ in range(3):
        xml = dump()
        field = field_at(xml, field_box)
        if field is None:
            return False
        if not field["text"]:
            return True
        if prev is not None and prev == field["text"]:
            return True  # 上一轮清空没让文字变化 → 是占位提示，当作空
        prev = field["text"]
        clear = clear_button_near(xml, field)
        if clear:
            tap(clear, pause=1.2)
            continue
        # 没有×才退回退格——而且要**单个慢速**发，连发会被 Flutter 丢掉
        key("KEYCODE_MOVE_END", pause=0.2)
        for _ in range(14):
            key("KEYCODE_DEL", pause=0.3)
    field = field_at(dump(), field_box)
    return bool(field) and not field["text"]


def type_into(field_box: tuple[int, int], text: str, *, verify: bool = True) -> dict[str, Any]:
    """点 → 确认聚焦 → 清空 → **重新聚焦** → 输入 → 回读。

    ★ 「清空后重新聚焦」不能省：清空要 dump 好几次（约 8 秒），输入框会失焦；
      不重新点就 input text，字会落在空处——**而且不报错，读回是空串**。

    `verify=False` 用于密码框（password=true 读回来恒为空，回读校验无意义）。
    """
    got: str | None = None
    for attempt in range(4):
        tap(field_box, pause=1.8)
        xml = dump()
        field = field_at(xml, field_box)
        if field is None:
            continue
        if not field["focused"]:
            continue
        if field["text"] and field["text"] != text:
            if not clear_field(field_box):
                continue
            tap(field_box, pause=1.8)  # ★ 清空后重新聚焦
        shell("input", "text", shlex.quote(text.replace(" ", "%s")))
        time.sleep(1.5)
        if not verify:
            return {"ok": True, "attempt": attempt + 1, "readback": None}
        got = None
        target = field_at(dump(), field_box)
        if target is not None:
            got = target["text"]
        if got == text:
            return {"ok": True, "attempt": attempt + 1, "readback": got}
    return {"ok": False, "readback": got, "seen": texts(dump())}


def tick_agreement(xml: str, text: str = "我已阅读并同意") -> bool:
    """勾选协议。方框在文字左侧，所以取文字左边约 169px 处。"""
    node = find(xml, text)
    if not node:
        return False
    x, y = node["center"]
    tap((max(x - 169, 60), y), pause=1.5)
    return True
