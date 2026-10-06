"""FakeLiepinDriver：协议一致性（7 方法签名与返回类型）+ 剧本行为。

覆盖：登录态剧本、重复会话幂等、附件按 tick 送达、"never" 时间线、
时钟注入、未知会话报错、确定性字节生成；World 模型测试（tick 拨快、
时间线读取、JSON 加载）并入本文件。全 mock，无网络/桌面依赖。
"""

import json

import pytest

from app.drivers.fake import (
    ConversationNotFoundError,
    FakeLiepinDriver,
    NoAttachmentError,
    NoResumeFixtureError,
    pdf_bytes,
    png_bytes,
)
from app.world import AttachmentSpec, ConversationScript, ReplyEvent, World, load_world
from hr_workbuddy import LiepinDriver, MinimalResume

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PDF_HEADER = b"%PDF-1.4"


def make_resume(uid: str) -> MinimalResume:
    """恰 7 字段的最小简历 fixture。"""
    return MinimalResume(
        name=f"候选人{uid}",
        liepin_user_id=uid,
        education="本科",
        years_of_experience="3年",
        city="杭州",
        salary="20-30K",
        experience_summary="3 年后端开发经验",
    )


def make_world(**overrides) -> World:
    """两会话剧本：uid_a 未读+简历、uid_b 已读无简历；默认登录态。"""
    defaults = dict(
        login_state=True,
        conversations=[
            ConversationScript(liepin_user_id="uid_a", unread=True, resume_fixture="fixture_a"),
            ConversationScript(liepin_user_id="uid_b", unread=False, resume_fixture=None),
        ],
        resume_fixtures={"fixture_a": make_resume("uid_a")},
        reply_timeline="never",
    )
    defaults.update(overrides)
    return World(**defaults)


def make_driver(world: World, clock=None) -> FakeLiepinDriver:
    return FakeLiepinDriver(world, now=clock)


# —— 协议一致性 ——


def test_protocol_conformance():
    driver = make_driver(make_world())
    assert isinstance(driver, LiepinDriver)  # runtime_checkable：7 方法齐备


def test_method_signatures_and_return_types():
    """7 方法逐一调用：返回类型与 contracts 协议逐字对齐。"""
    world = make_world(
        reply_timeline=[
            ReplyEvent(tick=0, conversation_id="uid_a", attachment=AttachmentSpec(file_name="简历.pdf"))
        ]
    )
    driver = make_driver(world)
    assert type(driver.check_login()) is bool
    unread = driver.list_unread_conversations()
    assert isinstance(unread, list) and all(isinstance(u, str) for u in unread)
    assert driver.open_conversation("uid_a") is None
    png, resume = driver.read_online_resume("uid_a")
    assert isinstance(png, bytes) and isinstance(resume, MinimalResume)
    assert driver.send_message("uid_a", "你好") is None
    assert type(driver.check_attachment("uid_a")) is bool
    pdf, name = driver.download_attachment("uid_a")  # R7：tuple[bytes, str]
    assert isinstance(pdf, bytes) and isinstance(name, str)


# —— 登录态剧本 ——


def test_login_state_script_logged_out():
    driver = make_driver(make_world(login_state=False))
    assert driver.check_login() is False


def test_login_state_script_logged_in():
    driver = make_driver(make_world(login_state=True))
    assert driver.check_login() is True


# —— 重复会话脚本（幂等） ——


def test_repeated_read_same_conversation_idempotent():
    driver = make_driver(make_world())
    png1, resume1 = driver.read_online_resume("uid_a")
    png2, resume2 = driver.read_online_resume("uid_a")
    assert png1 == png2
    assert resume1 == resume2
    assert resume1.liepin_user_id == "uid_a"


def test_read_returns_valid_png_signature():
    png, _ = make_driver(make_world()).read_online_resume("uid_a")
    assert png.startswith(PNG_SIGNATURE)


def test_read_conversation_without_fixture_raises():
    driver = make_driver(make_world())
    with pytest.raises(NoResumeFixtureError):
        driver.read_online_resume("uid_b")


# —— 附件按 tick 送达 ——


def test_attachment_arrives_exactly_at_tick():
    world = make_world(
        reply_timeline=[
            ReplyEvent(
                tick=3,
                conversation_id="uid_b",
                attachment=AttachmentSpec(file_name="简历.pdf", content_type="application/pdf"),
            )
        ]
    )
    driver = make_driver(world)
    assert driver.check_attachment("uid_b") is False  # tick 0
    with pytest.raises(NoAttachmentError):
        driver.download_attachment("uid_b")
    world.advance(2)  # tick 2：未到
    assert driver.check_attachment("uid_b") is False
    world.advance(1)  # tick 3：送达
    assert driver.check_attachment("uid_b") is True
    pdf, name = driver.download_attachment("uid_b")
    assert pdf.startswith(PDF_HEADER) and pdf.endswith(b"%%EOF\n")
    assert name == "简历.pdf"  # R7：文件名来自剧本附件定义


def test_attachment_latest_event_wins():
    world = make_world(
        reply_timeline=[
            ReplyEvent(tick=1, conversation_id="uid_a", attachment=AttachmentSpec(file_name="旧.pdf")),
            ReplyEvent(tick=2, conversation_id="uid_a", attachment=AttachmentSpec(file_name="新.pdf")),
        ]
    )
    driver = make_driver(world)
    world.advance(2)
    latest, name = driver.download_attachment("uid_a")
    assert latest == pdf_bytes(seed="uid_a:新.pdf")  # 确定性：按最新送达的附件
    assert name == "新.pdf"  # R7：文件名随字节一并返回


# —— "never" 时间线 ——


def test_never_timeline_no_reply_no_attachment():
    world = make_world(reply_timeline="never")
    driver = make_driver(world)
    world.advance(100)
    assert driver.check_attachment("uid_a") is False
    assert driver.check_attachment("uid_b") is False
    with pytest.raises(NoAttachmentError):
        driver.download_attachment("uid_a")


# —— 时钟注入 ——


def test_clock_injection_advances_without_mutating_world():
    """注入时钟可拨快：world.tick 不动，driver 按注入时钟观察送达。"""
    holder = {"t": 0}
    world = make_world(
        reply_timeline=[
            ReplyEvent(tick=5, conversation_id="uid_a", attachment=AttachmentSpec(file_name="简历.pdf"))
        ]
    )
    driver = make_driver(world, clock=lambda: holder["t"])
    assert driver.check_attachment("uid_a") is False
    holder["t"] = 5  # 只拨注入时钟，world.tick 保持 0
    assert world.tick == 0
    assert driver.check_attachment("uid_a") is True


# —— 未读列表 / 已发消息 / 未知会话 ——


def test_list_unread_only_unread_flagged():
    driver = make_driver(make_world())
    assert driver.list_unread_conversations() == ["uid_a"]


def test_send_message_records_without_mutating_world():
    world = make_world()
    driver = make_driver(world)
    driver.send_message("uid_a", "您好，方便发一份简历吗？")
    assert driver.sent_messages == [("uid_a", "您好，方便发一份简历吗？")]
    assert world.conversations[0].unread is True  # 剧本未被改写


def test_open_conversation_records():
    driver = make_driver(make_world())
    driver.open_conversation("uid_a")
    assert driver.opened == {"uid_a"}


def test_unknown_conversation_raises_on_all_methods():
    driver = make_driver(make_world())
    with pytest.raises(ConversationNotFoundError):
        driver.open_conversation("ghost")
    with pytest.raises(ConversationNotFoundError):
        driver.read_online_resume("ghost")
    with pytest.raises(ConversationNotFoundError):
        driver.send_message("ghost", "你好")
    with pytest.raises(ConversationNotFoundError):
        driver.check_attachment("ghost")
    with pytest.raises(ConversationNotFoundError):
        driver.download_attachment("ghost")


# —— 确定性字节 ——


def test_bytes_deterministic_and_distinct_by_seed():
    assert png_bytes("uid_a:resume") == png_bytes("uid_a:resume")
    assert png_bytes("uid_a:resume") != png_bytes("uid_b:resume")
    assert pdf_bytes("uid_a:简历.pdf") == pdf_bytes("uid_a:简历.pdf")
    assert pdf_bytes("uid_a:简历.pdf") != pdf_bytes("uid_b:简历.pdf")


# —— World 模型：tick 拨快 / 时间线读取 / JSON 加载 ——


def test_world_advance_and_visible_events_sorted():
    world = make_world(
        reply_timeline=[
            ReplyEvent(tick=5, conversation_id="uid_a", text="好的"),
            ReplyEvent(tick=1, conversation_id="uid_a", attachment=AttachmentSpec(file_name="a.pdf")),
            ReplyEvent(tick=5, conversation_id="uid_b", text="稍等"),
        ]
    )
    assert world.visible_events() == []  # tick 0：tick 1 事件未送达
    world.advance(1)  # tick 1
    assert [e.tick for e in world.visible_events()] == [1]
    world.advance(4)  # tick 5
    assert world.tick == 5
    assert [e.tick for e in world.visible_events()] == [1, 5, 5]  # 按 tick 升序
    world.advance(3)
    assert world.tick == 8


def test_world_visible_events_with_explicit_now():
    world = make_world(
        reply_timeline=[ReplyEvent(tick=3, conversation_id="uid_a", text="好的")]
    )
    assert world.visible_events(now=2) == []
    assert len(world.visible_events(now=3)) == 1


def test_load_world_from_json_file(tmp_path):
    data = {
        "login_state": False,
        "conversations": [
            {
                "liepin_user_id": "uid_a",
                "unread": True,
                "resume_fixture": "fixture_a",
            }
        ],
        "resume_fixtures": {
            "fixture_a": {
                "name": "张三",
                "liepin_user_id": "uid_a",
                "education": "本科",
                "years_of_experience": "3年",
                "city": "杭州",
                "salary": "20-30K",
                "experience_summary": "3 年后端",
            }
        },
        "reply_timeline": "never",
    }
    path = tmp_path / "world.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    world = load_world(path)
    assert world.login_state is False
    assert world.reply_timeline == "never"
    assert world.conversations[0].resume_fixture == "fixture_a"
    assert world.resume_fixtures["fixture_a"].name == "张三"


def test_load_world_json_with_event_list(tmp_path):
    data = {
        "login_state": True,
        "conversations": [],
        "resume_fixtures": {},
        "reply_timeline": [
            {
                "tick": 2,
                "conversation_id": "uid_a",
                "attachment": {"file_name": "简历.pdf", "content_type": "application/pdf"},
            }
        ],
    }
    path = tmp_path / "world.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    world = load_world(path)
    world.advance(2)
    assert [e.tick for e in world.visible_events()] == [2]
    assert world.visible_events()[0].attachment.file_name == "简历.pdf"


def test_world_rejects_unknown_fields():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        World.model_validate({"login_state": True, "typo_field": 1})
