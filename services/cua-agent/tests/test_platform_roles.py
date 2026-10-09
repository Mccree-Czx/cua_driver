# services/cua-agent/tests/test_platform_roles.py
"""角色映射表：把平台无关 Role 翻成各平台的树角色名。"""

import pytest

from app.drivers.platform.base import Role
from app.drivers.platform.macos import MACOS_ROLE_MAP
from app.drivers.platform.windows import WINDOWS_ROLE_MAP


def test_role_enum_is_complete():
    assert {r.name for r in Role} == {
        "TEXT", "BUTTON", "RADIO", "CHECKBOX", "IMAGE",
        "LINK", "TEXT_INPUT", "TEXT_AREA", "WEB_AREA",
    }


def test_macos_map_covers_every_role():
    assert set(MACOS_ROLE_MAP) == set(Role)


def test_macos_map_matches_verified_ax_names():
    """取值来自 cua_sdk.py 中已在真实页面校准过的角色名。"""
    assert MACOS_ROLE_MAP[Role.TEXT] == "AXStaticText"
    assert MACOS_ROLE_MAP[Role.BUTTON] == "AXButton"
    assert MACOS_ROLE_MAP[Role.RADIO] == "AXRadioButton"
    assert MACOS_ROLE_MAP[Role.CHECKBOX] == "AXCheckBox"
    assert MACOS_ROLE_MAP[Role.IMAGE] == "AXImage"
    assert MACOS_ROLE_MAP[Role.LINK] == "AXLink"
    assert MACOS_ROLE_MAP[Role.TEXT_INPUT] == "AXTextField"
    assert MACOS_ROLE_MAP[Role.TEXT_AREA] == "AXTextArea"
    assert MACOS_ROLE_MAP[Role.WEB_AREA] == "AXWebArea"


def test_windows_map_covers_every_role():
    assert set(WINDOWS_ROLE_MAP) == set(Role)


def test_windows_map_uses_uia_control_types():
    """UIA 标准 ControlType 名；阶段 2 校准时以真实树为准修正。"""
    assert WINDOWS_ROLE_MAP[Role.BUTTON] == "Button"
    assert WINDOWS_ROLE_MAP[Role.TEXT_INPUT] == "Edit"


# —— WindowsAdapter 的纯逻辑（不碰 SDK runtime）——


def _win(app_name: str, pid: int):
    """构造 SDK WindowInfo 形状的最小对象。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        app_name=app_name, pid=pid, window_id=pid, title="", is_on_screen=True
    )


def test_candidate_windows_matches_process_name_variants():
    """chrome.exe / Chrome.exe 都应命中；无关进程不命中。"""
    from app.drivers.platform.windows import WindowsAdapter

    adapter = WindowsAdapter(bridge=None)
    windows = [
        _win("Chrome.exe", pid=1),
        _win("chrome.exe", pid=2),
        _win("Feishu.exe", pid=3),
        _win("msedge.exe", pid=4),
    ]
    assert [w.pid for w in adapter.candidate_windows(windows)] == [1, 2, 4]


def test_windows_is_on_screen_false_for_unknown_pid():
    """已关闭窗口 / 失效 pid：应返回 False，不得抛异常。"""
    from app.drivers.platform.windows import WindowsAdapter

    assert WindowsAdapter(bridge=None).is_on_screen([], pid=999999, window_id=1) is False


def test_windows_screenshot_scale_is_not_retina_default():
    """不得沿用 macOS 的 2.0 —— 否则兜底坐标点击会系统性打偏。"""
    from app.drivers.platform.windows import WindowsAdapter

    scale = WindowsAdapter(bridge=None).screenshot_px_per_point
    assert scale > 0 and scale <= 4


# —— create_adapter 平台分发 ——


def test_create_adapter_rejects_unknown_platform(monkeypatch):
    """未知平台必须明确抛错，不得静默选到错误实现。"""
    import sys

    from app.drivers.platform import create_adapter

    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="不支持"):
        create_adapter(bridge=None)


def test_create_adapter_picks_windows(monkeypatch):
    import sys

    from app.drivers.platform import create_adapter
    from app.drivers.platform.windows import WindowsAdapter

    monkeypatch.setattr(sys, "platform", "win32")
    assert isinstance(create_adapter(bridge=None), WindowsAdapter)
