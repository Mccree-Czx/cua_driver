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
    "你是招聘初筛评分助手。根据岗位 JD 与候选人简历给出 1-5 星的匹配评级并说明理由。"
    "评级标准：1 星=不匹配；2 星=勉强；3 星=基本符合；4 星=完全符合；5 星=符合且有亮点。"
    '只输出 JSON 对象：{"stars": 1-5 的整数, "reason": "理由"}。'
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


def _scoring_prefs_block(prefs: dict) -> str:
    """把评分卡偏好（加分点/否决点/其他要求）拼成评分提示词片段。"""
    boosters = prefs.get("boosters") or []
    veto = prefs.get("veto") or []
    requirements = prefs.get("requirements") or []
    if not (boosters or veto or requirements):
        return ""
    parts = []
    if boosters:
        parts.append("加分点（符合则提升评级）：\n" + "\n".join(f"- {b}" for b in boosters))
    if veto:
        parts.append("一票否决点（命中任一直接 1 星）：\n" + "\n".join(f"- {v}" for v in veto))
    if requirements:
        parts.append("其他要求：\n" + "\n".join(f"- {r}" for r in requirements))
    return "评分偏好：\n" + "\n".join(parts)


def score(
    client: OpenAI,
    jd_text: str,
    resume: MinimalResume,
    model: str,
    scoring_prefs: dict | None = None,
) -> tuple[int, str]:
    """调用 LLM 评分，返回 (stars, reason)；失败抛 LLMScoreError。"""
    prefs_block = _scoring_prefs_block(scoring_prefs or {})
    user_prompt = (
        f"岗位 JD：\n{jd_text}\n\n"
        f"{prefs_block}\n"
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

    return result.stars, result.reason
