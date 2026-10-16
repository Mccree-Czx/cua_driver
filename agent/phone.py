"""猎聘执行端的 adb 原语（= 共享层 + 猎聘自己的底部 tab 标记）。

通用实现已搬到 `shared/android_ui.py`（2026-10-16，为 BOSS 执行端复用）。
**本文件只做三件事**：把共享层引进来、补上猎聘专有的那点东西、保持对外名字不变。

★ 对外接口与重构前**完全一致**，`flows.py` / `login_watch.py` / `probe_jobs.py` /
`services/pipeline/tests/test_job_scan.py` 都不用改。
★ 仍然是纯标准库、可被裸 python3 拉起（共享层也守这条）。

历史（想改这些行为前先读一遍 `shared/android_ui.py` 顶部的两条教训）：

- Flutter 输入框丢弃连发退格 → 清空优先点自带的清除小×
- 定位按钮必须精确匹配文字 → 按钮一律 `find_exact`
"""

from __future__ import annotations

import sys
from pathlib import Path

# —— 引入共享层 ——
# agent/ 不是 uv workspace 成员，且本模块可能被裸 python3 拉起，
# 所以只能靠 sys.path，不能依赖 venv 里装的包。
_SHARED = Path(__file__).resolve().parents[1] / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from android_ui import *  # noqa: F401,F403  （保持 `phone.xxx` 全部可用）
from android_ui import (  # noqa: F401  显式列出，让 IDE 与静态检查看得见
    ADB,
    DUMP_PATH,
    RISK_MARKERS,
    AdbError,
    adb,
    back,
    clear_button_near,
    clear_field,
    current_activity,
    current_device,
    current_package,
    dump,
    dump_stable,
    edittexts,
    ensure_main_tabs as _ensure_main_tabs,
    field_at,
    find,
    find_exact,
    find_rid,
    find_rid_text,
    focused_field,
    home,
    key,
    label,
    list_devices,
    nodes,
    open_app,
    resolve_adb,
    risk_markers,
    shell,
    swipe,
    tap,
    tap_node,
    texts,
    tick_agreement,
    type_into,
    use_device,
)

# ——— 猎聘专有 ———

#: 猎聘的底部导航三个 tab。**出现两个以上**才算「在主界面」。
MAIN_TAB_MARKERS = ("推荐", "职位", "我的")


def ensure_main_tabs(package: str, max_back: int = 5, settle: float = 6.0) -> dict:
    """退到猎聘的主界面。签名与重构前一致（标记写死为猎聘的）。"""
    return _ensure_main_tabs(package, MAIN_TAB_MARKERS, max_back=max_back, settle=settle)
