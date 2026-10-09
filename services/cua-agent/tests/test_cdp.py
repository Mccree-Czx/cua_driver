# services/cua-agent/tests/test_cdp.py
"""CDP 输入通道的纯逻辑：按键 → (CDP code, Windows 虚拟键码) 与修饰键位掩码。

清空草稿（Ctrl+A+Delete）依赖正确的虚拟键码 —— 缺键码时全选不生效（实盘踩中）。
"""

import pytest

from app.drivers.platform.cdp import _key_params, _modifier_mask, _parse_textarea_center


def test_key_params_letters():
    assert _key_params("a") == ("KeyA", 65)
    assert _key_params("A") == ("KeyA", 65)  # 大小写都映射到大写 code
    assert _key_params("z") == ("KeyZ", 90)


def test_key_params_digits():
    assert _key_params("1") == ("Digit1", 49)


def test_key_params_named_keys():
    assert _key_params("delete") == ("Delete", 46)
    assert _key_params("Delete") == ("Delete", 46)  # 大小写不敏感
    assert _key_params("enter") == ("Enter", 13)


def test_key_params_rejects_unknown():
    """修饰键不是可独立按压的按键；未识别的键应抛错而非盲发。"""
    with pytest.raises(ValueError):
        _key_params("ctrl")
    with pytest.raises(ValueError):
        _key_params("notakey")


def test_modifier_mask():
    assert _modifier_mask(["ctrl"]) == 2  # Ctrl=2
    assert _modifier_mask(["shift", "alt"]) == 9  # Shift=8 + Alt=1
    assert _modifier_mask(["cmd"]) == 4  # Meta=4
    assert _modifier_mask(None) == 0
    assert _modifier_mask([]) == 0


def test_parse_textarea_center():
    assert _parse_textarea_center({"x": 100.5, "y": 200.25}) == (100.5, 200.25)
    assert _parse_textarea_center({"x": "10", "y": "20"}) == (10.0, 20.0)  # 字符串可转浮点


def test_parse_textarea_center_rejects_invalid():
    with pytest.raises(ValueError):
        _parse_textarea_center(None)  # 不是 dict
    with pytest.raises(ValueError):
        _parse_textarea_center({"x": 1})  # 缺 y
    with pytest.raises(ValueError):
        _parse_textarea_center({"x": "abc", "y": 2})  # x 不可转浮点
