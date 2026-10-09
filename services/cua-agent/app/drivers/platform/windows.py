"""Windows 平台原语。

角色名取 UIA 标准 ControlType（微软 UI Automation 规范）。真实树上的实际取值
以阶段 2 的页面校准为准 —— 本机已实测到的角色为 Button / Edit / Pane / Document。
"""

from .base import Role

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
