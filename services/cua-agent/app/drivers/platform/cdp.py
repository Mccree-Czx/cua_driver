"""CDP（Chrome DevTools Protocol）输入通道：Windows 上替代 SDK CGEvent 的文本/按键注入。

前提：Chrome 以 --remote-debugging-port=9222 启动（本机 2026-10-09 已就绪）。
经 /json 找到猎聘页目标，向「聊天 textarea」发 Input.insertText / Input.dispatchKeyEvent。
所有协程经 `bridge.run` 跑在 SDK 同一事件循环线程上。

实盘校准（2026-10-09）：
- 聊天输入框是 `<textarea class="ant-im-input …">`（非 contenteditable）；insertText 会
  触发 React onChange，「发送」按钮由 disabled 翻为可用。
- 清空草稿用 Ctrl+A+Delete：dispatchKeyEvent 必须用 rawKeyDown（down）/ keyUp（up）
  + windowsVirtualKeyCode / nativeVirtualKeyCode；缺键码时全选不生效。
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

import websockets

CDP_HTTP = "http://127.0.0.1:9222"

# 命名的非字母键 → (CDP code, Windows 虚拟键码)
_NAMED_KEYS: dict[str, tuple[str, int]] = {
    "delete": ("Delete", 46),
    "backspace": ("Backspace", 8),
    "enter": ("Enter", 13),
    "return": ("Enter", 13),
    "tab": ("Tab", 9),
    "escape": ("Escape", 27),
    "esc": ("Escape", 27),
    "space": ("Space", 32),
    "arrowleft": ("ArrowLeft", 37),
    "arrowright": ("ArrowRight", 39),
    "arrowup": ("ArrowUp", 38),
    "arrowdown": ("ArrowDown", 40),
    "home": ("Home", 36),
    "end": ("End", 35),
    "pageup": ("PageUp", 33),
    "pagedown": ("PageDown", 34),
}

# CDP modifiers 位掩码（Alt=1, Ctrl=2, Meta=4, Shift=8）
_MODIFIER_BITS: dict[str, int] = {
    "alt": 1,
    "ctrl": 2,
    "control": 2,
    "meta": 4,
    "cmd": 4,
    "command": 4,
    "shift": 8,
}

# 聚焦可见 textarea（聊天输入框是 ant-im textarea，非 contenteditable）
_FOCUS_TEXTAREA_JS = """
(() => { const e = [...document.querySelectorAll('textarea')].find(x => x.offsetWidth > 0);
  if (!e) return false; e.focus(); return true; })()
"""

_FOCUSED_TEXTAREA_JS = (
    "document.activeElement && document.activeElement.tagName === 'TEXTAREA'"
)
_VISIBLE_TEXTAREA_JS = (
    "[...document.querySelectorAll('textarea')].some(x => x.offsetWidth > 0)"
)


def _key_params(key: str) -> tuple[str, int]:
    """key → (CDP code, Windows 虚拟键码)；未知键抛错（不盲发）。"""
    low = key.lower()
    if low in _NAMED_KEYS:
        return _NAMED_KEYS[low]
    if len(key) == 1 and key.isalnum():
        if key.isalpha():
            return f"Key{key.upper()}", ord(key.upper())
        return f"Digit{key}", ord(key)
    raise ValueError(f"不支持的按键：{key!r}")


def _modifier_mask(modifiers: list[str] | None) -> int:
    mask = 0
    for m in modifiers or []:
        mask |= _MODIFIER_BITS.get(m.lower(), 0)
    return mask


class _Session:
    """一个已连 CDP 目标的命令/求值通道（id 自增，跳过事件消息）。"""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._n = 0

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._n += 1
        mid = self._n
        await self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await self._ws.recv())
            if msg.get("id") == mid:
                return msg

    async def eval(self, expression: str) -> Any:
        r = await self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return r.get("result", {}).get("result", {}).get("value")

    async def close(self) -> None:
        await self._ws.close()


class CdpClient:
    """CDP 输入通道。同步入口，内部经 bridge.run 跑协程。"""

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge

    def insert_text(self, text: str) -> None:
        self._bridge.run(self._insert_text(text))

    def press_key(self, key: str, modifiers: list[str] | None = None) -> None:
        self._bridge.run(self._press_key(key, modifiers))

    # —— 内部 ——

    @staticmethod
    def _http_json(path: str) -> list[dict[str, Any]]:
        try:
            with urllib.request.urlopen(f"{CDP_HTTP}{path}", timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as e:  # noqa: BLE001 - 统一为可读的 RuntimeError
            raise RuntimeError(f"CDP 端点不可达（{CDP_HTTP}{path}）：{e}") from e

    async def _connect_chat_page(self) -> _Session:
        targets = self._http_json("/json")
        pages = [t for t in targets if t.get("type") == "page" and "liepin" in t.get("url", "")]
        if not pages:
            raise RuntimeError("未找到猎聘页面（CDP /json 无 liepin 目标）")
        # 优先：activeElement 是 textarea 的页（SDK 刚点击聚焦的那个标签）
        for t in pages:
            sess = await self._connect_session(t["webSocketDebuggerUrl"])
            if await sess.eval(_FOCUSED_TEXTAREA_JS):
                return sess
            await sess.close()
        # 次选：有可见 textarea 的页（聊天浮层已打开）
        for t in pages:
            sess = await self._connect_session(t["webSocketDebuggerUrl"])
            if await sess.eval(_VISIBLE_TEXTAREA_JS):
                return sess
            await sess.close()
        # 兜底：第一个猎聘页（聚焦 textarea 交由操作前的 focus 步骤兜住）
        return await self._connect_session(pages[0]["webSocketDebuggerUrl"])

    @staticmethod
    async def _connect_session(url: str) -> _Session:
        try:
            ws = await websockets.connect(url, max_size=32 * 1024 * 1024, open_timeout=10)
        except Exception as e:  # noqa: BLE001 - 统一为可读的 RuntimeError
            raise RuntimeError(f"CDP WebSocket 连接失败：{e}") from e
        return _Session(ws)

    async def _insert_text(self, text: str) -> None:
        sess = await self._connect_chat_page()
        try:
            await sess.eval(_FOCUS_TEXTAREA_JS)
            await sess.call("Input.insertText", {"text": text})
        finally:
            await sess.close()

    async def _press_key(self, key: str, modifiers: list[str] | None) -> None:
        sess = await self._connect_chat_page()
        try:
            await sess.eval(_FOCUS_TEXTAREA_JS)
            code, vk = _key_params(key)
            mask = _modifier_mask(modifiers)
            for typ in ("rawKeyDown", "keyUp"):
                await sess.call(
                    "Input.dispatchKeyEvent",
                    {
                        "type": typ,
                        "key": key,
                        "code": code,
                        "windowsVirtualKeyCode": vk,
                        "nativeVirtualKeyCode": vk,
                        "modifiers": mask,
                    },
                )
        finally:
            await sess.close()
