"""OpenAIBrain：schema 约束输出判定（ok=true/false）+ 错误 → 降级。

非法输出（非 JSON / 字段类型错 / 空 content）与供应商错误一律抛
BrainUnavailableError，调用方（T9 worker）据此降级。MockBrain 按剧本判定。
全 mock：假客户端注入，不打真实 API。
"""

import base64
from types import SimpleNamespace

import httpx
import pytest
from openai import APIError

from app.brain.mock import MockBrain
from app.brain.openai_brain import BrainUnavailableError, OpenAIBrain

MODEL = "deepseek-flash"


class FakeChat:
    """chat.completions.create 的替身：返回预设 content 或抛预设异常，并捕获 kwargs。"""

    def __init__(self, content=None, exc=None):
        self.content = content
        self.exc = exc
        self.captured = None

    def create(self, **kwargs):
        self.captured = kwargs
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


class FakeClient:
    """OpenAI 客户端的替身：仅暴露 chat.completions。"""

    def __init__(self, chat: FakeChat):
        self.chat = SimpleNamespace(completions=chat)


def make_brain(chat: FakeChat) -> OpenAIBrain:
    client = FakeClient(chat)
    return OpenAIBrain(
        base_url="http://fake-brain", api_key="fake-key", model=MODEL, client_factory=lambda: client
    )


PNG = b"\x89PNG\r\n\x1a\nfake-screenshot-bytes"


# —— schema 约束输出判定 ——


def test_ok_true():
    chat = FakeChat(content='{"ok": true, "reason": "screenshot shows login state"}')
    assert make_brain(chat).verify(PNG, "页面处于已登录状态") is True


def test_ok_false():
    chat = FakeChat(content='{"ok": false, "reason": "no login marker"}')
    assert make_brain(chat).verify(PNG, "页面处于已登录状态") is False


def test_request_shape_model_prompt_image_and_response_format():
    """请求形状：模型名、criteria 入 prompt、截图以 base64 data URL 附给模型、
    response_format 为 json_object（T12 实测：DeepSeek 不支持 json_schema，400 拒绝）。"""
    chat = FakeChat(content='{"ok": true, "reason": "yes"}')
    brain = make_brain(chat)
    brain.verify(PNG, "附件为 PDF 简历")
    kwargs = chat.captured
    assert kwargs["model"] == MODEL
    content = kwargs["messages"][0]["content"]
    assert content[0]["type"] == "text" and "附件为 PDF 简历" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    url = content[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG  # 原字节往返一致
    assert kwargs["response_format"] == {"type": "json_object"}


def test_code_fenced_json_output_tolerated():
    """解析容忍 ```json 代码围栏包裹（真实模型偶发输出形态）。"""
    chat = FakeChat(content='```json\n{"ok": true, "reason": "fenced"}\n```')
    assert make_brain(chat).verify(PNG, "criteria") is True


# —— 非法输出 → BrainUnavailableError ——


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",            # 非 JSON
        '{"ok": "maybe", "reason": "x"}',  # ok 不是合法布尔（"maybe" 不可强转）
        '{"reason": "missing ok"}',    # 缺必填字段
        None,                          # content 为 None
    ],
)
def test_invalid_output_raises(content):
    chat = FakeChat(content=content)
    with pytest.raises(BrainUnavailableError):
        make_brain(chat).verify(PNG, "criteria")


# —— 供应商错误 → BrainUnavailableError（不吞） ——


def test_provider_error_raises():
    exc = APIError(
        "upstream 500",
        request=httpx.Request("POST", "https://fake-brain/chat/completions"),
        body=None,
    )
    chat = FakeChat(exc=exc)
    with pytest.raises(BrainUnavailableError) as excinfo:
        make_brain(chat).verify(PNG, "criteria")
    assert "upstream 500" in str(excinfo.value)


# —— MockBrain 剧本判定 ——


def test_mock_brain_scripted_by_criteria():
    brain = MockBrain(script=lambda screenshot, criteria: criteria == "A")
    assert brain.verify(b"png", "A") is True
    assert brain.verify(b"png", "B") is False


def test_mock_brain_default_verdict():
    assert MockBrain().verify(b"png", "anything") is True
    assert MockBrain(default=False).verify(b"png", "anything") is False


# —— suggest：读取链兜底诊断（2026-10-06 新增）——


def test_suggest_parses_structured_suggestion():
    chat = FakeChat(
        content=(
            '{"diagnosis": "需先勾选全部候选人", "action": "click_text",'
            ' "target": "全部", "confidence": 0.85}'
        )
    )
    suggestion = make_brain(chat).suggest(PNG, "任务 list_unread 失败：未找到浏览简历按钮")
    assert suggestion.action == "click_text"
    assert suggestion.target == "全部"
    assert suggestion.confidence == 0.85


def test_suggest_request_shape_and_fenced_output():
    """请求形状：context 入 prompt、截图 base64、json_object；围栏输出容忍。"""
    chat = FakeChat(
        content='```json\n{"diagnosis": "d", "action": "none", "target": "", "confidence": 0.1}\n```'
    )
    brain = make_brain(chat)
    suggestion, usage = brain.suggest_with_usage(PNG, "任务 read_resume 失败：锚点缺失")
    assert suggestion.action == "none"
    assert usage.total_tokens == 0  # 假响应无 usage → 零记账
    kwargs = chat.captured
    assert kwargs["model"] == MODEL
    text = kwargs["messages"][0]["content"][0]["text"]
    assert "任务 read_resume 失败：锚点缺失" in text
    assert kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    "content",
    [
        "not json",  # 非 JSON
        '{"diagnosis": "d", "action": "launch_missile", "target": "x", "confidence": 1}',
        '{"action": "none"}',  # 缺 diagnosis
        None,
    ],
)
def test_suggest_invalid_output_raises(content):
    with pytest.raises(BrainUnavailableError):
        make_brain(FakeChat(content=content)).suggest(PNG, "ctx")


def test_mock_brain_suggest_default_none():
    suggestion = MockBrain().suggest(b"png", "ctx")
    assert suggestion.action == "none" and suggestion.confidence == 0.0
