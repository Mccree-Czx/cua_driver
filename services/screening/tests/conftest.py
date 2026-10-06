"""screening 测试 conftest：把服务目录加入 sys.path，使 `import app` 在任意 cwd 下可用。

screening 无外部依赖（不连 DB/LLM），无需 pipeline 式的环境预置；LLM 客户端
全部经 FastAPI dependency_overrides 注入 MockLLM，测试零网络调用。
"""

import sys
from pathlib import Path

SCREENING_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCREENING_DIR))
