"""话术渲染 + 一人一消息检查（spec §4 / 决策 3）。

- render_message：template_msgs JSON 变量填充，纯函数（不触库）
- ensure_no_out_message：发送前检查，该 job_candidate 已有 out 方向消息即抛
  OneMessagePerCandidateError——打招呼+索要简历合并为单条模板消息，无催促、
  无追发（决策 3）
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Candidate, Interaction, Job, JobCandidate

# 变量集（最小契约）：{name} 候选人姓名、{title} 岗位名。
# 模板随 JD 配置（1-2 套，话术零承诺性表述）；新增变量须与 UI/任务契约同步 。
TEMPLATE_VARIABLES = ("name", "title")

# 内置默认模板：job.template_msgs 未配置该变体时的兜底。
# 仅 direct_request / resume_ack（2026-10-06 inbound 策略新增变体，兼容存量岗位
# 配置）；greet_request（outbound 打招呼+索要）仍要求显式配置。
# 2026-10-06 晚修订：inbound 双向话术**零岗位名**——会话「沟通职位」可能与库内
# 岗位配置错位（实测事故），回执/直索要不引用 {title}；outbound greet 仍带 {title}。
DEFAULT_TEMPLATES = {
    "direct_request": "您好 {name}，方便发一份简历吗？",
    "resume_ack": "您好 {name}，已收到您的简历，感谢关注！",
}

# 2026-10-07 实测定稿：outbound（推荐人）发送 = 平台「向TA索要」一键动作，
# 系统固定文案（问候 + 简历同意请求，无需/无法自定义）；渲染层不再参与
# outbound 文案，此常量用于任务 context.text 与互动流水如实记录已发内容。
NATIVE_OUTREACH_TEXT = (
    "你好~我这里有个职位很适合你，待遇优厚，了解一下吗？期待回复！\n"
    "我想要一份你的简历，你是否同意？"
)


class OneMessagePerCandidateError(Exception):
    """该 job_candidate 已发送过 out 消息，拒绝再次发送（一人一消息，决策 3）。"""


class _TemplateVars(dict):
    """format_map 变量表：未支持的占位符抛可读 ValueError 而非 KeyError。"""

    def __missing__(self, key: str) -> str:
        raise ValueError(
            f"话术模板含未支持变量 {{{key}}}（可用变量：{', '.join(TEMPLATE_VARIABLES)}）"
        )


def render_message(job: Job, candidate: Candidate, variant: str) -> str:
    """渲染岗位话术模板：template_msgs[variant] 做 {name}、{title} 变量填充。

    job 未配置该变体时，direct_request 用内置默认模板兜底（存量岗位兼容）；
    其余变体缺失仍抛错。
    """
    template = (job.template_msgs or {}).get(variant)
    if not template and variant in DEFAULT_TEMPLATES:
        template = DEFAULT_TEMPLATES[variant]
    if not template:
        raise ValueError(f"岗位 {job.id} 的 template_msgs 缺少话术模板：{variant!r}")
    if not isinstance(template, str):
        raise ValueError(
            f"岗位 {job.id} 的 template_msgs[{variant!r}] 不是字符串模板"
        )
    return template.format_map(_TemplateVars(name=candidate.name, title=job.title))


def ensure_no_out_message(session: Session, jc: JobCandidate) -> None:
    """发送前检查：jc 已有 out 方向消息即抛 OneMessagePerCandidateError。"""
    if jc.id is None:
        raise ValueError("job_candidate 尚未持久化（无 id），无法执行一人一消息检查")
    existing = session.execute(
        select(Interaction.id)
        .where(Interaction.job_candidate_id == jc.id, Interaction.direction == "out")
        .limit(1)
    ).first()
    if existing is not None:
        raise OneMessagePerCandidateError(
            f"job_candidate {jc.id} 已存在 out 方向消息，"
            "一人一消息（决策 3）拒绝二次发送"
        )
