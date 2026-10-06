"""World JSON 剧本模型：FakeLiepinDriver 的确定性脚本。

- conversations：每会话 liepin_user_id + unread 标记 + resume fixture 引用；
- resume_fixtures：fixture_id → MinimalResume（contracts 恰 7 字段）；
- reply_timeline："never"（永不回复/送达）或按 tick 的 ReplyEvent 表；
- login_state：登录态剧本；tick：剧本时钟（advance() 拨快，测试用）。

时钟注入：World.tick 是剧本自带的时钟；driver 侧可再注入 now() 取时钟
（FakeLiepinDriver(world, now=...)），测试无需可变全局即可拨快时间。
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hr_workbuddy import MinimalResume


class AttachmentSpec(BaseModel):
    """附件送达规格：文件名 + 内容类型；字节由 driver 按 seed 确定性生成。"""

    model_config = ConfigDict(extra="forbid")

    file_name: str
    content_type: str = "application/pdf"


class ReplyEvent(BaseModel):
    """一次回复/附件送达事件：tick <= 当前 tick 时可见。"""

    model_config = ConfigDict(extra="forbid")

    tick: int = Field(ge=0)
    conversation_id: str  # liepin_user_id
    text: str | None = None  # 回复文本（无 attachment 时即纯文本回复）
    attachment: AttachmentSpec | None = None  # 附件（无 text 时即纯附件送达）


class ConversationScript(BaseModel):
    model_config = ConfigDict(extra="forbid")

    liepin_user_id: str
    unread: bool = False
    resume_fixture: str | None = None  # 引用 World.resume_fixtures 的 key；None = 无在线简历


class World(BaseModel):
    """猎聘世界剧本。reply_timeline 接受 "never" 或事件表两种 JSON 拼写。"""

    model_config = ConfigDict(extra="forbid")

    login_state: bool = True
    tick: int = Field(default=0, ge=0)
    conversations: list[ConversationScript] = Field(default_factory=list)
    resume_fixtures: dict[str, MinimalResume] = Field(default_factory=dict)
    reply_timeline: list[ReplyEvent] | Literal["never"] = "never"

    def advance(self, n: int = 1) -> None:
        """拨快剧本时钟（测试拨快 tick；生产由调度注入真实时钟）。"""
        self.tick += n

    def visible_events(self, now: int | None = None) -> list[ReplyEvent]:
        """已送达事件（tick <= now，默认当前 tick），按 tick 升序。"""
        if self.reply_timeline == "never":
            return []
        now = self.tick if now is None else now
        return sorted(
            (e for e in self.reply_timeline if e.tick <= now), key=lambda e: e.tick
        )


def load_world(path: str | Path) -> World:
    """从 JSON 文件加载剧本（pydantic 全量校验，未知字段拒绝）。"""
    return World.model_validate_json(Path(path).read_text(encoding="utf-8"))
