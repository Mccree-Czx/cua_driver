"""平台抽象：平台无关的语义角色、窗口引用与 adapter 协议。

元素对象沿用 SDK 原生的 WindowElement / WindowStateOutput，此处不做包装。
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, Sequence

# 求职意向里的薪资格式（如 11-22k×12薪 / 16-35k×12薪）—— 两侧共用的判据
SALARY_RE = re.compile(r"\d+\s*-\s*\d+\s*k(?:\s*×\s*\d+\s*薪)?", re.IGNORECASE)


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
    TAB = "TAB"


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
    primary_modifier: str  # 主修饰键：macOS "cmd" / Windows "ctrl"（共享层不写死）

    # —— 窗口 ——
    def candidate_windows(self, windows: Sequence[Any]) -> list[WindowRef]: ...

    def url_of(self, state: Any) -> str: ...

    def is_on_screen(self, windows: Sequence[Any], pid: int, window_id: int) -> bool: ...

    def activate(self, windows: Sequence[Any], pid: int) -> None: ...

    def raise_window(self, windows: Sequence[Any], pid: int, window_id: int) -> bool: ...

    # —— 解析 ——
    def role_name(self, role: Role) -> str: ...

    def tab_elements(self, state: Any) -> list[tuple[str, Any]]:
        """批量页候选人选项卡 [(姓名, 元素)]（树结构平台差异大，故下沉）。"""
        ...

    def field_value(self, state: Any, icon: str) -> str | None:
        """字段图标锚点（environment/work/education/file-search）后的首个文本值。"""
        ...

    def salary(self, state: Any) -> str:
        """求职意向里的薪资项（形如 11-22k×12薪）；页面未提供时返回空串。"""
        ...

    # —— 动作 ——
    def click_point(self, pid: int, window_id: int, x: float, y: float) -> None: ...

    def switch_to_first_tab(self) -> None: ...
