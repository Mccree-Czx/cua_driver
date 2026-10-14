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


def login_state_key(account_id: int) -> str:
    """登录态：最近一次检查「这个号还在不在线」的结果。按账号分片。

    原先定义在 login_state.py，与本模块的 docstring 说的（登录态归这里管）
    不一致。收敛过来，让 hr_api 与 Pi 的工具读同一份格式。
    """
    return f"pipeline:state:login:{account_id}"


def login_req_key() -> str:
    """登录请求状态机（单例，不分账号）。

    一部手机同一时刻只可能有一个登录在途——分片反而要扫描多个键才能发现
    「有请求」。账号 id 是这个键里的一个字段，不进键名。
    """
    return "pipeline:state:login_req"
