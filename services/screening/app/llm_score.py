"""LLM 结构化评分（spec v1.6 §3 步骤 4）：OpenAI 兼容客户端 + JSON 强约束输出。

provider 错误与 schema 非法（含供应商畸形响应：空 choices / 缺 content）统一抛
LLMScoreError 子类，由调用方（main）降级；本层不做重试 / 熔断（YAGNI，M1 无重试，
降级即终态）。客户端经 build_client 注入（测试用 MockLLM 替身，零真实网络调用）。
"""

import json

from openai import OpenAI, OpenAIError
from pydantic import ValidationError

from hr_workbuddy import MinimalResume

from app.schemas import LLMScoreOutput

SYSTEM_PROMPT = (
    "你是招聘初筛评分助手。根据岗位 JD 与候选人简历给出 0-100 的匹配分并说明理由。"
    '只输出 JSON 对象：{"score": 0-100 的整数, "reason": "理由"}。'
)


class LLMScoreError(Exception):
    """评分流程失败（provider 或 schema），调用方应降级。"""


class LLMProviderError(LLMScoreError):
    """provider 调用失败（网络 / 鉴权 / 超时等）。"""


class LLMSchemaError(LLMScoreError):
    """LLM 输出不符合 {score, reason} 强约束 schema。"""


def build_client(base_url: str, api_key: str) -> OpenAI:
    """构造 OpenAI 兼容客户端（依赖注入点）。"""
    return OpenAI(base_url=base_url, api_key=api_key)


def score(
    client: OpenAI, jd_text: str, resume: MinimalResume, model: str
) -> tuple[int, str]:
    """调用 LLM 评分，返回 (score, reason)；失败抛 LLMScoreError。"""
    user_prompt = (
        f"岗位 JD：\n{jd_text}\n\n"
        "候选人简历（最小字段快照）：\n"
        f"姓名：{resume.name}\n"
        f"学历：{resume.education}\n"
        f"工作年限：{resume.years_of_experience}\n"
        f"城市：{resume.city}\n"
        f"薪资：{resume.salary}\n"
        f"经历摘要：{resume.experience_summary}\n"
    )
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
    except OpenAIError as exc:
        raise LLMProviderError(f"LLM provider 调用失败: {exc}") from exc
    except (IndexError, AttributeError) as exc:
        # 供应商畸形响应（HTTP 200 + 空 choices / 缺 message/content）：
        # 按 schema 非法降级，不得以 IndexError 逃出降级路径（Review Fix T5(1)）
        raise LLMSchemaError(f"LLM 响应缺有效 choices/content: {exc}") from exc

    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMSchemaError(f"LLM 输出非 JSON: {content!r}") from exc

    try:
        result = LLMScoreOutput.model_validate(parsed)
    except ValidationError as exc:
        raise LLMSchemaError(f"LLM 输出不符合 schema: {exc}") from exc

    return result.score, result.reason
