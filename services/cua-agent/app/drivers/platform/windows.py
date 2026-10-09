"""Windows 平台原语。

角色名取 UIA 标准 ControlType（微软 UI Automation 规范）。真实树上的实际取值
以阶段 2 的页面校准为准 —— 本机已实测到的角色为 Button / Edit / Pane / Document。

坐标点击与热键走 SDK 原生能力（ClickPosition.COORDINATES / HotkeyInput），
不依赖 macOS 的 osascript；窗口激活走 SDK call_tool("bring_to_front")。
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from .base import Role, WindowRef

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


class WindowsAdapter:
    """Windows 原语实现。`bridge` 驱动 SDK 协程，`driver` 用于 click / hotkey / call_tool。"""

    name = "windows"

    def __init__(self, bridge: Any = None, driver: Any = None) -> None:
        self._bridge = bridge
        self._driver = driver
        self.screenshot_px_per_point = _system_dpi_scale()

    def role_name(self, role: Role) -> str:
        return WINDOWS_ROLE_MAP[role]

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
        for w in windows:
            if getattr(w, "pid", None) == pid and getattr(w, "window_id", None) == window_id:
                return bool(getattr(w, "is_on_screen", False))
        return False

    def activate(self, windows: Sequence[Any], pid: int) -> None:
        """激活窗口（best-effort；失败静默，与 macOS 侧口径一致）。"""
        if pid:
            self._call_tool_silent("bring_to_front", {"pid": pid})

    def raise_window(self, windows: Sequence[Any], pid: int, window_id: int) -> bool:
        """精确前置；返回「调用是否成功」，可见性判定由调用方重新枚举。"""
        if not pid or not window_id:
            return False
        return self._call_tool_silent("bring_to_front", {"pid": pid, "window_id": window_id})

    def click_point(self, pid: int, window_id: int, x: float, y: float) -> None:
        """坐标点击（SDK 原生 ClickPosition.COORDINATES；取代 osascript）。"""
        from cua_driver import ActionTarget, ClickInput, ClickPosition, InputDeliveryMode

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
