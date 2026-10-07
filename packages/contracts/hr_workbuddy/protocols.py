"""驱动与大脑协议（typing.Protocol + runtime_checkable，协议隔离 R1）。

FakeLiepinDriver / CuaLiepinDriver、MockBrain / OpenAIBrain 均按此形状实现；
协议签名在 spec 契约块明确者照录，其余为 M1 编排所需的最小约定。
"""

from typing import Protocol, runtime_checkable

from hr_workbuddy.models import FallbackSuggestion, MinimalResume


@runtime_checkable
class LiepinDriver(Protocol):
    """猎聘页面驱动：CUA SDK（真实）与 Fake（JSON 剧本）双实现。"""

    def check_login(self) -> bool: ...

    def list_unread_conversations(self) -> list[str]: ...

    def list_recommended(self, limit: int = 5) -> list[str]:
        """推荐人列表页读取（M2 路径二）：逐卡提取 liepin_user_id，最多 limit 张；
        页码/滚动翻页不在 v1（可见卡片不足时少于 limit）。"""

    def open_conversation(self, candidate_liepin_id: str) -> None: ...

    def read_online_resume(
        self, candidate_liepin_id: str
    ) -> tuple[bytes, MinimalResume]:
        """返回 (PNG bytes 截图, 最小简历)。"""

    def send_message(self, candidate_liepin_id: str, text: str) -> None: ...

    def check_attachment(self, candidate_liepin_id: str) -> bool: ...

    def download_attachment(self, candidate_liepin_id: str) -> tuple[bytes, str]:
        """返回 (附件字节, 文件名)——R7 裁定：pipeline artifact 端点要求
        filename 表单字段，T9 worker 下载后须带文件名上传。"""


@runtime_checkable
class BrainClient(Protocol):
    """视觉校验大脑：OpenAI 兼容（真实）与 Mock（剧本判定）双实现。"""

    def verify(self, screenshot: bytes, criteria: str) -> bool: ...

    def suggest(self, screenshot: bytes, context: str) -> FallbackSuggestion:
        """读取链兜底诊断：截图 + 失败上下文 → 结构化修复建议（2026-10-06 新增）。"""
        ...
