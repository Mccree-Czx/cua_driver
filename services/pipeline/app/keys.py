"""按账号分片的 Redis 键（多账号支持，2026-10-14）。

这些键的语义本来就是账号级的——猎聘的每日打招呼额度、风控熔断、登录态
都是按招聘号算的，不是全局的。放在一个模块里是为了让 hr_api（读接口）与
tools_mcp_server（Pi 的工具）用同一份格式，避免两边各写一遍字符串而漂移。
"""


def msg_touch_key(account_id: int, date_str: str) -> str:
    """每日打招呼计数。date_str 形如 20261014。"""
    return f"msg-touch:{account_id}:{date_str}"


def risk_pause_key(account_id: int) -> str:
    """风控熔断标志。"""
    return f"cua:risk:paused:{account_id}"
