"""CuaLiepinDriver：真实 CUA SDK 驱动（T12 校准中）。

Spike（T8）→ 可行性验证（T12 Phase 0，macOS 实测）：
- 接入 PyPI `cua-driver`（钉 0.30.4，与本机已装桌面应用同版）。`CuaDriver.create()`
  在导入进程内加载 Rust runtime（execution_mode=EMBEDDED，无 socket/daemon）；SDK
  方法全 async。
- macOS 实测（2026-10-06，授予「屏幕录制 + 辅助功能」后）：桌面截图、屏幕尺寸、
  list_apps/list_windows、get_window_state（含 tree_markdown 辅助功能树 +
  elements）均可用。注意两点实测行为：
  1) 桌面级 get_desktop_state 的 ToolResult 常携带 "capture binding failed …
     Not dispatching click" 拒绝注记，但 PNG 仍正常落盘——以文件为准
     （capture_desktop_png 实现即此口径）；窗口级 get_window_state 无此注记。
  2) 窗口截图要求窗口可见（不可见/其他 Space 时 "neither AX tree nor screenshot
     succeeded"）——校准动作前需先聚焦/前置目标窗口。
- 异步桥：契约方法为同步签名、SDK 全 async、且 worker 在事件循环内调用——
  用专用循环线程（_RuntimeBridge）+ run_coroutine_threadsafe 解决（asyncio.run
  在运行中的事件循环内会直接抛错）；SDK 对象统一在同一桥线程上创建与调用
  （Rust runtime 线程亲和性未文档化，保守取同线程）。

方法级实现（7 个页面方法）按 runbook §4 冒烟顺序在真实页面校准中逐步填入：
① check_login、③ list_unread_conversations / open_conversation / read_online_resume、
④ send_message 已校准（2026-10-06）→ check_attachment / download_attachment
待校准（保持 NotImplementedError 诚实标注，不假装实现）。

真实页面实测（2026-10-06，lpt.liepin.com 企业版后台）：
- 窗口匹配权威判据 = 地址栏 AX 值含 liepin.com（tab 标题可能是业务名，如「职位管理」，
  标题标记仅为快速通道）；query 过滤浅探约 0.6s/窗口，全树约 30K 字符 / 336 元素。
- 已登录锚点：后台导航「人才推荐/搜索人才/职位管理/招聘工作台」；登录页锚点：
  「扫码登录/密码登录/短信登录/验证码登录」。
- 读链路径：聊天页(lpt.liepin.com/chat/im) →「浏览简历」AXPress → 批量预览简历页
  (/resume/showbatchresumelist?token=...) → 顶部候选人选项卡（AXStaticText，AXPress
  切换）→ 详情 iframe 含「简历编号」（liepin_user_id 真实来源）与字段图标锚点
  （environment=城市 / work=年限 / education=学历 / file-search=摘要 / 求职意向=薪资）。
- 发送链：批量页 →「继续沟通」→ 聊天浮层（「<姓名>的简历」身份锚点）→
  AXTextArea 输入框（label=占位或已输入内容）→ cmd+A+Delete 清空 → type_text
  （CGEvent 注入，AX 读回作辅助校验）→「发送」按钮（仅输入非空时存在）→
  线程出现同文本为上屏证据；发送后未确认上屏即抛错且禁止重试。
- 元素索引逐快照漂移（实测踩中：两次快照间索引已错位）——一律按 role/label 即时
  定位，绝不硬编码索引；行/画布类点击需像素坐标（需前置可见，见下）。
- 跨 Space 前置：open -b 激活 + invoke_menu「窗口 > 标题」（实测有效）；窗口被遮挡/
  在其他 Space 时 Chrome 冻结渲染（AX/截图停更）——读取与操作前须 ensure_visible()。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from hr_workbuddy import MinimalResume


class CuaNotInstalledError(RuntimeError):
    """cua-driver SDK 未安装（应经 `uv sync --all-packages` 安装）。"""


# 风控页判据（2026-10-06 实测：连续高频操作触发「账号行为异常」安全验证页）
RISK_CONTROL_MARKERS = ("账号行为异常", "猎聘安全中心", "图形验证码")


class RiskControlDetectedError(RuntimeError):
    """检测到平台风控/安全验证页：立即失败即停（worker 转人工，绝不自动重试/绕过）。"""


def has_risk_control(tree_text: str) -> bool:
    """风控页判据（模块级纯函数，供驱动与单测复用）。"""
    return any(m in tree_text for m in RISK_CONTROL_MARKERS)


class WindowUnavailableError(RuntimeError):
    """窗口不可达（off_space_or_ax_unresolved）：风控/登录跳转或用户切屏的伴生状态。

    保守化口径（2026-10-06 二次风控事件教训）：此类错误不自动重试，worker 转人工。
    """


class LocatorFailedError(RuntimeError):
    """页面/元素定位失败（锚点缺失、页面结构变化）：读取链可走 LLM 视觉兜底一次。"""


OFF_SPACE_ERROR_CODE = "off_space_or_ax_unresolved"


def _translate_driver_error(exc: Exception) -> Exception:
    """SDK DriverError 翻译：off_space_or_ax_unresolved → WindowUnavailableError。"""
    if getattr(exc, "error_code", None) == OFF_SPACE_ERROR_CODE:
        return WindowUnavailableError(f"窗口不可达（{OFF_SPACE_ERROR_CODE}）：{exc}")
    return exc


class _RuntimeBridge:
    """专用事件循环线程：所有 SDK 调用经此串行执行（同步函数 call / 协程 run）。"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="cua-runtime-bridge"
        )
        self._thread.start()

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """在桥线程上执行同步函数（如 CuaDriver.create）。"""
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def _invoke() -> None:
            try:
                fut.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001 - 原样传回调用线程
                fut.set_exception(exc)

        self._loop.call_soon_threadsafe(_invoke)
        return fut.result()

    def run(self, coro: Any) -> Any:
        """在桥线程的事件循环上跑协程并阻塞等待（任意调用线程均安全）。"""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()


class CuaLiepinDriver:
    """真实驱动。构造即初始化 SDK runtime（进程内 EMBEDDED 模式）。"""

    # 窗口定位候补（list_windows 的 app_name / title 匹配）
    BROWSER_APPS = (
        "Google Chrome",
        "Safari",
        "Microsoft Edge",
        "Arc",
        "Chromium",
        "Firefox",
    )
    LIEPIN_TITLE_MARKERS = ("猎聘", "liepin")  # 快速通道；实测 tab 标题可能是业务名
    URL_MARKER = "liepin.com"  # 权威判据：AX 树的地址栏值
    BACKEND_MARKERS = ("人才推荐", "搜索人才", "职位管理", "招聘工作台")  # 已登录后台导航锚点
    LOGIN_PAGE_MARKERS = ("扫码登录", "密码登录", "短信登录", "验证码登录")  # 登录页锚点
    BROWSER_BUNDLE_IDS = {  # open -b 激活用；跨 Space 唤起浏览器
        "Google Chrome": "com.google.Chrome",
        "Safari": "com.apple.Safari",
        "Microsoft Edge": "com.microsoft.edgemac",
        "Arc": "company.thebrowser.Browser",
        "Chromium": "org.chromium.Chromium",
        "Firefox": "org.mozilla.firefox",
    }
    CHAT_PATH = "lpt.liepin.com/chat"  # 聊天页 URL 片段（实测 /chat/im）
    BATCH_PATH = "resume/showbatchresumelist"  # 批量预览简历页 URL 片段
    SETTLE_SECONDS = 2.0  # SPA 页内切换/导航后的渲染等待（实测 1-2s）
    SCREENSHOT_PX_PER_POINT = 2.0  # 桌面截图像素:屏幕点比例（Retina 实测 2880px:1440pt；换环境需校准）
    ATTACHMENT_EXTS = (".pdf", ".doc", ".docx", ".zip")  # 附件简历文件扩展名

    def __init__(self) -> None:
        try:
            from cua_driver import CuaDriver  # 延迟导入：mock 模式不触发
        except ImportError as e:  # pragma: no cover - 依赖已入 pyproject
            raise CuaNotInstalledError(
                "cua-driver SDK 未安装：请在仓库根执行 `uv sync --all-packages`"
            ) from e
        self._bridge = _RuntimeBridge()
        self._driver: Any = self._bridge.call(CuaDriver.create)
        self._session_started = False
        self._window_cache: tuple[int, int] | None = None  # 猎聘窗口 (pid, window_id) 缓存

    # —— 基础设施（T12 Phase 0 已实测）——

    def _ensure_session(self) -> None:
        """建立 DESKTOP 捕获会话（desktop_capture_authorized；幂等）。"""
        if self._session_started:
            return
        self._bridge.run(self._start_session())
        self._session_started = True

    async def _start_session(self) -> Any:
        from cua_driver import CaptureScope, StartSessionInput

        return await self._driver.start_session(
            StartSessionInput(
                session=None, capture_scope=CaptureScope.DESKTOP, cursor_theme=None
            )
        )

    def capture_desktop_png(self) -> bytes:
        """桌面截图 PNG 字节（worker.capture 注入：真实模式动作后校验的截图源）。

        ToolResult 可能带 capture_binding 拒绝注记但仍落盘——以文件为准；
        未产出文件则抛错并附带状态文本（诊断用）。
        """
        self._ensure_session()
        return self._bridge.run(self._capture_desktop())

    async def _capture_desktop(self) -> bytes:
        from cua_driver import GetDesktopStateInput

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            out = Path(fh.name)
        try:
            state = await self._driver.get_desktop_state(
                GetDesktopStateInput(session=None, screenshot_out_file=str(out))
            )
            if not out.exists() or out.stat().st_size == 0:
                raise RuntimeError(f"桌面截图未产出文件：{str(state)[:300]}")
            return out.read_bytes()
        finally:
            out.unlink(missing_ok=True)

    def find_liepin_windows(self) -> list[tuple[int, int]]:
        """猎聘窗口 (pid, window_id) 列表（当前 0/1 个：单账号单窗口；诊断/校准用）。"""
        found = self._resolve_liepin_window()
        return [(found[0], found[1])] if found else []

    # —— 窗口定位（实测：地址栏 AX 值含 liepin.com 为权威判据）——

    def _resolve_liepin_window(self) -> tuple[int, int, Any] | None:
        """定位猎聘窗口 → (pid, window_id, 全量窗口 state)；找不到返回 None。

        返回的窗口不保证可见（遮挡/其他 Space 时 Chrome 冻结渲染）——操作与
        读取前请调用 ensure_visible()。缓存命中先验证；扫描全失败（可能因
        遮挡冻结）时激活浏览器一次后重试。
        """
        if self._window_cache is not None:
            pid, wid = self._window_cache
            try:
                st = self.window_state(pid, wid)
                if self.URL_MARKER in self._tree_text(st):
                    return pid, wid, st
            except Exception:
                pass
            self._window_cache = None
        hit = self._scan_liepin()
        if hit is None:
            # 全部探测失败（典型：窗口被遮挡/在别的 Space，AX 冻结）→ 激活一次重试
            self._activate_browser(0)
            time.sleep(1.2)
            hit = self._scan_liepin()
        if hit is None:
            return None
        self._window_cache = (hit[0], hit[1])
        return hit

    def _scan_liepin(self) -> tuple[int, int, Any] | None:
        """扫描浏览器窗口找猎聘页（标题命中优先、在屏窗口优先；逐窗口 query 浅探）。"""
        windows = self._browser_windows()
        windows.sort(
            key=lambda w: (
                0 if self._title_matches(w) else 1,
                0 if getattr(w, "is_on_screen", False) else 1,
            )
        )
        for w in windows:
            pid, wid = getattr(w, "pid", None), getattr(w, "window_id", None)
            if pid is None or wid is None:
                continue
            try:
                if not self._probe_liepin(pid, wid):
                    continue
                st = self.window_state(pid, wid)
            except Exception:
                continue
            return pid, wid, st
        return None

    def _browser_windows(self) -> list[Any]:
        try:
            out = self._bridge.run(self._list_windows_async())
        except Exception:
            return []
        return [
            w
            for w in (getattr(out, "windows", []) or [])
            if getattr(w, "app_name", "") in self.BROWSER_APPS
        ]

    # —— 窗口可见性（T12 实测：遮挡下 Chrome 冻结渲染，读取/操作前必须确保可见）——

    def ensure_visible(self, pid: int, window_id: int, *, attempts: int = 3) -> None:
        """确保目标窗口在当前可见桌面；失败即停（抛 RuntimeError）。

        手段（实测）：LaunchServices 激活浏览器（`open -b`，可跨 Space 唤起）
        + 精确窗口前置（bring_to_front）→ list_windows 验证 on_screen；重试
        attempts 次仍不可见则抛错（调用方按失败处理）。
        """
        for index in range(attempts):
            if self._is_window_on_screen(pid, window_id):
                return
            self._activate_browser(pid)
            try:
                self._bridge.run(
                    self._driver.call_tool(
                        "bring_to_front",
                        json.dumps({"pid": pid, "window_id": window_id}),
                    )
                )
            except Exception:
                pass
            if not self._is_window_on_screen(pid, window_id):
                self._raise_via_window_menu(pid, window_id)
            time.sleep(1.5 if index == 0 else 1.0)
        if not self._is_window_on_screen(pid, window_id):
            raise RuntimeError(
                f"窗口 {window_id}（pid {pid}）无法置于可见桌面：多 Space 遮挡或窗口已消失"
            )

    def _raise_via_window_menu(self, pid: int, window_id: int) -> bool:
        """跨 Space 精确前置：经「窗口」菜单 makeKeyAndOrderFront（实测配方）。

        需要先激活应用且存在一个在屏窗口作为菜单上下文；失败返回 False。
        """
        title = ""
        context_wid = None
        for w in self._browser_windows_all():
            if getattr(w, "pid", None) != pid:
                continue
            if getattr(w, "window_id", None) == window_id:
                title = str(getattr(w, "title", "") or "").strip()
            elif getattr(w, "is_on_screen", False) and context_wid is None:
                context_wid = getattr(w, "window_id", None)
        if not title or context_wid is None:
            return False
        try:
            self._bridge.run(
                self._driver.call_tool(
                    "invoke_menu",
                    json.dumps({"pid": pid, "window_id": context_wid, "path": ["窗口", title]}),
                )
            )
        except Exception:
            return False
        time.sleep(1.5)
        return self._is_window_on_screen(pid, window_id)

    def _is_window_on_screen(self, pid: int, window_id: int) -> bool:
        for w in self._browser_windows_all():
            if getattr(w, "pid", None) == pid and getattr(w, "window_id", None) == window_id:
                return bool(getattr(w, "is_on_screen", False))
        return False

    def _browser_windows_all(self) -> list[Any]:
        """全部窗口（不限浏览器应用；用于按 pid/wid 精确查询）。"""
        try:
            out = self._bridge.run(self._list_windows_async())
        except Exception:
            return []
        return list(getattr(out, "windows", []) or [])

    def _activate_browser(self, pid: int) -> None:
        """激活浏览器应用（open -b <bundle>；跨 Space 有效，已激活时幂等）。"""
        bundle = "com.google.Chrome"
        if pid:
            for w in self._browser_windows_all():
                if getattr(w, "pid", None) == pid:
                    bundle = self.BROWSER_BUNDLE_IDS.get(
                        str(getattr(w, "app_name", "")), bundle
                    )
                    break
        try:
            subprocess.run(
                ["open", "-b", bundle], capture_output=True, check=False, timeout=10
            )
        except Exception:
            pass

    def _title_matches(self, window: Any) -> bool:
        title = str(getattr(window, "title", "") or "").lower()
        return any(m.lower() in title for m in self.LIEPIN_TITLE_MARKERS)

    def _probe_liepin(self, pid: int, wid: int) -> bool:
        """浅探单个窗口：query 过滤取树（含地址栏），命中 URL_MARKER 即可。"""
        st = self.window_state(pid, wid, query=self.URL_MARKER)
        return self.URL_MARKER in self._tree_text(st)

    @staticmethod
    def _tree_text(state: Any) -> str:
        return str(getattr(state, "tree_markdown", "") or "").lower()

    async def _list_windows_async(self) -> Any:
        from cua_driver import ListWindowsInput

        return await self._driver.list_windows(
            ListWindowsInput(pid=None, on_screen_only=None)
        )

    def window_state(
        self,
        pid: int,
        window_id: int,
        *,
        screenshot_out: Path | None = None,
        include_tree: bool = True,
        query: str | None = None,
        max_elements: int | None = None,
        max_depth: int | None = None,
    ) -> Any:
        """窗口状态（WindowStateOutput：tree_markdown/elements + 可选截图）。

        query 为 AX 元素文本过滤（浅探提速：实测 0.6s vs 全树约 1-2s）。
        截图要求窗口可见；AX 树读取无此限制。
        """
        self._ensure_session()
        return self._bridge.run(
            self._window_state(
                pid, window_id, screenshot_out, include_tree, query, max_elements, max_depth
            )
        )

    async def _window_state(
        self,
        pid: int,
        window_id: int,
        screenshot_out: Path | None,
        include_tree: bool,
        query: str | None,
        max_elements: int | None,
        max_depth: int | None,
    ) -> Any:
        from cua_driver import GetWindowStateInput

        return await self._driver.get_window_state(
            GetWindowStateInput(
                pid=pid,
                window_id=window_id,
                session=None,
                query=query,
                include_accessibility_tree=include_tree,
                include_screenshot=screenshot_out is not None,
                screenshot_out_file=str(screenshot_out) if screenshot_out else None,
                max_elements=max_elements,
                max_depth=max_depth,
                max_dimension=None,
                max_image_dimension=None,
            )
        )

    # —— 聊天页 / 批量简历页导航与读取（T12 步骤③ 实测路径）——

    def _live_state(self, pid: int, wid: int) -> Any:
        """取最新窗口 state（元素索引逐快照漂移：后续一律即时定位）。

        风控页闸口：检测到「账号行为异常」安全验证页立即抛
        RiskControlDetectedError（worker 转人工，不重试；绝不自动绕过验证）。
        """
        state = self.window_state(pid, wid)
        text = str(getattr(state, "tree_markdown", "") or "")
        if has_risk_control(text):
            raise RiskControlDetectedError(
                "检测到猎聘风控/安全验证页（账号行为异常）——停止操作，人工在桌面完成验证"
            )
        return state

    def _current_url(self, state: Any) -> str:
        for e in (getattr(state, "elements", []) or []):
            if getattr(e, "role", "") == "AXTextField" and "地址" in str(getattr(e, "label", "") or ""):
                return str(getattr(e, "value", "") or "")
        return ""

    def _find(
        self,
        state: Any,
        *,
        role: str | None = None,
        label: str | None = None,
        label_contains: str | None = None,
        max_index: int | None = None,
    ) -> Any | None:
        """按 role/label 即时定位元素；max_index 仅作范围过滤（禁跨快照复用索引）。"""
        for e in (getattr(state, "elements", []) or []):
            idx = getattr(e, "element_index", 0)
            if max_index is not None and idx >= max_index:
                continue
            if role is not None and str(getattr(e, "role", "")) != role:
                continue
            lbl = str(getattr(e, "label", "") or "")
            if label is not None and lbl != label:
                continue
            if label_contains is not None and label_contains not in lbl:
                continue
            return e
        return None

    def _press(self, pid: int, wid: int, element: Any) -> None:
        """元素级 AXPress（实测对导航链接/按钮/选项卡生效；后台执行不打扰用户）。"""
        from cua_driver import ActionTarget, ClickInput, ClickPosition, InputDeliveryMode

        try:
            self._bridge.run(
                self._driver.click(
                    ClickInput(
                        target=ActionTarget.WINDOW(pid, wid),
                        position=ClickPosition.ELEMENT(element.element_token),
                        delivery_mode=InputDeliveryMode.BACKGROUND,
                        session=None,
                        button=None,
                        count=None,
                    )
                )
            )
        except Exception as e:
            mapped = _translate_driver_error(e)
            if mapped is e:
                raise
            raise mapped from e

    def _tab_names(self, state: Any) -> list[str]:
        """批量页顶部候选人选项卡名。

        实测结构：选项卡是顶层 webarea 的直接子级静态文本，直到嵌套详情
        webarea（也是直接子级）出现为止；账户名「陈智旭」排除。
        """
        els = list(getattr(state, "elements", []) or [])
        top = next((e for e in els if str(getattr(e, "role", "")) == "AXWebArea"), None)
        if top is None:
            return []
        names: list[str] = []
        for e in els:
            if getattr(e, "element_index", 0) <= top.element_index:
                continue
            if getattr(e, "parent_index", None) != top.element_index:
                continue
            role = str(getattr(e, "role", ""))
            if role == "AXWebArea":
                break  # 嵌套详情 webarea：选项卡区结束
            if role != "AXStaticText":
                continue
            lbl = str(getattr(e, "label", "") or "").strip()
            if not lbl or lbl == "陈智旭" or lbl in names:
                continue
            names.append(lbl)
        return names

    def _press_tab(self, pid: int, wid: int, name: str) -> Any:
        """按名字即时定位并点击候选人选项卡，返回切换后的最新 state。"""
        state = self._live_state(pid, wid)
        els = list(getattr(state, "elements", []) or [])
        top = next((e for e in els if str(getattr(e, "role", "")) == "AXWebArea"), None)
        tab = None
        if top is not None:
            for e in els:
                if getattr(e, "element_index", 0) <= top.element_index:
                    continue
                if getattr(e, "parent_index", None) != top.element_index:
                    continue
                if str(getattr(e, "role", "")) == "AXWebArea":
                    break
                if (
                    str(getattr(e, "role", "")) == "AXStaticText"
                    and str(getattr(e, "label", "") or "").strip() == name
                ):
                    tab = e
                    break
        if tab is None:
            raise LocatorFailedError(f"批量页未找到选项卡「{name}」（页面结构变化？）")
        self._press(pid, wid, tab)
        time.sleep(self.SETTLE_SECONDS)
        return self._live_state(pid, wid)

    def _detail_liepin_id(self, state: Any) -> str | None:
        """详情「简历编号」值（标签 + 冒号后的首个文本；liepin_user_id 真实来源）。"""
        els = list(getattr(state, "elements", []) or [])
        for pos, e in enumerate(els):
            if str(getattr(e, "label", "") or "") == "简历编号":
                for nxt in els[pos + 1:pos + 4]:
                    t = str(getattr(nxt, "label", "") or "").strip()
                    if t and t not in (":", "："):
                        return t
        return None

    def _value_after_icon(self, state: Any, icon: str) -> str | None:
        """字段图标锚点（environment/work/education/file-search）后的首个文本值。"""
        els = list(getattr(state, "elements", []) or [])
        for pos, e in enumerate(els):
            if str(getattr(e, "role", "")) == "AXImage" and str(getattr(e, "label", "") or "") == icon:
                for nxt in els[pos + 1:pos + 4]:
                    if str(getattr(nxt, "role", "")) == "AXStaticText":
                        t = str(getattr(nxt, "label", "") or "").strip()
                        if t:
                            return t
        return None

    def _detail_salary(self, state: Any) -> str:
        """求职意向列表中的薪资项（形如 11-22k×12薪）；页面确实未提供时返回空串。"""
        els = list(getattr(state, "elements", []) or [])
        for pos, e in enumerate(els):
            if str(getattr(e, "label", "") or "") == "求职意向":
                for nxt in els[pos + 1:pos + 40]:
                    t = str(getattr(nxt, "label", "") or "").strip()
                    if "k" in t.lower() and ("薪" in t or "-" in t):
                        return t
                return ""
        return ""

    def _ensure_visible_and_resolved(self) -> tuple[int, int]:
        """resolve + ensure_visible 组合（读链/写链统一入口）。"""
        found = self._resolve_liepin_window()
        if found is None:
            raise RuntimeError("未找到猎聘窗口（浏览器未打开或未登录）")
        pid, wid, _ = found
        self.ensure_visible(pid, wid)
        time.sleep(0.8)  # 前置后留渲染静置（实测：切 Space 唤起后页面重绘需 1s 级）
        return pid, wid

    def _tab_names_retry(self, pid: int, wid: int, state: Any) -> list[str]:
        """选项卡名单（带一次重取重试：防前置窗口后页面重绘未完成）。"""
        names = self._tab_names(state)
        if names:
            return names
        time.sleep(self.SETTLE_SECONDS)
        return self._tab_names(self._live_state(pid, wid))

    def _candidate_detail(
        self, pid: int, wid: int, candidate_liepin_id: str
    ) -> tuple[Any, str]:
        """定位候选人详情（带懒渲染自愈）→ (state, 选项卡名)。

        步骤：确保批量页 → 逐选项卡按「简历编号」匹配；未命中（懒渲染/状态漂移）
        时重载页面一次再试；仍失败抛错（失败即停）。
        """
        state = self._reach_batch_page(pid, wid)
        names = self._tab_names_retry(pid, wid, state)
        for attempt in (1, 2):
            for name in names:
                state = self._press_tab(pid, wid, name)
                if self._detail_liepin_id(state) == candidate_liepin_id:
                    return state, name
            if attempt == 1:
                self._reload_page(pid, wid)
                time.sleep(self.SETTLE_SECONDS + 1.0)
                state = self._live_state(pid, wid)
                names = self._tab_names_retry(pid, wid, state)
        raise LocatorFailedError(
            f"批量页未找到简历编号 {candidate_liepin_id} 对应候选人（含重载重试）"
        )

    def _reload_page(self, pid: int, wid: int) -> None:
        """重载当前页（懒渲染自愈：面板滚动/切换后部分区块不再进 AX 缓存）。"""
        state = self._live_state(pid, wid)
        btn = self._find(state, role="AXButton", label="重新加载")
        if btn is not None:
            self._press(pid, wid, btn)
        time.sleep(1.0)

    def _back_to_chat_page(self, pid: int, wid: int) -> None:
        """切回「在线沟通」标签页（消息列表页）——list_unread 收尾语义：
        worker 后置校验判据=「消息列表页已打开，可见未读会话列表」（实测）。"""
        state = self._live_state(pid, wid)
        tab = self._find(state, role="AXRadioButton", label="在线沟通")
        if tab is None:
            raise LocatorFailedError("未找到「在线沟通」标签页（无法回到消息列表页）")
        self._press(pid, wid, tab)
        time.sleep(self.SETTLE_SECONDS)
        state = self._live_state(pid, wid)
        if self.CHAT_PATH not in self._current_url(state):
            raise LocatorFailedError(f"切换后未回到消息列表页（URL={self._current_url(state)[:120]}）")

    def _attachment_filename(self, state: Any) -> str | None:
        """详情附件文件名（取带附件扩展名的最靠后静态文本=详情区条目）。"""
        name = None
        for e in (getattr(state, "elements", []) or []):
            if str(getattr(e, "role", "")) != "AXStaticText":
                continue
            lbl = str(getattr(e, "label", "") or "").strip()
            if lbl.lower().endswith(self.ATTACHMENT_EXTS):
                name = lbl
        return name

    def _download_button(self, state: Any) -> Any | None:
        """附件区「下载」按钮（AXButton label=下载，实测位于附件文件名旁）。"""
        return self._find(state, role="AXButton", label="下载")

    def _reach_batch_page(self, pid: int, wid: int) -> Any:
        """确保位于批量预览简历页：已有批量标签页优先切回；否则经聊天页→「浏览简历」。

        实测：批量页以新标签页打开（标签名「批量预览简历」）——逐任务复用标签页
        切换（快）优于重复点击「浏览简历」（会新开页）。「浏览简历」不可见时
        （无会话选中/未展开通知面板）先点「收到简历」通知行露出消息面板。
        任一锚点缺失即抛错（失败即停）。
        """
        state = self._live_state(pid, wid)
        url = self._current_url(state)
        if self.BATCH_PATH in url:
            return state
        batch_tab = self._find(state, role="AXRadioButton", label="批量预览简历")
        if batch_tab is not None:
            self._press(pid, wid, batch_tab)
            time.sleep(self.SETTLE_SECONDS)
            state = self._live_state(pid, wid)
            if self.BATCH_PATH in self._current_url(state):
                return state
            url = self._current_url(state)
        if self.CHAT_PATH not in url:
            nav = self._find(state, role="AXLink", label_contains="沟通")
            if nav is None:
                raise LocatorFailedError(f"不在聊天/批量页且未找到「沟通」导航（URL={url[:120]}）")
            self._press(pid, wid, nav)
            time.sleep(self.SETTLE_SECONDS)
            state = self._live_state(pid, wid)
            url = self._current_url(state)
            if self.CHAT_PATH not in url and self.BATCH_PATH not in url:
                raise LocatorFailedError(f"点击「沟通」后未到达聊天页（URL={url[:120]}）")
        if self.BATCH_PATH in url:
            return state
        btn = self._find(state, role="AXButton", label="浏览简历")
        if btn is None:
            # T12 补丁（2026-10-06）：无批量标签页且「浏览简历」不可见——先点
            # 「收到简历」通知行露出消息面板（实测底部含「浏览简历」；即批量页
            # pgRef=b_pc_im_message_capply_batch_view_btn 入口）。
            if not self._open_batch_notification(pid, wid, state):
                raise LocatorFailedError("聊天页未找到「浏览简历」按钮（会话未选中或页面结构变化）")
            state = self._live_state(pid, wid)
            btn = self._find(state, role="AXButton", label="浏览简历")
        if btn is None:
            raise LocatorFailedError("点击通知行后仍未找到「浏览简历」按钮（页面结构变化？）")
        # 实测（2026-10-06）：「浏览简历」为批量动作——未勾选候选人时点击无效果；
        # 先勾底部「全部」复选框（AXCheckBox，与顶部同名筛选 AXRadioButton 区分；
        # value=1 已勾选则跳过，避免反选）。
        check_all = self._find(state, role="AXCheckBox", label="全部")
        if check_all is not None and str(getattr(check_all, "value", "0")) != "1":
            self._press(pid, wid, check_all)
            time.sleep(0.5)
        self._press(pid, wid, btn)
        time.sleep(self.SETTLE_SECONDS + 1.0)
        state = self._live_state(pid, wid)
        if self.BATCH_PATH not in self._current_url(state):
            raise LocatorFailedError(
                f"点击「浏览简历」后未进入批量页（URL={self._current_url(state)[:120]}）"
            )
        return state

    def _open_batch_notification(self, pid: int, wid: int, state: Any) -> bool:
        """点「收到简历」批次通知行露出消息面板；找到并已点击返回 True，未找到行返回 False。

        实测（2026-10-06）：通知行元素为 AXStaticText（无 AXPress）——经 System
        Events 坐标点击；点击后面板底部出现「浏览简历」（批量页入口）。锚点=通知行
        预览文本「…人的简历」（唯一性高于标题「收到简历」）。
        """
        row = None
        for e in getattr(state, "elements", []) or []:
            if str(getattr(e, "role", "")) != "AXStaticText":
                continue
            lbl = str(getattr(e, "label", "") or "")
            if "人的简历" in lbl and getattr(e, "frame", None) is not None:
                row = e
                break
        if row is None:
            return False
        fr = row.frame
        cy = fr.y + max(float(getattr(fr, "h", 0) or 0), 16.0) / 2.0
        self._click_point(pid, wid, fr.x + fr.w / 2.0, cy)
        time.sleep(self.SETTLE_SECONDS + 0.5)
        return True

    def _click_point(self, pid: int, wid: int, x: float, y: float) -> None:
        """坐标点击（System Events；实测通道：无 AXPress 的列表行元素）。

        前提：窗口可见（调用方 ensure_visible）且目标点在窗口内；失败即停。
        """
        script = f'tell application "System Events" to click at {{{x:.0f}, {y:.0f}}}'
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"坐标点击失败（osascript rc={result.returncode}）：{result.stderr.strip()[:200]}"
            )

    def click_text_contains(self, text: str) -> bool:
        """LLM 兜底通道：按 label 包含定位元素并后台 AXPress；找不到返回 False。

        窗口/风控异常仍上抛（失败即停优先于兜底）；命中即执行（调用方复查效果）。
        """
        target = str(text or "").strip()
        if not target:
            return False
        pid, wid = self._ensure_visible_and_resolved()
        state = self._live_state(pid, wid)
        for e in getattr(state, "elements", []) or []:
            lbl = str(getattr(e, "label", "") or "")
            if target in lbl:
                self._press(pid, wid, e)
                time.sleep(self.SETTLE_SECONDS)
                return True
        return False

    def click_at_screenshot_px(self, x_px: float, y_px: float) -> bool:
        """LLM 兜底通道：截图像素坐标 → 屏幕点（÷SCREENSHOT_PX_PER_POINT）→ 坐标点击。

        点须落在目标窗口 bounds 内，否则不点击返回 False（防打错窗口）。
        """
        pid, wid = self._ensure_visible_and_resolved()
        bounds = self._window_bounds(pid, wid)
        if bounds is None:
            return False
        bx, by, bw, bh = bounds
        x_pt = x_px / self.SCREENSHOT_PX_PER_POINT
        y_pt = y_px / self.SCREENSHOT_PX_PER_POINT
        if not (bx <= x_pt <= bx + bw and by <= y_pt <= by + bh):
            return False
        self._click_point(pid, wid, x_pt, y_pt)
        time.sleep(self.SETTLE_SECONDS)
        return True

    def _window_bounds(
        self, pid: int, window_id: int
    ) -> tuple[float, float, float, float] | None:
        """目标窗口 bounds（屏幕点坐标 x/y/w/h）；未找到返回 None。"""
        for w in self._browser_windows_all():
            if getattr(w, "pid", None) == pid and getattr(w, "window_id", None) == window_id:
                b = getattr(w, "bounds", None)
                if b is None:
                    return None
                return (float(b.x), float(b.y), float(b.width), float(b.height))
        return None

    # —— 协议方法（按 runbook §4 顺序逐步校准）——

    def check_login(self) -> bool:
        """① 登录态：定位猎聘窗口 → 全树标记判定（2026-10-06 真实页面校准）。

        口径：窗口存在（地址栏含 liepin.com）且树含后台导航锚点（人才推荐/
        搜索人才/职位管理/招聘工作台）任一 → True；登录页锚点（扫码/密码/短信/
        验证码登录）出现 → False；窗口不存在或异常 → False（未知按未登录处理：
        pipeline 侧只暂停派发 + 告警，方向安全）；风控页 → 抛
        RiskControlDetectedError（worker 写全局熔断标志并转人工，绝不重试）。
        """
        try:
            found = self._resolve_liepin_window()
        except Exception:
            return False
        if found is None:
            return False
        try:
            self.ensure_visible(found[0], found[1])  # 最佳努力：让后置校验截图能拍到页面
        except Exception:
            pass  # 可见性失败不阻断判定（树读取在遮挡下也常可用）
        tree = self._tree_text(found[2])
        if has_risk_control(tree):
            # 风控页：统一信号（worker 写全局熔断标志 + 转人工，绝不重试；2026-10-06 二次事件）
            raise RiskControlDetectedError(
                "检测到猎聘风控/安全验证页（账号行为异常）——停止操作，人工完成安全验证"
            )
        if any(m in tree for m in self.LOGIN_PAGE_MARKERS):
            return False
        return any(m in tree for m in self.BACKEND_MARKERS)

    def list_unread_conversations(self) -> list[str]:
        """③ 未读简历列表：批量页遍历选项卡收集「简历编号」（liepin_user_id）。

        M1 语义映射（披露）：真实批量页对应聊天页「收到简历」未读通知批次——当前
        返回批次内全部候选人 ID，去重交由 pipeline（jc 存在性幂等）；未读精细筛选
        待 M2。收集完成后切回「在线沟通」消息列表页（worker 后置校验判据语义）；
        任一环节失败抛错（失败即停，不猜）。
        """
        pid, wid = self._ensure_visible_and_resolved()
        state = self._reach_batch_page(pid, wid)
        names = self._tab_names_retry(pid, wid, state)
        if not names:
            raise LocatorFailedError("批量页未发现候选人选项卡（页面结构变化？）")
        ids: list[str] = []
        for name in names:
            state = self._press_tab(pid, wid, name)
            lid = self._detail_liepin_id(state)
            if lid:
                ids.append(lid)
        if not ids:
            raise LocatorFailedError("批量页未读到任何简历编号（页面结构变化？）")
        self._back_to_chat_page(pid, wid)
        return ids

    def open_conversation(self, candidate_liepin_id: str) -> None:
        """打开候选人会话：批量页按「简历编号」定位选项卡 →「继续沟通」进入聊天浮层。

        实测详情含「继续沟通」按钮（AXPress 生效）；浮层打开后以「<姓名>的简历」
        锚点做身份校验。未找到候选人即抛错（失败即停）。
        """
        pid, wid = self._ensure_visible_and_resolved()
        state, name = self._candidate_detail(pid, wid, candidate_liepin_id)
        self._open_chat_overlay(pid, wid, state, name)

    # —— 发送链（T12 步骤④ 校准，2026-10-06）——

    def send_message(self, candidate_liepin_id: str, text: str) -> None:
        """④ 发送消息（唯一触达动作；一人一消息约束由调用方/pipeline 保证）。

        实测链路：批量页定位候选人（简历编号）→「继续沟通」打开聊天浮层 →
        身份锚点校验（「<姓名>的简历」）→ 清空输入框（cmd+A+Delete）→
        type_text 输入并 AX 读回校验 → 点「发送」（仅输入非空时该按钮存在）→
        校验消息上屏（线程出现同文本）。任一步失败即抛错（失败即停）；发送后
        未确认上屏亦抛错且禁止自动重试（消息可能已发出，交人工/账目核对）。
        """
        if not text.strip():
            raise ValueError("发送文本为空，拒绝执行")
        pid, wid = self._ensure_visible_and_resolved()
        state, name = self._candidate_detail(pid, wid, candidate_liepin_id)
        self._open_chat_overlay(pid, wid, state, name)
        self._fill_and_send(pid, wid, text)

    def _open_chat_overlay(self, pid: int, wid: int, state: Any, name: str) -> None:
        """在候选人详情页点「继续沟通」打开聊天浮层，并做身份锚点校验。"""
        btn = self._find(state, role="AXButton", label_contains="继续沟通")
        if btn is None:
            raise RuntimeError("候选人详情未找到「继续沟通」按钮（页面结构变化？）")
        self._press(pid, wid, btn)
        time.sleep(self.SETTLE_SECONDS)
        state = self._live_state(pid, wid)
        if self._find(state, role="AXStaticText", label=f"{name}的简历") is None:
            raise RuntimeError(f"聊天浮层身份校验失败：未找到「{name}的简历」锚点（拒绝发送）")

    def _fill_and_send(self, pid: int, wid: int, text: str) -> None:
        """清空 → 输入（读回校验）→ 发送 → 上屏校验；失败即停、绝不盲目重发。"""
        state = self._live_state(pid, wid)
        box = self._find(state, role="AXTextArea")
        if box is None:
            raise RuntimeError("聊天浮层未找到输入框（页面结构变化？）")
        self._press(pid, wid, box)
        time.sleep(0.4)
        self._press_key(pid, wid, "a", modifiers=["cmd"])  # 清残留草稿
        time.sleep(0.2)
        self._press_key(pid, wid, "delete")
        time.sleep(0.4)
        actual = ""
        for _ in range(3):
            self._type_text(pid, wid, box, text)
            time.sleep(0.8)
            state = self._live_state(pid, wid)
            cur = self._find(state, role="AXTextArea")
            actual = str(getattr(cur, "value", "") or "") if cur is not None else ""
            if actual == text:
                break
            box = cur if cur is not None else box
        if actual != text:
            raise RuntimeError(f"输入校验失败：期望 {text!r}（拒绝发送）")
        send_btn = self._find(state, role="AXButton", label="发送")
        if send_btn is None:
            raise RuntimeError("未找到「发送」按钮（页面结构变化？）")
        self._press(pid, wid, send_btn)
        time.sleep(self.SETTLE_SECONDS)
        state = self._live_state(pid, wid)
        if self._find(state, role="AXStaticText", label=text) is None:
            raise RuntimeError("发送后未确认消息上屏——消息可能已发出，禁止重试，请人工/账目核对")

    def _press_key(
        self, pid: int, wid: int, key: str, *, modifiers: list[str] | None = None
    ) -> None:
        """键盘按键（后台注入；输入框辅助操作：cmd+A 全选、delete 清空等）。"""
        payload: dict[str, Any] = {
            "key": key,
            "pid": pid,
            "window_id": wid,
            "delivery_mode": "background",
        }
        if modifiers:
            payload["modifiers"] = modifiers
        try:
            self._bridge.run(self._driver.call_tool("press_key", json.dumps(payload)))
        except Exception as e:
            mapped = _translate_driver_error(e)
            if mapped is e:
                raise
            raise mapped from e
        time.sleep(0.2)

    def _type_text(self, pid: int, wid: int, element: Any, text: str) -> None:
        """向元素输入文本（实测：聚焦后经 CGEvent 注入，AX 读回作辅助校验）。"""
        try:
            self._bridge.run(
                self._driver.call_tool(
                    "type_text",
                    json.dumps(
                        {
                            "text": text,
                            "pid": pid,
                            "window_id": wid,
                            "element_token": element.element_token,
                            "delivery_mode": "background",
                        }
                    ),
                )
            )
        except Exception as e:
            mapped = _translate_driver_error(e)
            if mapped is e:
                raise
            raise mapped from e

    def read_online_resume(self, candidate_liepin_id: str) -> tuple[bytes, MinimalResume]:
        """③ 读在线简历：批量页定位候选人 → 提取 7 字段 + 桌面截图。

        实测路径：聊天页 →「浏览简历」→ 批量预览简历页 → 逐选项卡 AXPress → 读
        「简历编号」比对目标 → 图标锚点提取字段（environment/work/education/
        file-search + 求职意向薪资）；截图用桌面捕获（窗口捕获在 capture 绑定期
        不稳定）。字段缺失/查找失败即抛错（失败即停）；错误信息携带状态供人工/
        视觉复核（视觉兜底为后续接线点，驱动当前无大脑实例）。
        """
        pid, wid = self._ensure_visible_and_resolved()
        state, name = self._candidate_detail(pid, wid, candidate_liepin_id)
        city = self._value_after_icon(state, "environment")
        years = self._value_after_icon(state, "work")
        edu_full = self._value_after_icon(state, "education")
        summary = self._value_after_icon(state, "file-search")
        education = edu_full.split("·")[-1].strip() if edu_full else None
        missing = [
            key
            for key, value in (
                ("city", city),
                ("years_of_experience", years),
                ("education", education),
                ("experience_summary", summary),
            )
            if not value
        ]
        if missing:
            raise LocatorFailedError(
                f"候选人 {candidate_liepin_id} 字段缺失 {missing}（页面结构可能变化，待视觉复核）"
            )
        screenshot = self.capture_desktop_png()
        return screenshot, MinimalResume(
            name=name,
            liepin_user_id=candidate_liepin_id,
            education=education,
            years_of_experience=years,
            city=city,
            salary=self._detail_salary(state),
            experience_summary=summary,
        )

    def check_attachment(self, candidate_liepin_id: str) -> bool:
        """③ 附件存在判定：候选人详情「附件简历」区是否有可下载附件。

        实测口径（2026-10-06）：详情加载证据=「简历编号」可读（_candidate_detail
        保证）；存在证据=附件文件名（.pdf/.doc/…）或「下载」按钮。详情已加载而
        无附件元素 → False；详情无法加载 → 抛错（失败即停）。负例文本锚点待后续
        用无附件候选人校准补充。
        """
        pid, wid = self._ensure_visible_and_resolved()
        state, _ = self._candidate_detail(pid, wid, candidate_liepin_id)
        return (
            self._attachment_filename(state) is not None
            or self._download_button(state) is not None
        )

    def download_attachment(self, candidate_liepin_id: str) -> tuple[bytes, str]:
        """③ 下载附件简历：详情点「下载」→ 从浏览器下载目录读回 (字节, 文件名)。

        R7：返回 (字节, 文件名)——pipeline artifact 端点要求 filename 表单字段。
        实测（2026-10-06）：下载落盘 Chrome 默认目录 ~/Downloads（PDF，先于
        .crdownload 临时文件）；轮询等待新文件出现（默认 20s，取大小稳定后读回）；
        失败即停（超时抛错交人工检查，绝不返回空字节）。文件保留在下载目录（驱动
        不清理；如需归档由 pipeline artifact 链路负责）。
        """
        pid, wid = self._ensure_visible_and_resolved()
        state, _ = self._candidate_detail(pid, wid, candidate_liepin_id)
        btn = self._download_button(state)
        if btn is None:
            raise RuntimeError("候选人详情未找到「下载」按钮（无附件或页面结构变化）")
        downloads = Path.home() / "Downloads"
        before = {p.name for p in downloads.glob("*")} if downloads.exists() else set()
        self._press(pid, wid, btn)
        deadline = time.time() + 20
        while time.time() < deadline:
            time.sleep(1.0)
            if not downloads.exists():
                continue
            for p in downloads.glob("*"):
                if p.name in before or p.name.endswith(".crdownload"):
                    continue
                size1 = p.stat().st_size
                if size1 == 0:
                    continue
                time.sleep(0.4)
                size2 = p.stat().st_size
                if size1 != size2 or size2 == 0:
                    continue  # 仍在写入
                return p.read_bytes(), p.name
        raise RuntimeError("下载超时（20s）：下载目录未出现新文件——请人工检查浏览器下载")
