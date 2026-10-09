# services/cua-agent/tests/test_flow_with_fake_adapter.py
"""用假 adapter 驱动流程编排：让「找窗口→读树→判定」首次可单测。

driver 用 __new__ 绕过 __init__（不初始化 SDK runtime），只注入假 adapter 与
最小的窗口/树桩 —— 与 test_preview_parse.py 同一手法。
"""

from types import SimpleNamespace

import pytest

from app.drivers.cua_sdk import CuaLiepinDriver
from app.drivers.platform.base import WindowRef
from app.drivers.platform.macos import MACOS_ROLE_MAP


class _FakeAdapter:
    """最小平台 adapter：只提供流程编排需要的判据，不碰任何 SDK / 桌面。"""

    name = "fake"
    screenshot_px_per_point = 1.0

    def __init__(self, tree_text: str, url_marker_present: bool) -> None:
        self.tree_text = tree_text
        self.url_marker_present = url_marker_present

    def candidate_windows(self, windows):
        return [WindowRef(pid=1, window_id=1, app_name="fake", title="", is_on_screen=True)]

    def url_of(self, state):
        return "lpt.liepin.com/x" if self.url_marker_present else ""

    def is_on_screen(self, windows, pid, window_id):
        return True

    def activate(self, windows, pid):
        return None

    def raise_window(self, windows, pid, window_id):
        return True

    def role_name(self, role):
        return MACOS_ROLE_MAP[role]

    def click_point(self, pid, window_id, x, y):
        return None

    def switch_to_first_tab(self):
        return None


@pytest.fixture
def fake_driver_with_tree():
    """工厂：造一个驱动桩，`_plat` 为假 adapter。

    window_state 按 query 分流 —— 带 query（URL 浅探）只回 URL 标记，
    不带 query（全树）回整树文本。
    """

    def _make(tree_text: str, url_marker_present: bool = True) -> CuaLiepinDriver:
        driver = CuaLiepinDriver.__new__(CuaLiepinDriver)
        driver._plat = _FakeAdapter(tree_text, url_marker_present)
        driver._window_cache = None
        driver._session_started = True
        windows = [
            SimpleNamespace(pid=1, window_id=1, app_name="fake", title="", is_on_screen=True)
        ]

        def _browser_windows_all():
            return windows

        def window_state(pid, window_id, *, query=None, **kwargs):
            if query is not None:
                text = "liepin.com" if url_marker_present else ""
            else:
                text = tree_text
            return SimpleNamespace(tree_markdown=text, elements=[])

        driver._browser_windows_all = _browser_windows_all
        driver.window_state = window_state
        driver.ensure_visible = lambda *a, **k: None
        return driver

    return _make


def test_check_login_true_when_backend_markers_present(fake_driver_with_tree):
    driver = fake_driver_with_tree(tree_text="人才推荐 搜索人才 职位管理")
    assert driver.check_login() is True


def test_check_login_false_on_login_page_markers(fake_driver_with_tree):
    driver = fake_driver_with_tree(tree_text="扫码登录 密码登录")
    assert driver.check_login() is False


def test_check_login_false_when_no_url_marker(fake_driver_with_tree):
    """多标签窗口：第一个窗口探不到 liepin.com → 不应误判为已登录。"""
    driver = fake_driver_with_tree(tree_text="人才推荐", url_marker_present=False)
    assert driver.check_login() is False
