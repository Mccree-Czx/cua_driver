"""macOS 平台原语。角色名取自 cua_sdk.py 中已在真实页面校准过的 AX 角色。"""

from .base import Role

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
