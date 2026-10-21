"""ingest 令牌鉴权（`deps.require_ingest_token`）。

2026-10-21 配上真令牌后，原来抛裸 `PermissionError` 的路径第一次被走到
（FastAPI 回 500）——钉住三个分支的行为，防止再退化。
"""

import pytest
from fastapi import HTTPException

import app.deps as deps
from app.config import Settings
from app.deps import require_ingest_token


@pytest.fixture
def token_set(monkeypatch):
    """把 `get_settings` 换成指定令牌的配置。"""

    def _set(token: str) -> None:
        monkeypatch.setattr(deps, "get_settings", lambda: Settings(ingest_token=token))

    return _set


def test_unconfigured_passes(token_set):
    """未配置 = 放行（仅限本机开发，见 deps 模块 docstring）。"""
    token_set("")
    require_ingest_token(None)  # 不抛


def test_match_passes(token_set):
    token_set("abc")
    require_ingest_token("abc")  # 不抛


def test_mismatch_is_403(token_set):
    token_set("abc")
    with pytest.raises(HTTPException) as ei:
        require_ingest_token("wrong")
    assert ei.value.status_code == 403


def test_missing_is_403(token_set):
    token_set("abc")
    with pytest.raises(HTTPException) as ei:
        require_ingest_token(None)
    assert ei.value.status_code == 403
