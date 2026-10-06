"""scheduler 测试 conftest。

与 pipeline 同进程跑测试时（`uv run pytest services/scheduler/tests
services/pipeline/tests`），两个服务各有顶层包 `app`，普通 import 会撞名
（先加载的 conftest 缓存 `app`，后加载的服务测试拿错包）。解法：scheduler
的 app 包在 conftest 里以别名 scheduler_app 经 importlib 显式注册——
包内模块全部相对导入（与注册名无关），测试以 scheduler_app.* 导入。

测试不依赖真实 APScheduler / HTTP；limiter 与 registry 测试打真实 Redis
（127.0.0.1:6379，键名带 uuid 后缀，互不污染、用后即删）。
"""

import importlib.util
import sys
from pathlib import Path

SCHEDULER_DIR = Path(__file__).resolve().parents[1]


def _register_scheduler_app() -> None:
    pkg_dir = SCHEDULER_DIR / "app"
    spec = importlib.util.spec_from_file_location(
        "scheduler_app",
        pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["scheduler_app"] = module
    spec.loader.exec_module(module)


_register_scheduler_app()
