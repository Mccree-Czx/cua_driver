"""视觉大脑调用用量与判定结果（T9 cost 账目消费方）。

BrainClient 协议（contracts）只约定 verify -> bool；verify_with_usage 是
大脑实现的可选扩展——返回判定 + 本次调用 token 用量，供 cost.py 写入
TaskResult.evidence（brain_tokens / cost_est）。无该扩展的大脑按零用量
记账（verify.py 兜底）。
"""

from dataclasses import dataclass


@dataclass
class BrainUsage:
    """单次视觉调用 token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "BrainUsage") -> "BrainUsage":
        return BrainUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass
class VerifyVerdict:
    """verify_with_usage 的返回：判定 + 用量。"""

    ok: bool
    usage: BrainUsage
