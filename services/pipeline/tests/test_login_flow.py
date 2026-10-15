"""登录流程验收测试（批次 2，2026-10-14）。

覆盖三块：
1. 状态机 `RedisLoginRequest`——尤其是验证码的**一次性**：同一个码不能被提交两次。
2. 登录接口的守卫——没手机号不准登录、同一时刻只准一个登录在途。
3. `login_req_set('success')` 的副作用——它终于给 `RedisLoginState.set()` 找到了
   调用点（在此之前全仓库无人调用），并要顺带把该账号置为生效。

Redis 用 **15 号库**，不碰 db0 里真实的配额计数与风控标志。
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import models
from app.config import get_settings
from app.deps import get_login_request
from app.keys import login_state_key
from app.login_request import RedisLoginRequest
from app.main import app

import tools_mcp_server as tools

TEST_REDIS_URL = "redis://127.0.0.1:6379/15"


@pytest.fixture()
def store():
    s = RedisLoginRequest(TEST_REDIS_URL)
    s.clear()
    yield s
    s.clear()


@pytest.fixture()
def client(store):
    app.dependency_overrides[get_login_request] = lambda: store
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def hr_tools_on_test_redis(monkeypatch):
    """把 hr-tools 的 Redis 指到 15 号库——它内部按 settings 建连，并 lru_cache 住了。"""
    monkeypatch.setenv("REDIS_URL", TEST_REDIS_URL)
    get_settings.cache_clear()
    tools._redis.cache_clear()
    yield
    get_settings.cache_clear()
    tools._redis.cache_clear()


@pytest.fixture()
def made(session):
    """经 API 建出来的账号，测试后删干净（phone 有唯一约束，残留会污染下一轮）。"""
    ids: list[int] = []
    yield ids
    for aid in ids:
        session.execute(models.Account.__table__.delete().where(models.Account.id == aid))
    session.commit()


def _new_account(client, made, *, phone=None):
    resp = client.post(
        "/api/hr/accounts",
        json={
            "name": f"登录测试-{uuid4().hex[:6]}",
            "phone": phone or ("139" + f"{uuid4().int % 10**8:08d}"),
            "password": "pw",
        },
    )
    assert resp.status_code == 200, resp.text
    made.append(resp.json()["id"])
    return resp.json()


# —— 状态机 ——


def test_start_puts_request_in_pending(store):
    store.start(account_id=7)
    req = store.get()
    assert req["phase"] == "pending"
    assert req["account_id"] == 7
    assert req["attempts"] == 0
    assert store.active() is not None


def test_submit_code_moves_to_awaiting(store):
    store.start(account_id=7)
    store.submit_code("123456")
    req = store.get()
    assert req["phase"] == "awaiting_code"
    assert req["code"] == "123456"
    assert req["code_expires_at"] > 0


def test_code_can_only_be_claimed_once(store):
    """同一个码被提交两次 = 往猎聘重复提交同一个验证码，必须挡住。"""
    store.start(account_id=7)
    store.submit_code("123456")
    assert store.claim_code() == "123456"
    assert store.claim_code() is None


def test_claim_returns_none_when_no_code(store):
    store.start(account_id=7)
    assert store.claim_code() is None


def test_resubmitting_code_makes_it_claimable_again(store):
    """用户第一次填错了再填一次——新码必须能取，不能被上一次的消费标记锁死。"""
    store.start(account_id=7)
    store.submit_code("111111")
    store.claim_code()
    store.submit_code("222222")
    assert store.claim_code() == "222222"


def test_terminal_phase_is_not_active(store):
    store.start(account_id=7)
    store.set_phase("failed", "验证码错误")
    assert store.active() is None
    assert store.get()["error"] == "验证码错误"


def test_overall_timeout_is_not_active(store):
    store.start(account_id=7)
    # 把整体过期时间拨到过去
    store._redis.hset("pipeline:state:login_req", "expires_at", "1")
    assert store.active() is None
    assert store.get()["phase"] == "pending"  # 相位本身没被改，只是不再算「在途」


def test_clear_removes_request(store):
    store.start(account_id=7)
    store.clear()
    assert store.get() is None
    assert store.active() is None


def test_start_overwrites_stale_request(store):
    """重新发起要覆盖旧请求——否则会读到上一轮残留的 code/error。"""
    store.start(account_id=7)
    store.submit_code("123456")
    store.start(account_id=8)
    req = store.get()
    assert req["account_id"] == 8
    assert req["code"] == ""
    assert req["phase"] == "pending"


# —— 登录方式（2026-10-15 用户拍板：向导上二选一，密码失败自动降级）——


def test_start_records_method(store):
    assert store.start(account_id=7, method="password")["method"] == "password"
    assert store.get()["method"] == "password"


def test_start_rejects_unknown_method(store):
    with pytest.raises(ValueError):
        store.start(account_id=7, method="carrier_pigeon")


def test_note_goes_to_notice_unless_failed(store):
    """note 按相位分流：failed 是红条（结论），其余是黄条（过程说明）。

    「密码登录失败已改用验证码」属于后者。混进 error 会让前端把它渲染成一次失败，
    用户会以为登录挂了——其实只是换了个方式继续。
    """
    store.start(account_id=7, method="password")
    store.set_phase("awaiting_code", "密码登录失败，已改用验证码")
    req = store.get()
    assert req["notice"] == "密码登录失败，已改用验证码"
    assert req["error"] == ""

    store.set_phase("failed", "验证码错误")
    req = store.get()
    assert req["error"] == "验证码错误"
    assert req["notice"] == ""  # 落终态失败时，过程提示要让位给结论


def test_api_rejects_password_login_without_stored_password(client, made, session):
    acct = _new_account(client, made)
    session.execute(
        models.Account.__table__.update()
        .where(models.Account.id == acct["id"])
        .values(password_enc=None)
    )
    session.commit()
    resp = client.post(
        f"/api/hr/accounts/{acct['id']}/login", json={"method": "password"}
    )
    assert resp.status_code == 400, resp.text
    assert "没有存密码" in resp.json()["detail"]


def test_api_returns_method(client, made):
    acct = _new_account(client, made)
    client.post(f"/api/hr/accounts/{acct['id']}/login", json={"method": "password"})
    body = client.get("/api/hr/accounts/login-request").json()
    assert body["method"] == "password"
    assert body["notice"] is None


def test_method_defaults_to_code(client, made):
    """不传 method 时走验证码——不需要密码的那条路，作为默认更安全。"""
    acct = _new_account(client, made)
    client.post(f"/api/hr/accounts/{acct['id']}/login")
    assert client.get("/api/hr/accounts/login-request").json()["method"] == "code"


@pytest.mark.parametrize("bad", ["carrier_pigeon", "PASSWORD", ""])
def test_api_rejects_unknown_method(client, made, bad):
    acct = _new_account(client, made)
    resp = client.post(f"/api/hr/accounts/{acct['id']}/login", json={"method": bad})
    assert resp.status_code == 422, resp.text


# —— 接口守卫 ——


def test_login_requires_registered_phone(client, made, session):
    acct = _new_account(client, made)
    session.execute(
        models.Account.__table__.update()
        .where(models.Account.id == acct["id"])
        .values(phone=None)
    )
    session.commit()
    resp = client.post(f"/api/hr/accounts/{acct['id']}/login")
    assert resp.status_code == 400, resp.text
    assert "手机号" in resp.json()["detail"]


def test_login_unknown_account_404(client):
    assert client.post("/api/hr/accounts/999999/login").status_code == 404


def test_second_login_is_rejected_while_one_is_in_flight(client, made):
    a = _new_account(client, made)
    b = _new_account(client, made)
    assert client.post(f"/api/hr/accounts/{a['id']}/login").status_code == 200

    resp = client.post(f"/api/hr/accounts/{b['id']}/login")
    assert resp.status_code == 409, resp.text
    assert "只能登一个号" in resp.json()["detail"]


def test_force_overrides_in_flight(client, made):
    a = _new_account(client, made)
    b = _new_account(client, made)
    client.post(f"/api/hr/accounts/{a['id']}/login")
    resp = client.post(f"/api/hr/accounts/{b['id']}/login", json={"force": True})
    assert resp.status_code == 200, resp.text
    assert client.get("/api/hr/accounts/login-request").json()["account_id"] == b["id"]


@pytest.mark.parametrize("bad", ["", "abc", "12", "123456789"])
def test_code_must_be_4_to_8_digits(client, made, bad):
    acct = _new_account(client, made)
    client.post(f"/api/hr/accounts/{acct['id']}/login")
    resp = client.post("/api/hr/accounts/login-request/code", json={"code": bad})
    assert resp.status_code == 400, resp.text


def test_code_requires_active_login(client):
    assert client.post("/api/hr/accounts/login-request/code", json={"code": "123456"}).status_code == 409


def test_progress_never_echoes_the_code(client, made):
    """前端刚提交过验证码，回显没有意义，而它会进日志与浏览器历史。"""
    acct = _new_account(client, made)
    client.post(f"/api/hr/accounts/{acct['id']}/login")
    client.post("/api/hr/accounts/login-request/code", json={"code": "135790"})
    body = client.get("/api/hr/accounts/login-request").text
    assert "135790" not in body
    assert client.get("/api/hr/accounts/login-request").json()["phase"] == "awaiting_code"


def test_cancel_clears_and_allows_new_login(client, made):
    a = _new_account(client, made)
    b = _new_account(client, made)
    client.post(f"/api/hr/accounts/{a['id']}/login")
    assert client.post("/api/hr/accounts/login-request/cancel").status_code == 200
    assert client.get("/api/hr/accounts/login-request").json() == {"phase": "idle"}
    assert client.post(f"/api/hr/accounts/{b['id']}/login").status_code == 200


# —— MCP 工具（Pi 那一侧）——


def test_login_req_get_returns_phone_from_db(client, made, hr_tools_on_test_redis):
    acct = _new_account(client, made)
    client.post(f"/api/hr/accounts/{acct['id']}/login")
    info = tools.login_req_get()
    assert info["phase"] == "pending"
    assert info["account_id"] == acct["id"]
    assert info["phone"]  # 从 MySQL 取，不放 Redis


def test_login_req_set_rejects_unknown_phase(client, made, hr_tools_on_test_redis):
    acct = _new_account(client, made)
    client.post(f"/api/hr/accounts/{acct['id']}/login")
    assert tools.login_req_set("乱写的相位")["ok"] is False


def test_fill_tools_never_accept_a_password_parameter():
    """把设计约束变成可执行约束。

    密码只能由工具内部解密后直接投递给 adb。一旦有人给这些工具加了密码入参，
    Pi 就会把明文放进上下文——而它的完整 stdout 会落进 `.round_login_round.log`，
    等于把密码写进磁盘。这条断言就是为了在那一天拦住它。
    """
    import inspect

    forbidden = {"password", "secret", "pwd", "passwd", "credential", "token"}
    for fn in (tools.login_fill_phone, tools.login_fill_password):
        leaked = set(inspect.signature(fn).parameters) & forbidden
        assert not leaked, f"{fn.__name__} 出现了可传入的凭据参数：{leaked}"


def test_success_writes_login_state_and_activates(client, made, session, hr_tools_on_test_redis):
    acct = _new_account(client, made)
    client.post(f"/api/hr/accounts/{acct['id']}/login")
    assert tools.login_req_set("success")["ok"] is True

    # 登录态终于被写进去了（此前 RedisLoginState.set 全仓库无人调用）
    import json

    raw = tools._redis().get(login_state_key(acct["id"]))
    assert raw is not None
    assert json.loads(raw)["is_login"] is True

    session.expire_all()
    assert session.get(models.Account, acct["id"]).is_active is True
    assert session.get(models.Account, 1).is_active is False
    # 收拾现场：把生效账号还给默认账号
    client.post("/api/hr/accounts/1/activate")
