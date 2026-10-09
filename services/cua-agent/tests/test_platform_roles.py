# services/cua-agent/tests/test_platform_roles.py
"""角色映射表：把平台无关 Role 翻成各平台的树角色名。"""

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
