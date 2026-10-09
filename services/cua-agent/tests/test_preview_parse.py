"""推荐页预览解析单测（纯解析，不初始化 SDK）——以可注入 state 桩校验
2026-10-07 实测结构：_preview_name / _preview_summary / _recommend_cards /
_element_center（M2 推荐人 read 回退路径的解析层）。"""

from types import SimpleNamespace

from app.drivers.cua_sdk import CuaLiepinDriver
from app.drivers.platform.macos import MacOsAdapter


def _el(role: str, label: str = "", frame=None) -> SimpleNamespace:
    return SimpleNamespace(role=role, label=label, frame=frame)


def _driver() -> CuaLiepinDriver:
    """绕过 __init__（不初始化 SDK runtime）——仅测纯解析方法。

    注入 macOS adapter：解析方法按语义 Role 查平台角色名（2026-10-09 平台抽象后）。
    """
    driver = CuaLiepinDriver.__new__(CuaLiepinDriver)
    driver._plat = MacOsAdapter()
    return driver


def test_preview_name_after_view_big_image():
    state = SimpleNamespace(
        elements=[
            _el("AXStaticText", "查看大图"),
            _el("AXImage"),
            _el("AXStaticText", "卢杰"),
            _el("AXStaticText", "今天活跃"),
        ]
    )
    assert _driver()._preview_name(state) == "卢杰"


def test_preview_name_skips_status_word():
    state = SimpleNamespace(
        elements=[
            _el("AXStaticText", "查看大图"),
            _el("AXStaticText", "在线"),
            _el("AXStaticText", "刘先生"),
        ]
    )
    assert _driver()._preview_name(state) == "刘先生"


def test_preview_name_missing_returns_empty():
    assert _driver()._preview_name(SimpleNamespace(elements=[])) == ""


def test_preview_summary_first_long_paragraph_after_work_experience():
    long_text = "负责区域市场开拓与维护，" * 5
    state = SimpleNamespace(
        elements=[
            _el("AXStaticText", "工作经历"),
            _el("AXStaticText", "*该段内容已整合附件简历信息"),
            _el("AXStaticText", long_text),
        ]
    )
    assert _driver()._preview_summary(state) == long_text[:200]


def test_preview_summary_empty_when_no_long_paragraph():
    state = SimpleNamespace(
        elements=[_el("AXStaticText", "工作经历"), _el("AXStaticText", "短文本")]
    )
    assert _driver()._preview_summary(state) == ""


def test_recommend_cards_names_and_centers_after_section_anchor():
    """「系统推荐」之前的头像（侧栏顾问/AI 块）必须忽略；状态词跳过；中心可算。"""
    state = SimpleNamespace(
        elements=[
            _el("AXImage", "头像"),
            _el("AXStaticText", "我的专属顾问"),
            _el("AXStaticText", "系统推荐"),
            _el("AXImage", "头像"),
            _el("AXStaticText", "在线"),
            _el("AXStaticText", "卢杰", frame=(300, 560, 32, 20)),
            _el("AXImage", "头像"),
            _el("AXStaticText", "今天活跃"),
            _el("AXStaticText", "刘先生", frame=(300, 760, 40, 20)),
        ]
    )
    cards = _driver()._recommend_cards(state)
    assert [name for name, _ in cards] == ["卢杰", "刘先生"]
    # 2026-10-09：卡片改为返回姓名元素本身（点击通道由平台 adapter 决定：
    # macOS 走元素中心坐标、Windows 走 element_token）
    assert cards[0][1].label == "卢杰"


def test_element_center_none_on_missing_frame():
    assert _driver()._element_center(_el("AXImage", "x")) is None
