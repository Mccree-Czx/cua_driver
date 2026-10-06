"""MockBrain：剧本判定（测试 / 离线），verify 返回调用参数指定的期望 verdict。

T9 扩展：verify_with_usage 返回判定 + 注入的用量（默认零）；供 cost.py
把 token 账目写进 TaskResult.evidence。verify 仍只回 bool（协议不变）。
"""

from typing import Callable

from app.brain.usage import BrainUsage, VerifyVerdict


class MockBrain:
    """按 (screenshot, criteria) 脚本判定；未命中脚本时返回 default。"""

    def __init__(
        self,
        script: Callable[[bytes, str], bool] | None = None,
        *,
        default: bool = True,
        usage: BrainUsage | None = None,
    ) -> None:
        self._script = script or (lambda screenshot, criteria: default)
        self._usage = usage if usage is not None else BrainUsage()
        self._last_usage: BrainUsage | None = None

    @property
    def last_usage(self) -> BrainUsage | None:
        return self._last_usage

    def verify(self, screenshot: bytes, criteria: str) -> bool:
        return self.verify_with_usage(screenshot, criteria).ok

    def verify_with_usage(self, screenshot: bytes, criteria: str) -> VerifyVerdict:
        self._last_usage = self._usage
        return VerifyVerdict(ok=self._script(screenshot, criteria), usage=self._usage)
