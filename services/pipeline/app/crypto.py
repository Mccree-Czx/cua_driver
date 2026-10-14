"""账号密码的对称加密（多账号支持，2026-10-14）。

密码入库与仓库既有约定相冲——runbook 写的是「密钥只放 .env，绝不入库」
（DeepSeek key 就是这么处理的）。这里的划法是：**主密钥仍然只在 .env**
（ACCOUNT_SECRET_KEY），入库的只是被它加密后的密文。既不把明文凭据写进
数据库与备份，也不必在多账号场景下反复改配置文件。

密钥丢失 = 已存的密码不可恢复。账号本身不受影响（重新填一次即可），但
换号回滚时就没有旧号密码可用了——所以密钥要进 CT104 与 Mac 两处的 .env
并纳入备份。

轮换用 MultiFernet：新密钥放最前，旧密文仍能解，逐个重加密后再摘旧密钥。
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

_KEY_HELP = (
    "生成方式：python -c \"from cryptography.fernet import Fernet; "
    'print(Fernet.generate_key().decode())"'
)


class CredentialCryptoError(RuntimeError):
    """密钥缺失，或密文无法用当前密钥解开。"""


@lru_cache
def _fernet() -> Fernet:
    key = (get_settings().account_secret_key or "").strip()
    if not key:
        raise CredentialCryptoError(f"未配置 ACCOUNT_SECRET_KEY。{_KEY_HELP}")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise CredentialCryptoError(
            f"ACCOUNT_SECRET_KEY 不是合法的 Fernet 密钥（须为 urlsafe base64 的 32 字节）。{_KEY_HELP}"
        ) from exc


def encrypt_secret(plain: str) -> str:
    """明文 → Fernet 密文（每次调用结果不同：Fernet 内含时间戳与随机 IV）。"""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """密文 → 明文。密钥不对或密文损坏都抛 CredentialCryptoError。

    刻意不把 InvalidToken 直接漏出去：它是 cryptography 的实现细节，调用方
    需要知道的是「密钥与加密时不一致」，而不是某个库的异常类型。
    """
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialCryptoError(
            "密码解密失败：ACCOUNT_SECRET_KEY 与加密时不一致，或密文已损坏"
        ) from exc
