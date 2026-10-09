"""平台原语子包。`create_adapter` 按运行平台分发。"""

from __future__ import annotations

import sys
from typing import Any

from .base import PlatformAdapter


def create_adapter(bridge: Any = None, driver: Any = None) -> PlatformAdapter:
    """按 `sys.platform` 构造平台 adapter。

    未知平台明确抛错 —— 静默回退到某个实现会让驱动在错误的平台上跑出
    误导性结果（例如在 Windows 上按 macOS 的应用名找窗口，判定「未登录」）。
    """
    if sys.platform == "win32":
        from .windows import WindowsAdapter

        return WindowsAdapter(bridge, driver)
    if sys.platform == "darwin":
        from .macos import MacOsAdapter

        return MacOsAdapter(bridge, driver)
    raise RuntimeError(f"不支持的平台：{sys.platform}（仅支持 win32 / darwin）")
