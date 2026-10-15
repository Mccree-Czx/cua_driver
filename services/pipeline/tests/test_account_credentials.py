"""账号凭据（手机号 + 加密密码）验收测试（2026-10-14）。

守两条安全属性：
1. 明文密码**永不落库、永不进任何响应体**——只有 Fernet 密文进数据库。
2. 手机号是账号的真实身份，唯一约束必须挡住重复登记。

测试自建账号，并发的唯一约束意味着**必须清理**：API 的写是各自 commit 的，
conftest 的 session fixture 回滚管不到，残留会让下一次跑测试直接 409。
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import models
from app.crypto import CredentialCryptoError, decrypt_secret, encrypt_secret
from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def made(session):
    """记录测试期间经 API 建出来的账号，结束后删干净。"""
    ids: list[int] = []
    yield ids
    for aid in ids:
        session.execute(models.Account.__table__.delete().where(models.Account.id == aid))
    session.commit()


def _phone() -> str:
    """每次跑用不同号码——phone 有唯一约束，写死会与上一轮的残留撞车。"""
    return "139" + f"{uuid4().int % 10**8:08d}"


def _create(client, made, *, name="测试号", phone=None, password="Passw0rd!#abc"):
    resp = client.post(
        "/api/hr/accounts",
        json={"name": name, "phone": phone or _phone(), "password": password},
    )
    assert resp.status_code == 200, resp.text
    made.append(resp.json()["id"])
    return resp.json()


# —— 加密原语 ——


@pytest.mark.parametrize(
    "plain",
    ["简单密码", "P@ssw0rd!#$%^&*()", "a" * 128, "中文English123", " 前后空格 "],
)
def test_crypto_roundtrip(plain):
    assert decrypt_secret(encrypt_secret(plain)) == plain


def test_crypto_same_plaintext_yields_different_ciphertext():
    """Fernet 每次带随机 IV——同密码两次加密必须不同，否则能看出「两个号密码一样」。"""
    assert encrypt_secret("same") != encrypt_secret("same")


def test_decrypt_with_wrong_key_raises():
    """密钥不对要抛可读错误，而不是把 cryptography 的异常类型漏给调用方。"""
    token = encrypt_secret("x")
    with pytest.raises(CredentialCryptoError):
        decrypt_secret(token[:-4] + "AAAA")


# —— 建账号 ——


def test_create_stores_ciphertext_and_never_returns_password(client, made, session):
    phone = _phone()
    body = _create(client, made, name="联调号", phone=phone, password="MySecret-123")

    # 响应体：有打码手机号与 has_password，但绝无密码本身或密文
    assert body["phone_masked"] == f"{phone[:3]}****{phone[-4:]}"
    assert body["has_password"] is True
    assert "password" not in body and "password_enc" not in body
    assert "MySecret-123" not in str(body)

    # 库里：只有密文，且能解回原文
    row = session.get(models.Account, body["id"])
    assert row.password_enc and "MySecret-123" not in row.password_enc
    assert decrypt_secret(row.password_enc) == "MySecret-123"
    assert row.credential_updated_at is not None


def test_create_rejects_duplicate_phone(client, made):
    phone = _phone()
    _create(client, made, phone=phone)
    resp = client.post(
        "/api/hr/accounts",
        json={"name": "重复", "phone": phone, "password": "x"},
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.parametrize("bad", ["138abc", "12345", "1" * 21, ""])
def test_create_rejects_bad_phone(client, bad):
    resp = client.post(
        "/api/hr/accounts", json={"name": "坏号", "phone": bad, "password": "x"}
    )
    assert resp.status_code == 400, resp.text


def test_create_rejects_empty_password(client):
    resp = client.post(
        "/api/hr/accounts", json={"name": "无密码", "phone": _phone(), "password": ""}
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize(
    "password,must_mention",
    [
        ('has"quote', "双引号"),
        ("中文密码", "双引号与中文"),
        ("pct%shere", "%s"),
    ],
)
def test_create_rejects_untypeable_password(client, password, must_mention):
    """实测结论固化成规则：双引号与中文敲不进手机，含字面量 %s 会变成空格。

    这类密码存进库也永远登不上——必须在创建时就拦住，而不是等换号时失败。
    """
    resp = client.post(
        "/api/hr/accounts", json={"name": "坏密码", "phone": _phone(), "password": password}
    )
    assert resp.status_code == 400, resp.text
    assert must_mention in resp.json()["detail"]


def test_create_accepts_the_full_typeable_symbol_set(client, made, session):
    """除双引号外的可打印 ASCII 全部要能通过——别把规则收得比实测更紧。"""
    import string

    password = string.ascii_letters + string.digits + string.punctuation.replace('"', "") + " "
    assert '"' not in password
    created = _create(client, made, password=password)
    row = session.get(models.Account, created["id"])
    assert decrypt_secret(row.password_enc) == password


def test_list_masks_phone_and_hides_password(client, made):
    phone = _phone()
    created = _create(client, made, phone=phone)
    resp = client.get("/api/hr/accounts")
    assert resp.status_code == 200
    item = next(i for i in resp.json()["items"] if i["id"] == created["id"])
    assert item["phone_masked"] == f"{phone[:3]}****{phone[-4:]}"
    assert item["has_password"] is True
    assert phone not in resp.text  # 完整号码不出现在列表响应里


# —— 补录凭据（PATCH）——


def test_patch_only_touches_provided_fields(client, made, session):
    created = _create(client, made, name="原名", password="Original-1")
    phone_before = session.get(models.Account, created["id"]).phone
    # 结束读事务：MySQL 默认 REPEATABLE READ，不结束的话后面读到的还是旧快照，
    # 会把「没改」误判成「改对了」。
    session.rollback()

    resp = client.patch(f"/api/hr/accounts/{created['id']}", json={"name": "新名"})
    assert resp.status_code == 200, resp.text

    row = session.get(models.Account, created["id"])
    assert row.name == "新名"
    assert row.phone == phone_before  # 没传 phone，不该被动
    assert decrypt_secret(row.password_enc) == "Original-1"  # 没传 password，同样不动


def test_patch_updates_password(client, made, session):
    created = _create(client, made, password="OldPass-1")
    resp = client.patch(
        f"/api/hr/accounts/{created['id']}", json={"password": "NewPass-2!#abc"}
    )
    assert resp.status_code == 200, resp.text
    row = session.get(models.Account, created["id"])
    assert decrypt_secret(row.password_enc) == "NewPass-2!#abc"


def test_patch_rejects_phone_taken_by_another_account(client, made):
    phone_a = _phone()
    _create(client, made, phone=phone_a)
    b = _create(client, made)

    resp = client.patch(f"/api/hr/accounts/{b['id']}", json={"phone": phone_a})
    assert resp.status_code == 409, resp.text


def test_patch_unknown_account_404(client):
    resp = client.patch("/api/hr/accounts/999999", json={"name": "x"})
    assert resp.status_code == 404, resp.text
