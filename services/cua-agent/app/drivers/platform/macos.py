"""macOS 平台原语。

2026-10-09 从 cua_sdk.py 机械搬移，逻辑未改：坐标点击与热键经 osascript
（System Events），跨 Space 激活经 `open -b`，窗口角色名用 AX* 命名，
截图像素/点比为 Retina 实测值。
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any, Sequence

from .base import Role, WindowRef

MACOS_ROLE_MAP: dict[Role, str] = {
    Role.TEXT: "AXStaticText",
    Role.BUTTON: "AXButton",
    Role.RADIO: "AXRadioButton",
    Role.CHECKBOX: "AXCheckBox",
    Role.IMAGE: "AXImage",
    Role.LINK: "AXLink",
    Role.TEXT_INPUT: "AXTextField",
    Role.TEXT_AREA: "AXTextArea",
    Role.WEB_AREA: "AXWebArea",
}

BROWSER_APPS = ("Google Chrome", "Safari", "Microsoft Edge", "Arc", "Chromium", "Firefox")

BROWSER_BUNDLE_IDS = {
    "Google Chrome": "com.google.Chrome",
    "Safari": "com.apple.Safari",
    "Microsoft Edge": "com.microsoft.edgemac",
    "Arc": "company.thebrowser.Browser",
    "Chromium": "org.chromium.Chromium",
    "Firefox": "org.mozilla.firefox",
}


def _win_ref(w: Any) -> WindowRef:
    return WindowRef(
        pid=int(getattr(w, "pid", 0) or 0),
        window_id=int(getattr(w, "window_id", 0) or 0),
        app_name=str(getattr(w, "app_name", "") or ""),
        title=str(getattr(w, "title", "") or ""),
        is_on_screen=bool(getattr(w, "is_on_screen", False)),
    )


class MacOsAdapter:
    """macOS 原语实现。`bridge` 用于驱动 SDK 协程，`driver` 用于 call_tool。"""

    name = "macos"
    screenshot_px_per_point = 2.0  # Retina 实测 2880px:1440pt；换环境需校准

    def __init__(self, bridge: Any = None, driver: Any = None) -> None:
        self._bridge = bridge
        self._driver = driver

    def role_name(self, role: Role) -> str:
        return MACOS_ROLE_MAP[role]

    def candidate_windows(self, windows: Sequence[Any]) -> list[WindowRef]:
        return [_win_ref(w) for w in windows if getattr(w, "app_name", "") in BROWSER_APPS]

    def url_of(self, state: Any) -> str:
        for e in getattr(state, "elements", []) or []:
            if str(getattr(e, "role", "")) == "AXTextField" and "地址" in str(
                getattr(e, "label", "") or ""
            ):
                return str(getattr(e, "value", "") or "")
        return ""

    def is_on_screen(self, windows: Sequence[Any], pid: int, window_id: int) -> bool:
        for w in windows:
            if getattr(w, "pid", None) == pid and getattr(w, "window_id", None) == window_id:
                return bool(getattr(w, "is_on_screen", False))
        return False

    def activate(self, windows: Sequence[Any], pid: int) -> None:
        """激活浏览器应用（open -b <bundle>；跨 Space 有效，已激活时幂等）。"""
        bundle = "com.google.Chrome"
        if pid:
            for w in windows:
                if getattr(w, "pid", None) == pid:
                    bundle = BROWSER_BUNDLE_IDS.get(str(getattr(w, "app_name", "")), bundle)
                    break
        try:
            subprocess.run(["open", "-b", bundle], capture_output=True, check=False, timeout=10)
        except Exception:
            pass

    def raise_window(self, windows: Sequence[Any], pid: int, window_id: int) -> bool:
        """跨 Space 精确前置：经「窗口」菜单 makeKeyAndOrderFront（实测配方）。

        返回「菜单调用是否成功」；最终可见性由调用方（ensure_visible）重新枚举
        窗口判定 —— adapter 保持无状态，不持有窗口枚举能力。
        """
        title = ""
        context_wid = None
        for w in windows:
            if getattr(w, "pid", None) != pid:
                continue
            if getattr(w, "window_id", None) == window_id:
                title = str(getattr(w, "title", "") or "").strip()
            elif getattr(w, "is_on_screen", False) and context_wid is None:
                context_wid = getattr(w, "window_id", None)
        if not title or context_wid is None:
            return False
        try:
            self._bridge.run(
                self._driver.call_tool(
                    "invoke_menu",
                    json.dumps({"pid": pid, "window_id": context_wid, "path": ["窗口", title]}),
                )
            )
        except Exception:
            return False
        time.sleep(1.5)
        return True

    def click_point(self, pid: int, window_id: int, x: float, y: float) -> None:
        """坐标点击（System Events；实测通道：无 AXPress 的列表行元素）。"""
        script = f'tell application "System Events" to click at {{{x:.0f}, {y:.0f}}}'
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"坐标点击失败（osascript rc={result.returncode}）：{result.stderr.strip()[:200]}"
            )

    def switch_to_first_tab(self) -> None:
        """Cmd+1 切第 1 个标签（2026-10-07 实测：比坐标点页签可靠）。"""
        script = 'tell application "System Events" to keystroke "1" using {command down}'
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Cmd+1 切换标签失败（osascript rc={result.returncode}）：{result.stderr.strip()[:160]}"
            )
