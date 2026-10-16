"""锚点表按设备解析的测试（2026-10-16）。

`flows.map_path()` 决定用哪份锚点表（真机 / 云机）。它错的方式是**静默点错位置**：
真机的坐标落到 720x1280 的云机上，点偏了不报错，只是点到了别的东西——而这是在一台
真实招聘账号的手机上。所以这里把优先级钉死，尤其是「不设任何覆盖时与从前一致」。
"""

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "agent"))

import flows  # noqa: E402
import phone  # noqa: E402
import run_round  # noqa: E402


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


def _section(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["附件简历"]


def test_cloud_map_points_at_internal_attachment_dir():
    """云机表给出「直接取件目录」——adb 是 root，能读 /data/data。"""
    assert _section(flows.MAP_PATH_CLOUD)["直接取件目录"].startswith("/data/data/")


def test_real_phone_map_marks_it_explicitly_null():
    """真机表必须有这个键、且**显式**为 null。

    `tools_mcp_server._direct_attachment_dir()` 靠「有没有值」分派取件路径。
    缺键与「有意没有」必须能分辨——真机读不了 /data/data 是有意走 UC 的，
    将来有人看到缺键会以为是漏配、随手补上一条没验证过的路径。
    """
    section = _section(flows.MAP_PATH)
    assert "直接取件目录" in section
    assert section["直接取件目录"] is None


class TestEnsureDevice:
    """`run_round.ensure_device()` 的四档行为。

    这条闸门错的方式很贵：它决定 `flows.map_path()` 选哪份锚点表——选错就是
    **拿 1080x2244 的坐标去点 720x1280 的屏**，而且全程不报错。
    """

    def _clear(self, monkeypatch):
        monkeypatch.delenv("ANDROID_SERIAL", raising=False)
        monkeypatch.delenv("HR_CLOUD_PHONE", raising=False)
        # 复位模块级的 _DEVICE（use_device() 会改它；None 就是「不传 -s」的默认态）
        phone.use_device(None)

    def test_mac_shaped_machine_is_left_alone(self, monkeypatch):
        """没设 HR_CLOUD_PHONE 的机器（Mac 那台跑真机的）必须保持老行为。

        ★ 这条最要紧：Mac 上没有多多云客户端，发现函数永远返回 None。
          要是把它也弄成「拒跑」，等于把正在跑的生产停掉。
        """
        self._clear(monkeypatch)
        monkeypatch.setattr(
            phone, "discover_cloud_phone", lambda *a, **k: pytest.fail("不该去发现云机")
        )
        assert run_round.ensure_device() is None
        assert "ANDROID_SERIAL" not in os.environ

    def test_cloud_executor_discovers_and_injects(self, monkeypatch):
        """.66 那台：发现当前端点 → 注入 → map_path 必须选到云机表。"""
        self._clear(monkeypatch)
        monkeypatch.setenv("HR_CLOUD_PHONE", "1")
        monkeypatch.setattr(phone, "discover_cloud_phone", lambda *a, **k: "127.0.0.1:51672")

        assert run_round.ensure_device() is None
        assert os.environ["ANDROID_SERIAL"] == "127.0.0.1:51672"
        assert phone.current_device() == "127.0.0.1:51672"
        assert flows.map_path().name == "login_map.cloud.json"  # ← 这一步才是重点

    def test_missing_cloud_phone_refuses_instead_of_guessing(self, monkeypatch):
        """找不到云机就拒跑——不猜。猜了就会用错锚点表。"""
        self._clear(monkeypatch)
        monkeypatch.setenv("HR_CLOUD_PHONE", "1")
        monkeypatch.setattr(phone, "discover_cloud_phone", lambda *a, **k: None)

        reason = run_round.ensure_device()
        assert reason and "找不到云机的 adb 端点" in reason
        assert "ANDROID_SERIAL" not in os.environ  # 没被写脏

    def test_existing_serial_wins_and_skips_discovery(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("HR_CLOUD_PHONE", "1")
        monkeypatch.setenv("ANDROID_SERIAL", "VED0218928022836")
        monkeypatch.setattr(
            phone, "discover_cloud_phone", lambda *a, **k: pytest.fail("已指定就不该再发现")
        )
        assert run_round.ensure_device() is None
        assert os.environ["ANDROID_SERIAL"] == "VED0218928022836"
