"""hr-tools MCP 工具守卫与锚点单测（B 方案 §12 修复）。

覆盖：state_transition 触达类事件要求先有 out 流水（先落账后推进）；
锚点自动落库（last_touch_at / resume_requested_at / resume_downloaded_at）；
interaction_log 拒绝第二条 out（一人一消息）。
"""

from uuid import uuid4

import pytest
from sqlalchemy import select

from app import models
from app.db import SessionLocal

import tools_mcp_server as tools


def _png(color):
    import io

    from PIL import Image

    img = Image.new("RGB", (10, 10), color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


class _FakeStore:
    bucket = "hr-workbuddy"

    def __init__(self):
        self.keys = []

    def put_object(self, bucket, key, data, content_type):
        self.keys.append(key)


@pytest.fixture()
def jc_factory():
    """建 (job, candidate, jc) 三元组并提交，返回 jc（真实测试库，与既有测试同源）。"""
    created = []

    def _make(source: str = "inbound", status: str = "screened_pass") -> models.JobCandidate:
        job = models.Job(title="工具测试", jd_text="JD", hard_rules={}, template_msgs={})
        cand = models.Candidate(
            liepin_user_id=f"tool_{uuid4().hex[:8]}",
            name="工具候选人",
            online_resume_minimal={},
            source=source,
        )
        with SessionLocal() as session:
            session.add_all([job, cand])
            session.flush()
            jc = models.JobCandidate(
                job_id=job.id, candidate_id=cand.id, status=status
            )
            jc.candidate = cand
            session.add(jc)
            session.commit()
            created.append(jc.id)
            return jc

    return _make


class TestTouchGuard:
    def test_touch_transition_without_out_interaction_refused(self, jc_factory):
        jc = jc_factory(source="inbound", status="screened_pass")
        result = tools.state_transition(jc.id, "request_resume", source="inbound")
        assert "error" in result
        assert "out" in result["error"]
        with SessionLocal() as session:
            reloaded = session.get(models.JobCandidate, jc.id)
            assert reloaded.status == "screened_pass"  # 未被推进

    def test_greet_without_out_interaction_refused(self, jc_factory):
        jc = jc_factory(source="recommended", status="screened_pass")
        result = tools.state_transition(jc.id, "greet", source="recommended")
        assert "error" in result


class TestAnchors:
    def test_await_resume_sets_72h_anchor_and_last_touch(self, jc_factory):
        jc = jc_factory(source="inbound", status="screened_pass")
        assert "ok" in tools.interaction_log(jc.id, "out", "direct_request", "索要简历")
        r1 = tools.state_transition(jc.id, "request_resume", source="inbound")
        assert "error" not in r1
        r2 = tools.state_transition(jc.id, "await_resume")
        assert "error" not in r2
        assert r2["resume_requested_at"] is not None  # 72h 关闭锚点
        assert r2["last_touch_at"] is not None
        with SessionLocal() as session:
            reloaded = session.get(models.JobCandidate, jc.id)
            assert reloaded.status == "awaiting_resume"
            assert reloaded.resume_requested_at is not None
            assert reloaded.last_touch_at is not None

    def test_greet_sets_last_touch(self, jc_factory):
        jc = jc_factory(source="recommended", status="screened_pass")
        assert "ok" in tools.interaction_log(jc.id, "out", "greet_request", "你好")
        result = tools.state_transition(jc.id, "greet", source="recommended")
        assert "error" not in result
        assert result["last_touch_at"] is not None

    def test_receive_resume_sets_download_anchor(self, jc_factory):
        jc = jc_factory(status="awaiting_resume")
        result = tools.state_transition(jc.id, "receive_resume")
        assert "error" not in result
        assert result["resume_downloaded_at"] is not None


class TestOneMessage:
    def test_second_out_interaction_refused(self, jc_factory):
        jc = jc_factory()
        assert "ok" in tools.interaction_log(jc.id, "out", "direct_request", "第一次")
        result = tools.interaction_log(jc.id, "out", "direct_request", "第二次")
        assert "error" in result

    def test_in_interaction_not_blocked(self, jc_factory):
        jc = jc_factory()
        assert "ok" in tools.interaction_log(jc.id, "out", "direct_request", "索要")
        assert "ok" in tools.interaction_log(jc.id, "in", "attachment", "简历")


class TestCaptureAttachmentPages:
    def _patch(self, monkeypatch, frames):
        store = _FakeStore()
        monkeypatch.setattr(tools, "_store", lambda: store)
        monkeypatch.setattr(tools, "_swipe_up", lambda: None)
        it = iter(frames)
        monkeypatch.setattr(tools, "_screencap", lambda: next(it))
        return store

    def test_stops_at_bottom(self, monkeypatch):
        p0, p1 = _png("red"), _png("blue")
        store = self._patch(monkeypatch, [p0, p1, p1])  # 第 3 帧重复 = 到底
        result = tools.capture_attachment_pages("LP_X")
        assert result["page_count"] == 2
        assert len(store.keys) == 2

    def test_single_page_stops_immediately(self, monkeypatch):
        p0 = _png("red")
        store = self._patch(monkeypatch, [p0, p0])  # 滚动后未变化 = 单页
        result = tools.capture_attachment_pages("LP_Y")
        assert result["page_count"] == 1
        assert len(store.keys) == 1

    def test_max_pages_caps(self, monkeypatch):
        store = self._patch(monkeypatch, [_png(c) for c in ("red", "blue", "white", "black")])
        result = tools.capture_attachment_pages("LP_Z", max_pages=3)
        assert result["page_count"] == 3
        assert len(store.keys) == 3

    def test_images_differ_identical_vs_distinct(self):
        p0, p1 = _png("red"), _png("blue")
        assert tools._images_differ(p0, p0) is False
        assert tools._images_differ(p0, p1) is True

    def test_writes_snapshot_key_back_to_candidate(self, monkeypatch):
        lid = f"cap_{uuid4().hex[:8]}"
        with SessionLocal() as session:
            cand = models.Candidate(
                liepin_user_id=lid, name="截图回写", online_resume_minimal={}, source="inbound"
            )
            session.add(cand)
            session.commit()
        p0 = _png("red")
        self._patch(monkeypatch, [p0, p0])  # 单页
        result = tools.capture_attachment_pages(lid)
        assert result["snapshot_object_key"] is not None
        with SessionLocal() as session:
            cand = session.execute(
                select(models.Candidate).where(models.Candidate.liepin_user_id == lid)
            ).scalar_one()
            assert cand.snapshot_object_key == result["snapshot_object_key"]
