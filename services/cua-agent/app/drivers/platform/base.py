"""平台抽象：平台无关的语义角色、窗口引用与 adapter 协议。

元素对象沿用 SDK 原生的 WindowElement / WindowStateOutput，此处不做包装。
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, Sequence

# 求职意向里的薪资格式（如 11-22k×12薪 / 16-35k×12薪）—— 两侧共用的判据
SALARY_RE = re.compile(r"\d+\s*-\s*\d+\s*k(?:\s*×\s*\d+\s*薪)?", re.IGNORECASE)

# 推荐卡状态词（头像/卡片首部的状态文本，如「今天活跃」「隐藏」）
CARD_STATUS_RE = re.compile(r"^(在线|离线|隐藏|.*活跃)$")


def element_center(element: Any) -> tuple[float, float] | None:
    """元素 frame 的屏幕点中心；不可解析时返回 None（调用方放弃点击，不盲点）。"""
    frame = getattr(element, "frame", None)
    if frame is None:
        return None
    try:
        vals = list(frame)
    except TypeError:
        vals = []
    if len(vals) == 4:
        x, y, w, h = (float(v) for v in vals)
        return x + w / 2.0, y + h / 2.0
    x = getattr(frame, "x", None)
    y = getattr(frame, "y", None)
    w = getattr(frame, "w", getattr(frame, "width", None))
    h = getattr(frame, "h", getattr(frame, "height", None))
    if None not in (x, y, w, h):
        return float(x) + float(w) / 2.0, float(y) + float(h) / 2.0
    return None


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

    def recommend_cards(self, state: Any) -> list[tuple[str, Any | None]]:
        """推荐页卡片 [(姓名, 姓名元素或 None)]（卡片锚点平台差异大，故下沉）。"""
        ...

    # —— 动作 ——
    def click_point(self, pid: int, window_id: int, x: float, y: float) -> None: ...

    def click_element(self, pid: int, window_id: int, element: Any) -> None:
        """元素级点击：macOS 走元素中心坐标，Windows 走 SDK element_token。"""
        ...

    def switch_to_first_tab(self) -> None: ...
