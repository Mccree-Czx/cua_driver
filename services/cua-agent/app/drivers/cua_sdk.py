"""CuaLiepinDriver：真实 CUA SDK 驱动骨架（spike 结论落地的唯一文件，爆炸半径仅此文件）。

Spike（T8 Step 1，2026-10-05）结论：接入 PyPI `cua-driver`（钉 0.30.4，
与本机已装桌面应用同版；证据见 pyproject 顶部注释）。`CuaDriver.create()`
在导入进程内加载 Rust runtime，不依赖已装 exe/daemon；SDK 方法全 async；
`start_session` 可选（隐式 session，5 分钟无活动过期）；桌面观测返回带
text/images/verification 元数据的 typed ToolResult。

本类只保证「能构造」（初始化 SDK client）。页面级方法按协议逐个映射到
SDK 能力（get_desktop_state 截图 / type_text / click / list_windows /
page 工具等），但真实桌面会话在本任务环境（无交互式桌面验证条件）下
无法校准，方法体一律 NotImplementedError 并注明「待 T12 真实账号冒烟
校准」——诚实标注，不假装实现。

同步适配注记（T9 worker 关注）：SDK 方法全 async，而 contracts 协议是
同步签名；真实模式下 worker 需以 asyncio.run 包裹（或协议升级为 async），
本骨架不代做该决策。
"""

from typing import Any

from hr_workbuddy import MinimalResume


class CuaNotInstalledError(RuntimeError):
    """cua-driver SDK 未安装（应经 `uv sync --all-packages` 安装）。"""


class CuaLiepinDriver:
    """真实驱动。构造即初始化 SDK client（CuaDriver.create()，进程内 runtime）。"""

    def __init__(self) -> None:
        try:
            from cua_driver import CuaDriver  # 延迟导入：mock 模式不触发
        except ImportError as e:  # pragma: no cover - 依赖已入 pyproject
            raise CuaNotInstalledError(
                "cua-driver SDK 未安装：请在仓库根执行 `uv sync --all-packages`"
            ) from e
        self._driver: Any = CuaDriver.create()
        # 备选（daemon 模式，官方标注为迁移过渡接口，M1 不用）：
        #   self._driver = CuaDriver.connect(socket_path)

    def check_login(self) -> bool:
        """映射方案：get_desktop_state 截图 → brain.verify("页面处于已登录状态")。

        待 T12 真实账号冒烟校准：确认登录态判定锚点（右上角头像/「登录」按钮）。
        """
        raise NotImplementedError("check_login：待 T12 真实账号冒烟校准（desktop 截图 + 视觉判定）")

    def list_unread_conversations(self) -> list[str]:
        """映射方案：进入消息列表页（page/browser 工具）→ 截图 → 视觉解析未读会话 id 列表。

        待 T12 真实账号冒烟校准：未读标记的 DOM/视觉特征与列表分页策略。
        """
        raise NotImplementedError("list_unread_conversations：待 T12 真实账号冒烟校准")

    def open_conversation(self, candidate_liepin_id: str) -> None:
        """映射方案：消息列表页定位目标会话（type_text 搜索 / 列表点击）→ click 进入。"""
        raise NotImplementedError("open_conversation：待 T12 真实账号冒烟校准")

    def read_online_resume(self, candidate_liepin_id: str) -> tuple[bytes, MinimalResume]:
        """映射方案：会话页点开在线简历 → get_desktop_state 截图 → 视觉解析 7 字段。

        待 T12 真实账号冒烟校准：简历卡片位置、字段锚点、无简历时的页面形态。
        """
        raise NotImplementedError("read_online_resume：待 T12 真实账号冒烟校准")

    def send_message(self, candidate_liepin_id: str, text: str) -> None:
        """映射方案：会话输入框 click + type_text → 截图校验已上屏（brain.verify）。"""
        raise NotImplementedError("send_message：待 T12 真实账号冒烟校准")

    def check_attachment(self, candidate_liepin_id: str) -> bool:
        """映射方案：会话页截图 → brain.verify("对方已发送简历附件")。"""
        raise NotImplementedError("check_attachment：待 T12 真实账号冒烟校准")

    def download_attachment(self, candidate_liepin_id: str) -> tuple[bytes, str]:
        """映射方案：附件卡片 click 下载 → 从下载目录/剪贴板读回字节。

        R7：返回 (字节, 文件名)——pipeline artifact 端点要求 filename 表单字段。
        待 T12 真实账号冒烟校准：下载目录约定与文件回落位置、文件名取法。
        """
        raise NotImplementedError("download_attachment：待 T12 真实账号冒烟校准")
