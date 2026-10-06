"""MockBrain：剧本判定（测试 / 离线），verify 返回调用参数指定的期望 verdict。

T9 扩展：verify_with_usage 返回判定 + 注入的用量（默认零）；供 cost.py
把 token 账目写进 TaskResult.evidence。verify 仍只回 bool（协议不变）。
2026-10-06 扩展：suggest / suggest_with_usage——读取链兜底诊断（mock 默认
none 建议；测试可注入 suggest_result）。
"""

from typing import Callable

from hr_workbuddy import FallbackSuggestion

from app.brain.usage import BrainUsage, VerifyVerdict


class MockBrain:
    """按 (screenshot, criteria) 脚本判定；未命中脚本时返回 default。"""

    def __init__(
        self,
        script: Callable[[bytes, str], bool] | None = None,
        *,
        default: bool = True,
        usage: BrainUsage | None = None,
        suggest_result: FallbackSuggestion | None = None,
    ) -> None:
        self._script = script or (lambda screenshot, criteria: default)
        self._usage = usage if usage is not None else BrainUsage()
        self._last_usage: BrainUsage | None = None
        self._suggest_result = suggest_result or FallbackSuggestion(
            diagnosis="mock：无兜底建议", action="none", confidence=0.0
        )

    @property
    def last_usage(self) -> BrainUsage | None:
        return self._last_usage

    def verify(self, screenshot: bytes, criteria: str) -> bool:
        return self.verify_with_usage(screenshot, criteria).ok

    def verify_with_usage(self, screenshot: bytes, criteria: str) -> VerifyVerdict:
        self._last_usage = self._usage
        return VerifyVerdict(ok=self._script(screenshot, criteria), usage=self._usage)

    def suggest(self, screenshot: bytes, context: str) -> FallbackSuggestion:
        return self.suggest_with_usage(screenshot, context)[0]

    def suggest_with_usage(
        self, screenshot: bytes, context: str
    ) -> tuple[FallbackSuggestion, BrainUsage]:
        """兜底诊断：返回注入的建议 + 注入用量（默认零）。"""
        self._last_usage = self._usage
        return self._suggest_result, self._usage
