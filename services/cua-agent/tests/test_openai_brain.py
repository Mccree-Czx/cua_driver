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


def test_request_shape_model_prompt_image_and_schema():
    """请求形状：模型名、criteria 入 prompt、截图以 base64 data URL 附给模型、
    response_format 施加 {ok, reason} JSON Schema。"""
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
    schema = kwargs["response_format"]["json_schema"]["schema"]
    assert schema["required"] == ["ok", "reason"]
    assert schema["properties"]["ok"] == {"type": "boolean"}
    assert schema["properties"]["reason"] == {"type": "string"}


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
