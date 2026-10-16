"""adb 原语与锚点匹配（**猎聘与 BOSS 共用**）。

从 `agent/phone.py` 抽出来的通用层（2026-10-16，为 BOSS 执行端铺路）。
**只做确定性操作**：看屏幕、找元素、点、打字、回读校验。不做任何判断——
「该点哪一步」属于各自的 `flows.py`，「符不符预期」由调用方决定，
本模块只负责如实报告看到了什么。

## ★ 为什么是普通模块而不是 workspace package

`agent/` 与 `agent_boss/` **都不是 uv workspace 成员**，
而且这段代码**可能被裸 python3 拉起**（launchd 拉起时 PATH 里没有 venv）。
所以本模块**只用标准库**、靠调用方 `sys.path.insert` 引入，
**不能依赖 venv 里装的任何东西**。改动时请守住这条。

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

#: 手机上存 dump 的路径。★ 两套执行端（猎聘 / BOSS）**同时跑时会互相覆盖**，
#: 所以各自的入口都该调 `set_dump_path()` 换成自己的（见 agent_boss/phone.py）。
DUMP_PATH = "/sdcard/hr_login.xml"


def set_dump_path(path: str) -> None:
    global DUMP_PATH
    DUMP_PATH = path

# 出现这些字样就停手——铁律 3：风控页即停，绝不尝试绕过。
RISK_MARKERS = ("图形验证码", "滑块", "拖动", "账号行为异常", "安全中心", "Web 验证码")


# ——— 设备选择（2026-10-16 新增） ———

#: 选中的设备序列号。None = 不传 `-s`，作用于唯一连接的那台（= 过去的行为）。
_DEVICE: str | None = os.environ.get("ANDROID_SERIAL") or None


def use_device(serial: str | None) -> None:
    """选定后续所有 adb 命令作用的设备。

    ★ 这是为「多台设备」铺路的（BOSS 那边计划一台模拟器一个号）。
    不调用它就完全等于以前的行为——不传 `-s`，交给 adb 自己挑唯一那台。
    """
    global _DEVICE
    _DEVICE = serial or None


def current_device() -> str | None:
    return _DEVICE


def list_devices() -> list[dict[str, str]]:
    """`adb devices -l` 的解析结果。用来判断手机连没连上。

    返回 [{"serial": ..., "state": "device"/"offline"/"unauthorized", "model": ...}]。
    ★ 注意区分：`offline` / `unauthorized` 说明**设备在，但没授权**，
    跟「没连上」是两回事，处理方式也不同。
    """
    out = adb("devices", "-l")
    found: list[dict[str, str]] = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        item = {"serial": parts[0], "state": parts[1] if len(parts) > 1 else ""}
        for token in parts[2:]:
            if ":" in token:
                key, _, value = token.partition(":")
                item[key] = value
        found.append(item)
    return found


class AdbError(RuntimeError):
    """adb 命令失败。"""


def adb(*args: str, timeout: int = 90) -> str:
    cmd = [ADB]
    if _DEVICE:
        cmd += ["-s", _DEVICE]
    cmd += list(args)
    # ★ 必须显式 encoding="utf-8"：手机回来的 dump 是 UTF-8，而 Windows 上
    #   `text=True` 会按本地代码页（cp936）解码 → 所有中文变成「鎺ㄨ崘」这种乱码，
    #   于是**每个文字锚点都匹配不上**，且不报错。macOS 默认就是 UTF-8，所以
    #   这个坑只在 Windows 上暴露（2026-10-16 跑云机探针时踩到）。
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        errors="replace",
    )
    if result.returncode != 0 and "error" in (result.stderr or "").lower():
        raise AdbError(result.stderr.strip()[:200])
    return result.stdout or ""


def shell(*args: str, timeout: int = 90) -> str:
    return adb("shell", *[str(a) for a in args], timeout=timeout)


def reconnect(serial: str | None = None, pause: float = 2.0) -> bool:
    """重连无线 adb。**只对 `host:port` 形式的 serial 有意义。**

    ★ 2026-10-16 实测：这台手机的无线 adb 会**偶发抖动**，一条命令报
      `error: closed`，紧接着又完全正常。长流程（一次扫职位要跑几十条命令）
      撞上一次就整个挂掉，所以要么能重连、要么能重试——两个都给了。
    """
    target = serial or _DEVICE
    if not target or ":" not in target:
        return False
    try:
        adb("connect", target, timeout=15)
        time.sleep(pause)
        return any(d["serial"] == target and d["state"] == "device" for d in list_devices())
    except (AdbError, OSError):
        return False


def ensure_connected(*, pause: float = 2.0, tries: int = 3) -> bool:
    """确保 `_DEVICE`（形如 `host:port` 的网络设备）此刻真的在连着。

    ★ 为什么需要它：`adb connect` 是**一次性**的——adb server 重启、设备掉线、
      或云手机客户端重开之后，那条连接**不会自己回来**，而后续每条命令都会失败。
      进程启动时调一次，比每条命令都祈祷它还在要可靠。

    非网络设备（USB 序列号）直接返回 True：本函数不管 USB，那由系统负责。
    ★ 没设 `_DEVICE` 时同样返回 True——「不传 -s、交给 adb 挑唯一那台」是既有行为。
    """
    target = _DEVICE
    if not target or ":" not in target:
        return True
    for _ in range(tries):
        if any(d["serial"] == target and d["state"] == "device" for d in list_devices()):
            return True
        if reconnect(target, pause=pause):
            return True
    return False


def discover_cloud_phone(process_name: str = "ddyun") -> str | None:
    """在 Windows 上找出云手机客户端**当前**暴露 adb 的那个端口。

    ★ 为什么需要它：多多云的 adb 端口**不固定**。2026-10-16 一次会话里实测它就换了
      三次（63833 → 51639 → 51672）——每结束一次 adb 会话就换一个。所以**任何地方
      写死端口都会静默失效**，只能每次重新发现。（同一台机器上还有一个一直不变的
      64343，那个不是 adb。）

    做法：`netstat -ano` 拿本机监听端口 → `tasklist` 拿客户端 PID → 取二者交集里
    的 127.0.0.1 端口。返回 `"127.0.0.1:<端口>"`，找不到返回 None。

    非 Windows 返回 None：云机只在 Windows 上跑，别的平台不需要它。
    """
    if os.name != "nt":
        return None
    try:
        listing = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
        ).stdout
        tasks = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {process_name}.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    pids = {m.group(1) for m in re.finditer(r'"[^"]+","(\d+)"', tasks)}
    if not pids:
        return None
    ports: list[int] = []
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[-2] != "LISTENING" or parts[-1] not in pids:
            continue
        match = re.fullmatch(r"127\.0\.0\.1:(\d+)", parts[1])
        if match:
            ports.append(int(match.group(1)))
    if not ports:
        return None
    # 有多个就按端口从大到小试——实测 adb 那个端点总是较大的那个（小的那个 refuse）
    for port in sorted(ports, reverse=True):
        target = f"127.0.0.1:{port}"
        try:
            # 裸调 adb（不走 adb()）：connect 不该被 _DEVICE 污染成 `-s X connect`
            output = subprocess.run(
                [ADB, "connect", target],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            ).stdout or ""
        except (OSError, subprocess.SubprocessError):
            continue
        time.sleep(1.0)
        if any(d["serial"] == target and d["state"] == "device" for d in list_devices()):
            return target
        # 明确连不上（英文 "failed to connect" / 中文 "cannot connect to ... 积极拒绝"）
        # → 清掉这个假条目，否则 adb devices 里会越积越多 offline。
        # **只清明确失败的**：连上了只是还没就绪的情况不能断，那会误杀真端点。
        if "cannot connect" in output or "failed" in output.lower():
            try:
                subprocess.run(
                    [ADB, "disconnect", target],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                )
            except (OSError, subprocess.SubprocessError):
                pass
    return None


def dump(retries: int = 3, pause: float = 1.5) -> str:
    """取当前界面的 uiautomator dump。

    ★ 会重试瞬时故障（`error: closed` 这类无线 adb 抖动）。
      **只在这里重试是因为 dump 是幂等的**——`input tap` 绝不能重试，
      重试会变成连点两下。所以重试逻辑只放在读命令上。

    ★★ 每次**先删掉上一份 dump 文件**（2026-10-16 云机实测踩到）：
      `uiautomator dump` 失败时**不会覆盖**旧文件，`cat` 于是把**上一屏**当成
      这一屏返回——不报错，但是错的页面。云机上这一下骗过了我：截图明明是消息
      列表，dump 却返回 PDF 查看器的层级（11 个节点，刚好越过 dump_stable 的
      min_nodes=10 门槛，于是被当成「像样的结果」收下了）。
      先删掉旧文件，失败时就拿不到任何东西 → 空结果 → 重试，而不是拿旧屏当真相。
    """
    last: Exception | None = None
    for attempt in range(retries):
        try:
            shell("rm", "-f", DUMP_PATH)
            shell("uiautomator", "dump", DUMP_PATH)
            xml = shell("cat", DUMP_PATH)
        except AdbError as exc:
            last = exc
        else:
            if xml.strip():
                return xml
            last = AdbError("uiautomator dump 没有产出内容（旧文件已先删，不会是旧屏）")
        if attempt == retries - 1:
            break
        time.sleep(pause)
        reconnect()
    raise last  # type: ignore[misc]


def dump_stable(tries: int = 5, pause: float = 3.0, min_nodes: int = 10) -> str:
    """反复 dump 直到拿到像样的结果。

    ★ 为什么需要它（2026-10-16 BOSS 实测踩到）：`uiautomator dump` 会**偶发失败**
    （「could not get idle state」，页面有动画时尤其容易），此时
    `cat` 拿到的是**上一次的旧文件**——不报错，但是错的页面。
    用旧 dump 做判断会得出完全相反的结论：我的盯梢脚本就因此把
    「dump 失败」误判成了「职位已开放」，还据此报了错。
    凡是**要拿 dump 做决策**的地方，都该用它，而不是裸 dump()。
    """
    xml = ""
    for _ in range(tries):
        try:
            xml = dump()
        except AdbError:
            # 连接抖到 dump() 自己的重试都用完了——再给它一次机会
            reconnect()
            time.sleep(pause)
            continue
        if len(re.findall(r"<node\b", xml)) >= min_nodes:
            return xml
        time.sleep(pause)
    return xml


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
                "rid": attrs.get("resource-id", ""),  # ★ 原生 App 有，Flutter 没有
                "text": html.unescape(attrs.get("text", "")),
                "desc": html.unescape(attrs.get("content-desc", "")),
                "focused": attrs.get("focused") == "true",
                "password": attrs.get("password") == "true",
                "clickable": attrs.get("clickable") == "true",
                "checked": attrs.get("checked") == "true",
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


def find_rid(xml: str, rid: str, *, suffix: bool = True) -> dict[str, Any] | None:
    """按 resource-id 找节点（**原生 App 优先用这个**，比文字稳）。

    `suffix=True`（默认）时按后缀匹配——因为 dump 里是
    `com.hpbr.bosszhipin:id/tv_title` 这种全限定名，按后缀找不用写死包名。
    """
    for node in nodes(xml):
        node_rid = node["rid"]
        if not node_rid:
            continue
        if (suffix and node_rid.endswith(rid)) or (not suffix and node_rid == rid):
            return node
    return None


def find_rid_text(xml: str, rid: str, text: str) -> dict[str, Any] | None:
    """rid 与文字**双重校验**后再返回。

    ★ 2026-10-16 加的，为了防一类具体的事故：
    BOSS 的发布表单里，`btn_confirm` 与 `btn_reset` 并排，文字分别是
    「开放职位」与「编辑」——**只按 rid 找会点错**（前者点下去是付费/发布，
    后者是改在跑的职位）。只按文字找则会被同名文字骗。
    两个条件都满足才返回，找不到就老实返回 None（调用方应该据此放弃动作）。
    """
    for node in nodes(xml):
        if node["rid"].endswith(rid) and label(node) == text:
            return node
    return None


def edittexts(xml: str) -> list[dict[str, Any]]:
    return [n for n in nodes(xml) if n["cls"] == "EditText"]


def focused_field(xml: str) -> dict[str, Any] | None:
    for node in edittexts(xml):
        if node["focused"]:
            return node
    return None


def risk_markers(xml: str, markers: tuple[str, ...] = RISK_MARKERS) -> list[str]:
    """命中即停手的风控/验证标记。"""
    return [marker for marker in markers if marker in xml]


def current_package(xml: str) -> str:
    pkgs = re.findall(r'package="([^"]+)"', xml)
    return pkgs[0] if pkgs else ""


def current_activity() -> str:
    """当前前台 Activity（全限定名）。给「我到底走到哪一页了」用。"""
    out = shell("dumpsys", "window")
    match = re.search(r"mCurrentFocus=Window\{[^}]*?\s+u0\s+(\S+?)\}", out)
    return match.group(1) if match else ""


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


def swipe(
    start: tuple[int, int],
    end: tuple[int, int],
    duration_ms: int = 400,
    pause: float = 1.5,
) -> None:
    """滑动。列表翻页用。

    ★ **WebView 不吃 uiautomator 的滑动**（见 login_map 的「★通用教训」）——
      猎聘「我的」页就是 WebView。原生列表（职位管理页）可以。
      所以滑完必须 dump 确认内容真的变了，不能假设它动了。
    """
    shell(
        "input", "swipe",
        str(start[0]), str(start[1]), str(end[0]), str(end[1]), str(duration_ms),
    )
    time.sleep(pause)


def _locate_by_text(markers: tuple[str, ...]):
    def locate(xml: str) -> list[str]:
        return [m for m in markers if find_exact(xml, m)]

    return locate


def ensure_main_tabs(
    package: str,
    markers: tuple[str, ...] | None = None,
    max_back: int = 5,
    settle: float = 6.0,
    *,
    locate: Any = None,
) -> dict[str, Any]:
    """退到带底部导航的主界面。

    固定序列都假设 App 在**主界面**上，但手机可能停在任何页面——会话页、候选人详情页、
    职位页……那是上一轮留下的，不可预测。

    2026-10-15 第一次真机运行就栽在这儿：`logout()` 上来就点「我的」tab 的坐标 (945,2122)，
    而 App 被上一轮留在了**会话页**，那里没有底部导航，那一下打到了别处。

    ★ **每一步都要确认还在目标 App 里**：登录页、协议页、验证码页**都没有底部导航**，
      盲按 BACK 会一路退出 App 退到桌面——那样「退不回主界面」这个结论就是假的，
      真相是「本来就不该退」。所以一旦发现离开了目标包，立刻重新打开并如实返回。

    `markers` 是底部 tab 的文字（猎聘 `("推荐","职位","我的")`）。
    ★ **出现两个以上**才算「在主界面」——只认一个会被页面里碰巧出现的同名文字骗过。

    ★ BOSS 要用 `locate=`：它的 tab 文字节点 bounds 是 `[0,0][0,0]`（标签被隐藏），
      而本模块的 `nodes()` **会跳过零尺寸节点**，所以按文字永远找不到——
      必须换成按 rid 判定（`find_rid`）。
    """
    finder = locate or _locate_by_text(markers or ())
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
        marks = finder(xml)
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
