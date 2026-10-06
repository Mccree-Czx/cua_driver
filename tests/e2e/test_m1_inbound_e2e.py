"""M1 E2E 验收（mock 模式）——pytest 包装。

与 scripts/run_m1_e2e.py 同一套逻辑（import 其函数）：一条命令跑全部两剧本
（剧本 A 张伟/LP001 全链路 + 剧本 B 拒绝/零触达/72h 关闭）。

运行（单独跑——仓库根单次 pytest 收集有跨服务冲突，既有问题）：
    uv run pytest tests/e2e -m e2e -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 仓库根：可 import scripts.*

from scripts.run_m1_e2e import format_report, run_e2e  # noqa: E402

pytestmark = pytest.mark.e2e


def test_m1_inbound_e2e_mock_mode():
    """剧本 A + 剧本 B 全量验收：快照键 / 7 字段 / 状态链 / PDF 键 /
    interactions / judge_reason / 拒绝零触达 / 72h 边界两向。"""
    report = run_e2e()
    assert report.passed, format_report(report)
