"""Redis 键名。

## ★ 两层隔离，缺一不可

猎聘与 BOSS 会跑在**同一个 Redis 实例**上（本机是同一个 docker 容器，
生产是同一台 197）。所以：

1. **db 分开**：BOSS 用 db 1，猎聘用 db 0（见 `config.py` 的 `redis_url`）
2. **前缀分开**：BOSS 的键**全部**以 `boss:` 开头

第 2 层看着冗余，但它是**防手滑**的那一层：哪天有人把 `.env` 里的 db 号抄错了，
前缀仍然能让两边的键不撞。猎聘那边因为只有一个使用者，键名是光秃秃的
（`msg-touch:...`），这里刻意不跟。

## 与猎聘的键对照

| 猎聘 | BOSS | 说明 |
|---|---|---|
| `msg-touch:{account_id}:{date}` | `boss:msg-touch:{date}` | 每日触达计数。**没有 account_id**——单账号 |
| `cua:risk:paused:{account_id}` | `boss:cua:risk:paused` | 风控熔断。同上 |
| `pipeline:state:login:{account_id}` | —— | 不复刻：BOSS 登录是人过的关卡，没有自动登录态要存 |
| `pipeline:state:login_req` | `boss:login_gate_in_flight` | ★ **语义不同**：猎聘那个是「登录请求在途」，BOSS 这个是「手机正停在风控关卡前等人」 |
"""

from __future__ import annotations

from datetime import date

#: 所有键的统一前缀。**别去掉。**
PREFIX = "boss:"


def msg_touch(day: date | str | None = None) -> str:
    """当日打招呼计数。★ 没有 account_id——BOSS 单账号。"""
    if day is None:
        day = date.today()
    return f"{PREFIX}msg-touch:{day.isoformat()}"


def risk_paused() -> str:
    """风控熔断标记。

    ★ 语义：**一旦置上，所有写动作停手**，直到有人查清原因并手动清掉。
      照猎聘的铁律 3——风控页即停，绝不尝试绕过。
    """
    return f"{PREFIX}cua:risk:paused"


def login_gate_in_flight() -> str:
    """★ 「手机正停在风控关卡前等人」。

    **与猎聘同名键的语义完全不同**，别照搬逻辑：

    - 猎聘的 `pipeline:state:login_req` = 「有一个换号请求在途」，是**机器要去做的事**
    - BOSS 这个 = 「手机停在滑块/短信/人脸页上，**等人在手机上过**」，是**机器做不了的事**

    为什么必须有它：BOSS 的手机随时可能停在那三关中的任意一关，
    而**关卡页看起来就像「掉登录了」**。没有这个标记，轮次会把关卡页误判成
    掉登录 → 触发假熔断 → 把好好的账号锁死。
    """
    return f"{PREFIX}login_gate_in_flight"
