"""Windows 平台原语。

角色名取 UIA 标准 ControlType（微软 UI Automation 规范）。真实树上的实际取值
以阶段 2 的页面校准为准 —— 本机已实测到的角色为 Button / Edit / Pane / Document。

坐标点击与热键走 SDK 原生能力（ClickPosition.COORDINATES / HotkeyInput），
不依赖 macOS 的 osascript；窗口激活走 SDK call_tool("bring_to_front")。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

from .base import (  # noqa: F401
    CARD_STATUS_RE,
    SALARY_RE,
    Role,
    WindowRef,
    element_center,
)

WINDOWS_ROLE_MAP: dict[Role, str] = {
    Role.TEXT: "Text",
    Role.BUTTON: "Button",
    Role.RADIO: "RadioButton",
    Role.CHECKBOX: "CheckBox",
    Role.IMAGE: "Image",
    Role.LINK: "Hyperlink",
    Role.TEXT_INPUT: "Edit",
    Role.TEXT_AREA: "Edit",
    Role.WEB_AREA: "Document",
    Role.TAB: "TabItem",
}

# 浏览器进程名（小写比较；本机实测 chrome.exe / msedge.exe）
BROWSER_EXES = ("chrome.exe", "msedge.exe", "chromium.exe", "firefox.exe")

# 地址栏元素判据（2026-10-09 本机实测 chrome.exe：role=Edit, label="地址和搜索栏"）
ADDRESS_LABEL_MARKERS = ("地址", "address")


def _win_ref(w: Any) -> WindowRef:
    return WindowRef(
        pid=int(getattr(w, "pid", 0) or 0),
        window_id=int(getattr(w, "window_id", 0) or 0),
        app_name=str(getattr(w, "app_name", "") or ""),
        title=str(getattr(w, "title", "") or ""),
        is_on_screen=bool(getattr(w, "is_on_screen", False)),
    )


def _system_dpi_scale() -> float:
    """系统 DPI 缩放（96 DPI = 1.0）。读不到时回退 1.0。

    截图是物理像素、点击走逻辑坐标 —— 二者之比必须取自系统 DPI，
    沿用 macOS 的 Retina 值 2.0 会让兜底坐标点击系统性打偏。
    """
    try:
        import ctypes

        dpi = int(ctypes.windll.user32.GetDpiForSystem())
        if dpi > 0:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def _top_level_windows(pid: int) -> list[tuple[int, bool]]:
    """该 pid 的可见顶层窗口 [(hwnd, 是否最小化)]；读不到时返回 []。"""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found: list[tuple[int, bool]] = []

        def cb(hwnd, _lparam):
            wpid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid and user32.IsWindowVisible(hwnd):
                found.append((hwnd, bool(user32.IsIconic(hwnd))))
            return True

        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(proc(cb), 0)
        return found
    except Exception:
        return []


def _window_is_minimized(pid: int) -> bool:
    return any(minimized for _, minimized in _top_level_windows(pid))


def _restore_window(pid: int) -> None:
    """还原并前置该 pid 的最小化顶层窗口。

    SDK 的 bring_to_front 不还原最小化状态，而最小化窗口的元素点击会报
    「window … is minimized」——必须先显式 SW_RESTORE。
    """
    try:
        import ctypes

        user32 = ctypes.windll.user32
        for hwnd, minimized in _top_level_windows(pid):
            if minimized:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


class WindowsAdapter:
    """Windows 原语实现。`bridge` 驱动 SDK 协程，`driver` 用于 click / hotkey / call_tool。"""

    name = "windows"
    primary_modifier = "ctrl"

    def __init__(self, bridge: Any = None, driver: Any = None) -> None:
        self._bridge = bridge
        self._driver = driver
        self.screenshot_px_per_point = _system_dpi_scale()

    def role_name(self, role: Role) -> str:
        return WINDOWS_ROLE_MAP[role]

    def tab_elements(self, state: Any) -> list[tuple[str, Any]]:
        """批量页候选人选项卡 [(姓名, 元素)]。

        实测结构（2026-10-09 本机 Windows）：选项卡是顶层 Document 的直接子级
        Group，每个 Group 内第一个 Text 为姓名（后随一个无 label 的 Image）；
        到第一个嵌套 Document（详情 iframe）为止；账户名「陈智旭」排除。
        """
        doc = WINDOWS_ROLE_MAP[Role.WEB_AREA]
        text = WINDOWS_ROLE_MAP[Role.TEXT]
        els = list(getattr(state, "elements", []) or [])
        top = next((e for e in els if str(getattr(e, "role", "")) == doc), None)
        if top is None:
            return []
        # 实测：顶层 Document 的直接子级只有一个 Group，选项卡区与详情 iframe
        # 都在该 Group 之下 —— 需要下钻一层。
        kids = [e for e in els if getattr(e, "parent_index", None) == top.element_index]
        if len(kids) == 1 and str(getattr(kids[0], "role", "")) == "Group":
            container = kids[0]
        else:
            container = top
        out: list[tuple[str, Any]] = []
        seen: list[str] = []
        for e in els:
            if getattr(e, "parent_index", None) != container.element_index:
                continue
            role = str(getattr(e, "role", ""))
            if role == doc:
                break  # 详情 iframe：选项卡区结束
            if role != "Group":
                continue
            for child in els:
                if getattr(child, "parent_index", None) != e.element_index:
                    continue
                if str(getattr(child, "role", "")) != text:
                    continue
                lbl = str(getattr(child, "label", "") or "").strip()
                if lbl and lbl != "陈智旭" and lbl not in seen:
                    seen.append(lbl)
                    out.append((lbl, child))
                break
        return out

    def field_value(self, state: Any, icon: str) -> str | None:
        """图标锚点后的首个文本值。

        实测（2026-10-09）：字段图标 Image（environment/work/education…）只出现在
        tree_markdown（该行无 [N] 索引前缀），不进 elements 列表 —— 故从树文本解析。
        """
        tree = str(getattr(state, "tree_markdown", "") or "")
        m = re.search(
            rf'Image "{re.escape(icon)}"[^\n]*\n\s*-\s*(?:\[\d+\]\s*)?Text "([^"]*)"',
            tree,
        )
        if m:
            value = m.group(1).strip()
            return value or None
        return None

    def recommend_cards(self, state: Any) -> list[tuple[str, Any | None]]:
        """推荐页卡片 [(姓名, 姓名元素或 None)]。

        实测（2026-10-09）：「头像」Image 不进 elements，故改用 Button「立即沟通」
        作卡片锚点（每卡一个、在 elements 内）；姓名取该按钮所在 Group 的直接子级
        里、跳过状态词与空文本后的首个 Text。
        """
        btn_role = WINDOWS_ROLE_MAP[Role.BUTTON]
        text_role = WINDOWS_ROLE_MAP[Role.TEXT]
        els = list(getattr(state, "elements", []) or [])
        start = 0
        for pos, e in enumerate(els):
            if str(getattr(e, "label", "") or "") == "系统推荐":
                start = pos
                break
        out: list[tuple[str, Any | None]] = []
        seen: set[int] = set()
        for e in els[start:]:
            if str(getattr(e, "role", "")) != btn_role:
                continue
            if str(getattr(e, "label", "") or "") not in ("立即沟通", "向TA索要"):
                continue
            card_idx = getattr(e, "parent_index", None)
            if card_idx is None or card_idx in seen:
                continue
            seen.add(card_idx)
            name_el = None
            for child in els:
                if getattr(child, "parent_index", None) != card_idx:
                    continue
                if str(getattr(child, "role", "")) != text_role:
                    continue
                lbl = str(getattr(child, "label", "") or "").strip()
                if not lbl or CARD_STATUS_RE.match(lbl):
                    continue
                name_el = child
                break
            name = str(getattr(name_el, "label", "") or "").strip() if name_el is not None else ""
            out.append((name, name_el))
        return out

    def salary(self, state: Any) -> str:
        """求职意向里的薪资项。

        实测（2026-10-09）：薪资只出现在 tree_markdown 的 ListItem label
        （如 ListItem "海外销售16-35k×12薪中国、上海…"），不进 elements 列表。
        """
        tree = str(getattr(state, "tree_markdown", "") or "")
        i = tree.find("求职意向")
        if i < 0:
            return ""
        m = re.search(r'ListItem "([^"]*)"', tree[i : i + 600])
        if m is None:
            return ""
        sm = SALARY_RE.search(m.group(1))
        return sm.group(0) if sm else ""

    def candidate_windows(self, windows: Sequence[Any]) -> list[WindowRef]:
        return [
            _win_ref(w)
            for w in windows
            if str(getattr(w, "app_name", "") or "").lower() in BROWSER_EXES
        ]

    def url_of(self, state: Any) -> str:
        for e in getattr(state, "elements", []) or []:
            if str(getattr(e, "role", "")) != "Edit":
                continue
            label = str(getattr(e, "label", "") or "")
            if any(m in label for m in ADDRESS_LABEL_MARKERS):
                return str(getattr(e, "value", "") or "")
        return ""

    def is_on_screen(self, windows: Sequence[Any], pid: int, window_id: int) -> bool:
        # 最小化窗口 SDK 仍报 is_on_screen=True，会误导 ensure_visible 直接返回；
        # 这里显式降级为不可见，好让 ensure_visible 走还原路径。
        if _window_is_minimized(pid):
            return False
        for w in windows:
            if getattr(w, "pid", None) == pid and getattr(w, "window_id", None) == window_id:
                return bool(getattr(w, "is_on_screen", False))
        return False

    def activate(self, windows: Sequence[Any], pid: int) -> None:
        """激活窗口：先 Win32 还原最小化，再 SDK bring_to_front（best-effort）。"""
        if not pid:
            return
        _restore_window(pid)
        self._call_tool_silent("bring_to_front", {"pid": pid})

    def raise_window(self, windows: Sequence[Any], pid: int, window_id: int) -> bool:
        """精确前置；返回「调用是否成功」，可见性判定由调用方重新枚举。"""
        if not pid or not window_id:
            return False
        _restore_window(pid)
        return self._call_tool_silent("bring_to_front", {"pid": pid, "window_id": window_id})

    def click_element(self, pid: int, window_id: int, element: Any) -> None:
        """元素级点击（Windows）：直接用 SDK 的 element_token。

        坐标点击在 Windows 上不可用 —— 窗口截图报 "capture binding is invalid"，
        而 SDK 要求「本会话拥有的截图」才放行像素坐标。
        """
        from cua_driver import ActionTarget, ClickInput, ClickPosition, InputDeliveryMode

        try:
            self._bridge.run(
                self._driver.click(
                    ClickInput(
                        target=ActionTarget.WINDOW(pid, window_id),
                        position=ClickPosition.ELEMENT(element.element_token),
                        delivery_mode=InputDeliveryMode.BACKGROUND,
                        session=None,
                        button=None,
                        count=None,
                    )
                )
            )
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"元素点击失败（Windows/SDK）：{e}") from e

    def click_point(self, pid: int, window_id: int, x: float, y: float) -> None:
        """坐标点击（SDK 原生 ClickPosition.COORDINATES；取代 osascript）。

        Windows 要求目标窗口先有「本会话拥有的截图快照」，否则 SDK 报
        "does not contain a screenshot owned by this session" —— 故先补拍一次。
        """
        from cua_driver import ActionTarget, ClickInput, ClickPosition, InputDeliveryMode

        self._snapshot_window(pid, window_id)
        try:
            self._bridge.run(
                self._driver.click(
                    ClickInput(
                        target=ActionTarget.WINDOW(pid, window_id),
                        position=ClickPosition.COORDINATES(x, y),
                        delivery_mode=InputDeliveryMode.BACKGROUND,
                        session=None,
                        button=None,
                        count=None,
                    )
                )
            )
        except Exception as e:  # noqa: BLE001 - 统一为 RuntimeError，与 macOS 侧一致
            raise RuntimeError(f"坐标点击失败（Windows/SDK）：{e}") from e

    def _snapshot_window(self, pid: int, window_id: int) -> None:
        """给目标窗口拍一次本会话截图（坐标点击的前置条件）；失败静默。"""
        import tempfile

        from cua_driver import GetWindowStateInput

        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        out = Path(tmp.name)
        try:
            self._bridge.run(
                self._driver.get_window_state(
                    GetWindowStateInput(
                        pid=pid,
                        window_id=window_id,
                        session=None,
                        query=None,
                        include_accessibility_tree=False,
                        include_screenshot=True,
                        screenshot_out_file=str(out),
                        max_elements=None,
                        max_depth=None,
                        max_dimension=None,
                        max_image_dimension=None,
                    )
                )
            )
        except Exception:  # noqa: BLE001 - best-effort
            pass
        finally:
            out.unlink(missing_ok=True)

    def switch_to_first_tab(self) -> None:
        """Ctrl+1 切第 1 个标签（SDK HotkeyInput；取代 macOS 的 Cmd+1）。"""
        from cua_driver import HotkeyInput

        try:
            self._bridge.run(
                self._driver.hotkey(
                    HotkeyInput(keys=["ctrl", "1"], target=None, scope=None, session=None)
                )
            )
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Ctrl+1 切换标签失败（Windows/SDK）：{e}") from e

    def _call_tool_silent(self, name: str, payload: dict[str, Any]) -> bool:
        try:
            self._bridge.run(self._driver.call_tool(name, json.dumps(payload)))
            return True
        except Exception:  # noqa: BLE001 - best-effort
            return False
