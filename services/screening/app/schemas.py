"""screening 内部 schema：LLM 评分输出强约束（spec 决策 11：pydantic + JSON Schema）。"""

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class LLMScoreOutput(BaseModel):
    """LLM 必须输出的 JSON 对象：{score: int 0-100, reason: str}。

    StrictInt + ge/le + extra="forbid"：非整数（"82"、82.5）、越界（150）、
    缺字段、多字段全部 ValidationError → 调用方走降级。
    """

    model_config = ConfigDict(extra="forbid")

    score: StrictInt = Field(ge=0, le=100)
    reason: str
