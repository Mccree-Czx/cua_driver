"""cua-agent 测试 conftest：使 `import app` 在任意 cwd（如仓库根）下可用。

与 pipeline 测试同模式：sys.path 插入服务目录，不做其他全局配置——
本服务测试全 mock，无数据库/网络/桌面依赖。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
