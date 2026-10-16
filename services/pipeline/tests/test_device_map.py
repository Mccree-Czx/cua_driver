"""锚点表按设备解析的测试（2026-10-16）。

`flows.map_path()` 决定用哪份锚点表（真机 / 云机）。它错的方式是**静默点错位置**：
真机的坐标落到 720x1280 的云机上，点偏了不报错，只是点到了别的东西——而这是在一台
真实招聘账号的手机上。所以这里把优先级钉死，尤其是「不设任何覆盖时与从前一致」。
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "agent"))

import flows  # noqa: E402


def test_defaults_to_real_phone_map(monkeypatch):
    monkeypatch.delenv("HR_LOGIN_MAP", raising=False)
    monkeypatch.delenv("ANDROID_SERIAL", raising=False)
    assert flows.map_path() == flows.MAP_PATH


def test_network_device_uses_cloud_map(monkeypatch):
    monkeypatch.delenv("HR_LOGIN_MAP", raising=False)
    monkeypatch.setenv("ANDROID_SERIAL", "127.0.0.1:63833")
    assert flows.map_path() == flows.MAP_PATH_CLOUD


def test_usb_serial_stays_on_real_phone_map(monkeypatch):
    """USB 序列号不带冒号，不是网络设备——仍走真机表。"""
    monkeypatch.delenv("HR_LOGIN_MAP", raising=False)
    monkeypatch.setenv("ANDROID_SERIAL", "VED0218928022836")
    assert flows.map_path() == flows.MAP_PATH


def test_explicit_override_wins(monkeypatch, tmp_path):
    target = tmp_path / "custom.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ANDROID_SERIAL", "127.0.0.1:63833")
    monkeypatch.setenv("HR_LOGIN_MAP", str(target))
    assert flows.map_path() == target


def test_load_map_reads_the_selected_file(monkeypatch, tmp_path):
    """load_map 必须读 map_path 选中的那份，而不是写死的 MAP_PATH。"""
    target = tmp_path / "custom.json"
    target.write_text('{"app": {"screen": "720x1280"}}', encoding="utf-8")
    monkeypatch.setenv("HR_LOGIN_MAP", str(target))
    assert flows.load_map()["app"]["screen"] == "720x1280"


def test_missing_map_raises_instead_of_falling_back(monkeypatch, tmp_path):
    """选中的表不存在**必须报错**，不能静默退回真机表——那等于拿真机坐标点云机的屏。"""
    monkeypatch.setenv("HR_LOGIN_MAP", str(tmp_path / "does-not-exist.json"))
    with pytest.raises(OSError):
        flows.load_map()
