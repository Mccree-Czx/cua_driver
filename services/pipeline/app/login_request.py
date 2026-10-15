"""登录请求状态机（多账号登录，2026-10-14）。

**为什么要跨机器交接**：前端与 pipeline 在 CT104，Pi 与手机在 Mac。CT104 连不到
Mac（Mac 在 NAT 后），两边只能靠 Redis 见面——前端经 pipeline 写，Mac 上的轮询器
读，Pi 再经 MCP 工具读写。

**为什么拆成两次 Pi 调用**：登录验证码发到用户自己的手机上，Pi 物理上等不到。
让一轮 LLM 挂在「等验证码」上空转会烧 token，也极易跑偏。所以拆成「填手机号 → 点
获取验证码」与「读人工填的码 → 提交」两段，中间由人在前端衔接。

**为什么是单例键**：一部手机同一时刻只可能有一个登录在途。按账号分片反而要扫描
多个键才能发现「有请求」。

存的都是**状态**：手机号与密码这类 PII 留在 MySQL，Pi 用 login_req_get() 自己去取。
"""

import time
import uuid
from typing import Any, Protocol

import redis

from app.config import get_settings
from app.keys import login_req_key

LOGIN_TTL_SECONDS = 1800  # 整个登录请求的有效期
CODE_TTL_SECONDS = 300  # 验证码有效期（短信验证码一般 5 分钟）
MAX_ATTEMPTS = 3  # 拉起次数上限。轮询器 20 秒一轮，不设上限会在卡死时无限烧 token

PHASE_PENDING = "pending"  # 前端发起，等 Mac 拉起
PHASE_DISPATCHED = "dispatched"  # Pi 已开始操作手机
PHASE_AWAITING_CODE = "awaiting_code"  # 验证码已发送，等人工填
PHASE_VERIFYING = "verifying"  # Pi 拿到码，正在提交
PHASE_SUCCESS = "success"
PHASE_FAILED = "failed"
PHASE_CANCELLED = "cancelled"
PHASE_EXPIRED = "expired"

TERMINAL_PHASES = frozenset(
    {PHASE_SUCCESS, PHASE_FAILED, PHASE_CANCELLED, PHASE_EXPIRED}
)

# 登录方式（用户在前端选）：
#   password —— 全自动，不需要人；前提是密码已存且正确
#   code     —— 无需密码，但要人工从自己手机上读一次验证码
# 密码失败会自动降级到 code，并在 notice 里说明原因（不静默换路）。
METHOD_PASSWORD = "password"
METHOD_CODE = "code"
LOGIN_METHODS = frozenset({METHOD_PASSWORD, METHOD_CODE})

# 该拉起 Pi 的相位。pending = 发起，awaiting_code = 人工已填码。
LAUNCHABLE_PHASES = frozenset({PHASE_PENDING, PHASE_AWAITING_CODE})

# 原子取码：HGET 与标记已消费必须在一条命令里完成，否则两次启动可能拿到同一个码。
# Redis 的 Lua 脚本是原子的。
_CLAIM_CODE_LUA = """
local code = redis.call('HGET', KEYS[1], 'code')
if not code or code == '' then return false end
if tonumber(redis.call('HGET', KEYS[1], 'code_consumed_at') or '0') > 0 then return false end
redis.call('HSET', KEYS[1], 'code_consumed_at', ARGV[1])
return code
"""

_INT_FIELDS = ("account_id", "resend_seq", "attempts")
_FLOAT_FIELDS = ("code_expires_at", "code_consumed_at", "expires_at", "updated_at")


class LoginRequestStore(Protocol):
    """登录请求存储形状：生产 RedisLoginRequest，测试可换内存替身。"""

    def get(self) -> dict[str, Any] | None: ...

    def start(self, account_id: int) -> dict[str, Any]: ...

    def claim_code(self) -> str | None: ...


def _parse(raw: dict[str, str]) -> dict[str, Any]:
    """Redis 取回来的一律是字符串——按 schema 转回 Python 类型。

    不转的话 `expires_at < time.time()` 会变成字符串与浮点数比较，直接 TypeError；
    更糟的是 `attempts >= MAX_ATTEMPTS` 会静默地永远为假。
    """
    out: dict[str, Any] = dict(raw)
    for field in _INT_FIELDS:
        out[field] = int(raw.get(field) or 0)
    for field in _FLOAT_FIELDS:
        out[field] = float(raw.get(field) or 0)
    out["code"] = raw.get("code") or ""
    out["error"] = raw.get("error") or ""
    out["notice"] = raw.get("notice") or ""
    out["method"] = raw.get("method") or METHOD_CODE
    return out


class RedisLoginRequest:
    """Redis 实现：单例 hash `pipeline:state:login_req`，带 TTL。"""

    def __init__(self, redis_url: str | None = None) -> None:
        self._redis = redis.Redis.from_url(
            redis_url or get_settings().redis_url, decode_responses=True
        )

    # —— 读 ——

    def get(self) -> dict[str, Any] | None:
        raw = self._redis.hgetall(login_req_key())
        return _parse(raw) if raw else None

    def active(self) -> dict[str, Any] | None:
        """在途的请求：非终态、且整体未过期。轮询器与轮次互斥都看这个。"""
        req = self.get()
        if req is None or req["phase"] in TERMINAL_PHASES:
            return None
        if req["expires_at"] and req["expires_at"] < time.time():
            return None
        return req

    def claim_code(self) -> str | None:
        """原子取走验证码；已被取过或根本没填过都返回 None。"""
        result = self._redis.eval(_CLAIM_CODE_LUA, 1, login_req_key(), str(time.time()))
        return str(result) if result else None

    def code_is_expired(self, req: dict[str, Any]) -> bool:
        return bool(req["code_expires_at"]) and req["code_expires_at"] < time.time()

    # —— 写 ——

    def start(self, account_id: int, method: str = METHOD_CODE) -> dict[str, Any]:
        """发起一次登录。会**覆盖**在途的旧请求——同一时刻只能有一个。"""
        if method not in LOGIN_METHODS:
            raise ValueError(f"未知登录方式：{method}")
        now = time.time()
        key = login_req_key()
        payload = {
            "request_id": uuid.uuid4().hex,
            "account_id": account_id,
            "method": method,
            "phase": PHASE_PENDING,
            "code": "",
            "code_expires_at": 0,
            "code_consumed_at": 0,
            "resend_seq": 0,
            "attempts": 0,
            "expires_at": now + LOGIN_TTL_SECONDS,
            "error": "",
            "notice": "",
            "updated_at": now,
        }
        pipe = self._redis.pipeline()
        pipe.delete(key)  # 清掉上一轮的残留字段（避免读到旧的 code/error）
        pipe.hset(key, mapping={k: str(v) for k, v in payload.items()})
        pipe.expire(key, LOGIN_TTL_SECONDS)
        pipe.execute()
        return payload

    def set_phase(self, phase: str, note: str = "") -> None:
        """推进相位。`note` 按相位分流，两者互斥。

        `error` 是「这次登录失败了」的结论，前端红着脸显示；`notice` 是
        「密码没成功，已改用验证码」这类过程说明，是黄条不是红条。混在一个字段里，
        前端就只能靠相位猜语气，迟早猜错。
        """
        field, other = (
            ("error", "notice") if phase == PHASE_FAILED else ("notice", "error")
        )
        self._redis.hset(
            login_req_key(),
            mapping={
                "phase": phase,
                field: note,
                other: "",
                "updated_at": str(time.time()),
            },
        )

    def submit_code(self, code: str) -> None:
        """人工填码。同时把相位推到 awaiting_code，等 Pi 下一轮来取。"""
        now = time.time()
        self._redis.hset(
            login_req_key(),
            mapping={
                "code": code,
                "code_expires_at": str(now + CODE_TTL_SECONDS),
                "code_consumed_at": "0",  # 新码来了，解除上一枚的消费标记
                "phase": PHASE_AWAITING_CODE,
                "error": "",
                "updated_at": str(now),
            },
        )

    def bump_resend(self) -> None:
        """用户点了重发。下一轮 Pi 启动时才会真的去点（禁止 Pi 自己循环点）。"""
        self._redis.hincrby(login_req_key(), "resend_seq", 1)

    def bump_attempts(self) -> int:
        return int(self._redis.hincrby(login_req_key(), "attempts", 1))

    def clear(self) -> None:
        self._redis.delete(login_req_key())
