"""登录 / 切换账号的固定序列。

每个 flow 都是确定的点击序列，**每一步都 dump 校验**；任何一步与锚点不符就抛
`FlowMismatch`，并附上当时屏幕上看到了什么。刻意不做「猜着点」——锚点失配说明
`login_map.json` 过期了，正确动作是重新校准锚点表，不是现场试。

锚点全部来自 `login_map.json`（实测、外部化，App 升级只改它），本文件不写死坐标。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import phone

PKG = "com.lietou.mishu"
MAP_PATH = Path(__file__).resolve().parent / "login_map.json"


class FlowMismatch(RuntimeError):
    """某一步与锚点不符。带上屏上所见，便于判断是「锚点过期」还是「状态不对」。"""

    def __init__(self, step: str, detail: str, seen: list[str] | None = None):
        self.step = step
        self.detail = detail
        self.seen = seen or []
        super().__init__(f"[{step}] {detail}｜屏上：{self.seen[:12]}")


def load_map() -> dict[str, Any]:
    return json.loads(MAP_PATH.read_text(encoding="utf-8"))


def _expect(xml: str, markers: tuple[str, ...], step: str, what: str) -> None:
    joined = " ".join(phone.texts(xml))
    if not any(marker in joined for marker in markers):
        raise FlowMismatch(step, f"预期{what}，但没看到 {markers}", phone.texts(xml))


def _guard_risk(xml: str, step: str) -> None:
    """铁律 3：风控/验证页即停，绝不尝试绕过。"""
    markers = phone.risk_markers(xml)
    if markers:
        raise FlowMismatch(step, f"出现风控/验证页：{markers}", phone.texts(xml))


def _ensure_login_page(agree: bool) -> str:
    """确保停在「手机号登录/注册」页；顺带处理可能冒出来的独立协议同意页。"""
    phone.home()
    phone.open_app(PKG)
    xml = phone.dump()

    if "同意并继续" in " ".join(phone.texts(xml)):
        # ★ 代点这个 = 代用户做法律同意，必须由调用方显式授权
        if not agree:
            raise FlowMismatch(
                "协议页", "停在「服务协议及个人信息保护」同意页——需显式授权才代点", phone.texts(xml)
            )
        node = phone.find_exact(xml, "同意并继续")
        if not node:
            raise FlowMismatch("协议页", "找不到「同意并继续」按钮", phone.texts(xml))
        phone.tap_node(node, pause=5)
        xml = phone.dump()

    _guard_risk(xml, "打开猎聘")
    if "手机号登录" not in " ".join(phone.texts(xml)):
        raise FlowMismatch(
            "打开猎聘", "不在登录页——手机上可能还登着号，或用的是引导页", phone.texts(xml)
        )
    return xml


def login_code(phone_number: str, *, agree: bool = False) -> dict[str, Any]:
    """把手机操作到「验证码已发送」。不含提交（那要等人工给码）。

    ★ 登录页**没有「获取验证码」按钮**：点「下一步」验证码就自动发出并跳到输入页。
      照「找获取验证码」写必然卡住（2026-10-15 实测）。
    """
    m = load_map()["登录"]
    xml = _ensure_login_page(agree)

    # 1. 输手机号（预填的是上次的号，type_into 会先清空并回读校验）
    box = tuple(m["phone_login"]["phone_field"]["center"])
    result = phone.type_into(box, phone_number)
    if not result["ok"]:
        raise FlowMismatch(
            "输手机号", f"回读不一致：期望 {phone_number}，读到 {result.get('readback')!r}",
            result.get("seen"),
        )

    # 2. 勾协议（不勾「下一步」是灰的）
    xml = phone.dump()
    if not phone.tick_agreement(xml):
        raise FlowMismatch("勾协议", "找不到「我已阅读并同意」", phone.texts(xml))

    # 3. 下一步——验证码会自动发出
    xml = phone.dump()
    node = phone.find_exact(xml, "下一步") or phone.find_exact(xml, m["phone_login"]["next_button"]["match"])
    if not node:
        raise FlowMismatch("下一步", "找不到「下一步」按钮", phone.texts(xml))
    phone.tap_node(node, pause=5)

    # 可能先弹独立协议页
    xml = phone.dump()
    if "同意并继续" in " ".join(phone.texts(xml)) and agree:
        again = phone.find_exact(xml, "同意并继续")
        if again:
            phone.tap_node(again, pause=5)
            xml = phone.dump()

    _guard_risk(xml, "发出验证码")
    if "验证码已发送" not in " ".join(phone.texts(xml)):
        raise FlowMismatch("发出验证码", "没进到「验证码已发送」页", phone.texts(xml))

    return {"ok": True, "phone": phone_number}


def submit_code(code: str) -> dict[str, Any]:
    """输码并提交，返回判定结果。"""
    xml = phone.dump()
    _guard_risk(xml, "提交验证码")

    fields = phone.edittexts(xml)
    if not fields:
        raise FlowMismatch("输验证码", "页面上没有验证码输入框", phone.texts(xml))
    field = fields[0]
    for _ in range(8):
        if not field["text"]:
            break
        phone.key("KEYCODE_DEL", pause=0.3)
    phone.tap(field["center"], pause=1.2)
    phone.shell("input", "text", code)
    phone.tap(field["center"], pause=0.5)

    # 提交按钮：有的版本输满自动提交，有的要按「登录」「下一步」「确定」
    xml = phone.dump()
    for name in ("登录", "下一步", "确定"):
        node = phone.find_exact(xml, name)
        if node:
            phone.tap_node(node, pause=7)
            break
    else:
        import time as _t
        _t.sleep(7)

    xml = phone.dump()
    _guard_risk(xml, "提交后")
    seen = phone.texts(xml)
    joined = " ".join(seen)

    if any(k in joined for k in ("推荐", "职位", "我的")) or "创建招聘名片" in joined:
        return {"ok": True, "landed": seen[:6]}
    if any(k in joined for k in ("验证码错误", "已失效", "不正确")):
        raise FlowMismatch("提交验证码", "验证码被拒", seen)
    raise FlowMismatch("提交验证码", "停在无法识别的页面", seen)


def logout(*, agree: bool = False) -> dict[str, Any]:
    """退出登录。

    ★ 点「退出登录」之前**必须复核「注销账号」不在同一坐标**——它是删号，
      与「退出登录」相邻（实测相距 341px）。这条检查不能省。
    """
    m = load_map()["退出"]
    steps = m["logout_path_已登录态"]
    my_tab = {s["step"]: s for s in steps}

    phone.home()
    phone.open_app(PKG)
    xml = phone.dump()

    # 已经是登出状态就直接返回（幂等）
    if "手机号登录" in " ".join(phone.texts(xml)):
        return {"ok": True, "already_logged_out": True}

    # 新账号引导页有自己的退出入口
    if "创建招聘名片" in " ".join(phone.texts(xml)):
        more = phone.find_exact(xml, "更多操作") or phone.find(xml, "更多操作")
        if not more:
            raise FlowMismatch("退出", "引导页上找不到「更多操作」", phone.texts(xml))
        phone.tap_node(more, pause=2.5)
        xml = phone.dump()
        # ★ 同一菜单里还有「我要找工作」（会切到求职者端），只能精确点「退出登录」
        node = phone.find_exact(xml, "退出登录")
        if not node:
            raise FlowMismatch("退出", "「更多操作」菜单里没有「退出登录」", phone.texts(xml))
        phone.tap_node(node, pause=3)
    else:
        # 常规路径：我的 → 右上角设置图标 → 设置页底部「退出登录」
        tab = my_tab.get("打开底部「我的」tab")
        if tab:
            phone.tap(tuple(tab["center"]), pause=4.5)
        xml = phone.dump()
        _expect(xml, ("小猎客服", "安全服务中心", "个人主页"), "进我的页", "「我的」页")

        gear = my_tab.get("点右上角设置图标")
        if not gear:
            raise FlowMismatch("退出", "锚点表里没有设置图标坐标", phone.texts(xml))
        phone.tap(tuple(gear["center"]), pause=5)
        xml = phone.dump()
        _expect(xml, ("退出登录",), "进设置页", "设置页")

        # ★ 安全复核：注销账号必须离「退出登录」足够远
        target = phone.find_exact(xml, "退出登录")
        danger = phone.find_exact(xml, "注销账号")
        if not target:
            raise FlowMismatch("退出", "设置页上没有「退出登录」", phone.texts(xml))
        if danger:
            dy = abs(danger["center"][1] - target["center"][1])
            dx = abs(danger["center"][0] - target["center"][0])
            if dx < 100 and dy < 60:
                raise FlowMismatch(
                    "退出", f"「注销账号」（删号）离「退出登录」只有 {dy}px，拒绝点击", phone.texts(xml)
                )
        phone.tap_node(target, pause=3)

    # 确认框：一律用 BACK 取消，但这里要确认
    xml = phone.dump()
    if "是否退出登录" not in " ".join(phone.texts(xml)):
        raise FlowMismatch("退出", "没弹出「是否退出登录」确认框", phone.texts(xml))
    confirm = phone.find_exact(xml, "确定")
    cancel = phone.find_exact(xml, "取消")
    if not confirm or (cancel and confirm["center"][0] <= cancel["center"][0]):
        raise FlowMismatch("退出", "「确定」定位可疑，拒绝点击", phone.texts(xml))
    phone.tap_node(confirm, pause=5)

    xml = phone.dump()
    _guard_risk(xml, "退出后")
    if "手机号登录" not in " ".join(phone.texts(xml)):
        raise FlowMismatch("退出", "退出后没落到登录页", phone.texts(xml))
    return {"ok": True, "already_logged_out": False}
