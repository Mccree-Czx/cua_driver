"""平台抽象：平台无关的语义角色、窗口引用与 adapter 协议。

元素对象沿用 SDK 原生的 WindowElement / WindowStateOutput，此处不做包装。
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, Sequence


class Role(StrEnum):
    """平台无关的树元素角色。各平台映射见 macos.py / windows.py。"""

    TEXT = "TEXT"
    BUTTON = "BUTTON"
    RADIO = "RADIO"
    CHECKBOX = "CHECKBOX"
    IMAGE = "IMAGE"
    LINK = "LINK"
    TEXT_INPUT = "TEXT_INPUT"
    TEXT_AREA = "TEXT_AREA"
    WEB_AREA = "WEB_AREA"


@dataclass(frozen=True)
class WindowRef:
    """目标窗口的窄化视图（由 SDK WindowInfo 转换而来）。"""

    pid: int
    window_id: int
    app_name: str
    title: str
    is_on_screen: bool


class PlatformAdapter(Protocol):
    """平台差异的唯一收敛点：窗口判据、树角色名、动作通道。"""

    name: str
    screenshot_px_per_point: float

    # —— 窗口 ——
    def candidate_windows(self, windows: Sequence[Any]) -> list[WindowRef]: ...

    def url_of(self, state: Any) -> str: ...

    def is_on_screen(self, windows: Sequence[Any], pid: int, window_id: int) -> bool: ...

    def activate(self, windows: Sequence[Any], pid: int) -> None: ...

    def raise_window(self, windows: Sequence[Any], pid: int, window_id: int) -> bool: ...

    # —— 解析 ——
    def role_name(self, role: Role) -> str: ...

    # —— 动作 ——
    def click_point(self, pid: int, window_id: int, x: float, y: float) -> None: ...

    def switch_to_first_tab(self) -> None: ...
