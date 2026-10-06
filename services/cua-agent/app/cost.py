"""token 用量统计 → TaskResult.evidence 账目（brain_tokens / cost_est）。

spec §5 成本失控：云端视觉调用按任务计费；本服务把每次视觉调用的用量
累加进 evidence，pipeline 侧通用 TaskLog 落账由 T10 补（R10）——本模块
只保证 evidence 里有账。

单位约定（与 pipeline TaskLog docstring 一致）：brain_tokens 为 int
（total_tokens 之和）；cost_est 为 float 元、保留 6 位（微元精度）——
单次调用成本 = total_tokens / 1000 × price_per_1k_tokens。
price_per_1k_tokens 注入（config CUA_BRAIN_PRICE_PER_1K_TOKENS，
默认 0 占位，T12 按所选视觉模型定价校准）。
"""

from app.brain.usage import BrainUsage

COST_ROUND_DIGITS = 6  # cost_est 精度：微元


def estimate_cost(usage: BrainUsage, price_per_1k_tokens: float) -> float:
    """单次调用成本（元）。"""
    return round(usage.total_tokens / 1000.0 * price_per_1k_tokens, COST_ROUND_DIGITS)


def accumulate(evidence: dict, usage: BrainUsage, price_per_1k_tokens: float) -> None:
    """把一次调用的用量累加进 evidence（原地更新，既有字段不动）。"""
    evidence["brain_tokens"] = evidence.get("brain_tokens", 0) + usage.total_tokens
    evidence["cost_est"] = round(
        evidence.get("cost_est", 0.0) + estimate_cost(usage, price_per_1k_tokens),
        COST_ROUND_DIGITS,
    )


def usage_evidence(usage: BrainUsage, price_per_1k_tokens: float) -> dict:
    """失败结果的账目 evidence（仅账目，无业务字段）。"""
    evidence: dict = {}
    accumulate(evidence, usage, price_per_1k_tokens)
    return evidence
