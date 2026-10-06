"""动作后成功校验：verify_success(screenshot, criteria) 经 BrainClient。

优先走 verify_with_usage（判定 + token 用量 → cost 账目）；仅实现协议
verify 的大脑按零用量记账。BrainUnavailableError 不捕获——由 worker
降级为 deferred 重判（T8 大脑模块的错误策略），本模块不吞异常。
"""

from hr_workbuddy import BrainClient

from app.brain.usage import BrainUsage, VerifyVerdict


def verify_success(screenshot: bytes, criteria: str, brain: BrainClient) -> VerifyVerdict:
    """校验截图满足成功判据；返回 (ok, 本次调用用量)。"""
    verify_with_usage = getattr(brain, "verify_with_usage", None)
    if callable(verify_with_usage):
        return verify_with_usage(screenshot, criteria)
    return VerifyVerdict(ok=brain.verify(screenshot, criteria), usage=BrainUsage())
