"""登录 / 切换账号的固定序列。

每个 flow 都是确定的点击序列，**每一步都 dump 校验**；任何一步与锚点不符就抛
`FlowMismatch`，并附上当时屏幕上看到了什么。刻意不做「猜着点」——锚点失配说明
`login_map.json` 过期了，正确动作是重新校准锚点表，不是现场试。

锚点全部来自 `login_map.json`（实测、外部化，App 升级只改它），本文件不写死坐标。
"""

from __future__ import annotations

import json
import re
import time
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
    """确保停在「手机号登录/注册」页；顺带处理可能冒出来的独立协议同意页。

    **这里不调 `ensure_main_tabs`**：登录页本身就没有底部导航，那样会把好好的状态
    当成「退不回主界面」。`login_code` 的前提就是手机已经在登录页，不在就如实报告。
    """
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
            "打开猎聘", "不在登录页——手机停在别的页面（可能是上一轮留下的会话页/详情页）",
            phone.texts(xml),
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

    if any(k in joined for k in ("验证码错误", "已失效", "不正确")):
        raise FlowMismatch("提交验证码", "验证码被拒", seen)

    # ★ 判据用「**离开了验证码页**」而不是「看到了某个特定页面」。
    #   验证码页只在成功后才会消失（失败会停在原地或退回登录页），
    #   而成功之后落到哪一页是不确定的：
    #     - 已有账号 → 招聘首页（推荐/职位/我的）
    #     - **新账号 → 入职引导，一页一页往下走**（"创建招聘名片" → "请填写公司名称" → …）
    #   2026-10-15 就是在这儿误判的：验证码明明被接受、账号已经登进去，
    #   只因为那一页叫「请填写公司名称」不在我的白名单里，就报了「停在无法识别的页面」。
    if "验证码已发送" in joined:
        raise FlowMismatch("提交验证码", "仍停在验证码页——码可能没提交上去", seen)
    if "手机号登录" in joined:
        raise FlowMismatch("提交验证码", "退回了登录页——登录没成", seen)

    onboarding = "创建招聘名片" in joined or "请填写" in joined or "完成认证" in joined
    return {"ok": True, "landed": seen[:6], "onboarding": onboarding}


# 已经登出时会看到的几种页面。**不能只认登录页**——协议页和验证码页同样是登出态。
# 漏掉它们会让 logout() 走错分支：去按 BACK 找底部导航，而那种页面本来就没有，
# 于是一路退出 App 退到桌面，报出一个假的「退不回主界面」。
# （2026-10-15 真机第二次跑就栽在这儿。）
_LOGGED_OUT_MARKERS = ("手机号登录", "验证码已发送", "服务协议及个人信息保护")

# 新账号的入职引导页。**它是一串页面，不是一页**——"创建招聘名片" → "请填写公司名称" → …
# 只认第一页会在第二页上判错分支（2026-10-15 第三次跑就这样把 App 退到了桌面）。
# 这几页共同的特征是有「更多操作」（退出登录的入口藏在那儿）。
_ONBOARDING_MARKERS = ("创建招聘名片", "完成认证", "更多操作", "请填写公司名称")


def _is_logged_out_screen(xml: str) -> bool:
    joined = " ".join(phone.texts(xml))
    return any(marker in joined for marker in _LOGGED_OUT_MARKERS)


def _is_onboarding(xml: str) -> bool:
    joined = " ".join(phone.texts(xml))
    return any(marker in joined for marker in _ONBOARDING_MARKERS)


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

    # 已经是登出状态就直接返回（幂等）。
    # ★ 这条必须**先判**：登录页/协议页/验证码页都没有底部导航，
    #   先调 ensure_main_tabs 会把正常状态判成异常（甚至把 App 退出到桌面）。
    if _is_logged_out_screen(xml):
        return {"ok": True, "already_logged_out": True}

    # 新账号引导页也没有底部导航，且有自己的退出入口（藏在「更多操作」里）。
    # ★ 引导页是一串页面，不是一个——用它自己的特征判，别只认第一页。
    if _is_onboarding(xml):
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
        # 常规路径：**先退到主界面**再操作。
        # 手机可能停在上一轮留下的任何页面（会话页/详情页），而底部「我的」tab 只在主界面存在。
        result = phone.ensure_main_tabs(PKG)
        if not result["ok"]:
            raise FlowMismatch(
                "退出", "退不回主界面（底部导航没出现），没法找到「我的」入口",
                result.get("seen", []),
            )
        xml = phone.dump()

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

    # 确认框。**它不一定出现**——常规路径（我的→设置）会弹「是否退出登录」，
    # 而新账号引导页的「更多操作→退出登录」点了直接退。所以「没弹框」不等于失败：
    # 先看是不是已经退出了。（2026-10-15 第四次跑就误判在这儿，
    # 而且给用户的信息还反了——明明退干净了，却说「应该还登着原账号」。）
    xml = phone.dump()
    if "是否退出登录" not in " ".join(phone.texts(xml)):
        if _is_logged_out_screen(xml):
            return {"ok": True, "already_logged_out": False, "no_confirm": True}
        raise FlowMismatch(
            "退出", "点了「退出登录」但既没弹确认框、也没退出", phone.texts(xml)
        )
    confirm = phone.find_exact(xml, "确定")
    cancel = phone.find_exact(xml, "取消")
    if not confirm or (cancel and confirm["center"][0] <= cancel["center"][0]):
        raise FlowMismatch("退出", "「确定」定位可疑，拒绝点击", phone.texts(xml))
    phone.tap_node(confirm, pause=5)

    xml = phone.dump()
    _guard_risk(xml, "退出后")
    if not _is_logged_out_screen(xml):
        raise FlowMismatch("退出", "退出后没落到登录页", phone.texts(xml))
    return {"ok": True, "already_logged_out": False}


# ——— 职位扫描（2026-10-15）———

# 职位管理页的标记：两个都在，才算真的站在那一页上。
_JOB_PAGE_MARKERS = ("招聘中", "发职位")

# ★ 危险页：「发职位」的**空白新建表单**。误入并提交 = **真的对外发布一个职位**。
#   这不是假想——daily_round 当年就是栽在这儿：
#   `.round_daily_round.log:23-24`「未完成…误入『发布职位』表单，已退出」。
#   用「请填写职位描述」这个**占位符**当标记，而不是「职位描述」：编辑已有职位时
#   那一页也有「职位描述」标签，但里面是有内容的，不会是「请填写」。
_PUBLISH_FORM_MARKERS = ("请填写职位描述", "发布职位")

# 卡片第二行的特征：有薪资（25-45k）或地点（上海-黄浦区）。
# 没有这个特征的行不当详情——卡片上还有「看过我/沟通过/收到简历」这类计数行。
_DETAIL_RE = re.compile(r"\d+\s*[-~]?\s*\d*\s*[kK]|-\s*[一-龥]{2,}[区市县]")


def _guard_publish_form(xml: str) -> None:
    """看到「发职位」空白表单就立刻退出并报错。"""
    joined = " ".join(phone.texts(xml))
    hit = [marker for marker in _PUBLISH_FORM_MARKERS if marker in joined]
    if hit:
        phone.back()
        raise FlowMismatch(
            "扫描职位", f"误入「发职位」新建表单（看到 {hit}），已退出，未做任何提交", phone.texts(xml)
        )


def _find_startswith(xml: str, prefix: str) -> dict[str, Any] | None:
    """找 label 以 prefix 开头的节点。

    「招聘中」那个 tab 的文字实测是「招聘中 N」（带数量），所以 `find_exact` 匹配不上；
    但也不能退回子串匹配——那会命中页面里别处出现的「招聘中」。用前缀刚好。
    """
    for node in phone.nodes(xml):
        if phone.label(node).startswith(prefix):
            return node
    return None


def _collect_job_cards(xml: str, seen: dict[str, dict[str, Any]]) -> int:
    """把这一屏的职位卡收进 `seen`，返回新增条数。

    带上 `bounds`：抓 JD 时要按卡片位置算出点击点（见 `_card_tap_point`）。
    """
    added = 0
    for node in phone.nodes(xml):
        text = (node["desc"] or node["text"]).strip()
        if "\n" not in text:
            continue
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if len(lines) < 2 or len(lines[0]) > 40:
            continue
        details = next((line for line in lines[1:] if _DETAIL_RE.search(line)), "")
        if not details:
            continue
        title = lines[0]
        if title not in seen:
            seen[title] = {"title": title, "details": details, "bounds": node["bounds"]}
            added += 1
    return added


# ——— 抓 JD（2026-10-15 实测）———

_JD_LABEL = "职位介绍"

# ★★ 详情页底部并排两颗按钮：「关闭职位」(237,2111) 会把职位下架，
#    「编辑职位」(747,2111) 会弹「该职位正在使用【邀请应聘】服务，修改职位将导致服务终止」。
#    **两颗都不碰**——读 JD 走详情页本身，是纯只读的。
_JD_DENY = (
    "关闭职位", "编辑职位", "保存", "发布", "发布职位", "提交", "确定", "完成", "更新", "删除",
)


def _card_tap_point(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    """点职位卡上一个安全的落点。

    实测（2026-10-15）点卡的标题区进「职位详情页」。落点取卡片左上角偏移 (300,100)：
    避开右上角那颗「火爆刷」付费推广按钮（x 786~996），也避开卡片下部的
    看过我/沟通过/收到简历三个计数按钮。
    """
    x1, y1, x2, y2 = bounds
    return (min(x1 + 300, x2 - 30), min(y1 + 100, y2 - 30))


def _jd_from_screen(xml: str) -> str:
    """从一屏 dump 里取 JD：「职位介绍」标签下方最长的那段文本。"""
    label = phone.find_exact(xml, _JD_LABEL)
    nodes = phone.nodes(xml)
    if label:
        top = label["bounds"][3]
        pool = [
            n for n in nodes
            if n["bounds"][1] >= top - 20 and (n["text"] or n["desc"]).strip()
        ]
        if pool:
            pick = max(pool, key=lambda n: len(n["text"] or n["desc"]))
            return (pick["desc"] or pick["text"]).strip()
    # 标签没找到就退而求其次：全屏最长的那个文本节点
    pick = max(nodes, key=lambda n: len(n["text"] or n["desc"]), default=None)
    return ((pick["desc"] or pick["text"]).strip() if pick else "")


def _extract_jd(max_scroll: int = 3) -> tuple[str, bool]:
    """读 JD，返回 `(jd_text, complete)`。调用前必须已在职位详情页。

    实测（2026-10-15）：「职位介绍」下方那个节点的 content-desc **一次性给出全部
    543 字**，不需要滚动。

    ★ 但那是**一个职位**的实测行为，不能当保证——Flutter 有可能只暴露它已布局出来的
      那部分。所以做一次**增长检测**：往下滚一屏再取一次，文本变长说明被截断了，
      继续滚（最多 `max_scroll` 次）直到不再增长。**没增长过才敢说 complete**，
      而只有 complete 的 JD 才会被自动启用（见 `app/job_sync.py`）。
    """
    text = _jd_from_screen(phone.dump())
    for _ in range(max_scroll):
        before = len(text)
        phone.swipe((540, 1600), (540, 900))
        text = _jd_from_screen(phone.dump())
        if len(text) <= before:
            return text, True
    return text, False


def _goto_job_list(step: str) -> str:
    """把手机带到「职位管理 → 招聘中」列表，返回那一屏的 dump。

    每一步都校验；失配即抛，绝不猜着点。`scan_jobs` 与 `fetch_jds` 共用这一条路，
    所以锚点只有一份。
    """
    m = load_map()["职位扫描"]
    result = phone.ensure_main_tabs(PKG)
    if not result["ok"]:
        raise FlowMismatch(step, "退不回主界面，找不到底部「职位」tab", result.get("seen", []))

    xml = phone.dump()
    _guard_risk(xml, step)
    tab = phone.find_exact(xml, m["phone_tab"]["match"])
    if not tab:
        raise FlowMismatch(step, "主界面底部没有「职位」tab", phone.texts(xml))
    phone.tap_node(tab, pause=4.5)

    xml = phone.dump()
    _guard_risk(xml, "进职位页")
    _guard_publish_form(xml)
    joined = " ".join(phone.texts(xml))
    if not all(marker in joined for marker in _JOB_PAGE_MARKERS):
        raise FlowMismatch(step, "没进到职位管理页（看不到「招聘中/发职位」）", phone.texts(xml))

    recruiting = _find_startswith(xml, "招聘中")
    if not recruiting:
        raise FlowMismatch(step, "职位管理页上没有「招聘中」筛选 tab", phone.texts(xml))
    phone.tap_node(recruiting, pause=3)
    xml = phone.dump()
    _guard_publish_form(xml)
    return xml


def scan_jobs(*, max_swipe: int = 6, budget_seconds: float = 180.0) -> dict[str, Any]:
    """扫出当前账号「招聘中」的职位列表。**只读**。

    两条绝不越的线：
    - **不点职位卡**：点进去是**候选人数据页**，那屏上有「不合适」「看简历」这类
      会真实影响候选人的按钮。
    - **不碰「发职位」**：那是新建表单，提交了就是真的对外发布一个职位。

    返回 `{"ok", "jobs": [{"title","details"}], "swipes", "truncated", "reason", "seen"}`。
    `reason` 非空 = 根本没扫成（不在登录态 / 还在引导页），**必须与「这个号确实没职位」
    区分开**——账号 3 就是新号，登录后落在「创建招聘名片」引导页，误判成 0 职位
    会让它静默空转。
    """
    m = load_map()["职位扫描"]
    phone.home()
    phone.open_app(PKG)
    xml = phone.dump()

    if _is_logged_out_screen(xml):
        return {"ok": False, "reason": "not_logged_in", "jobs": [], "seen": phone.texts(xml)}
    if _is_onboarding(xml):
        return {"ok": False, "reason": "onboarding", "jobs": [], "seen": phone.texts(xml)}

    xml = _goto_job_list("扫描职位")

    # ★ 时间预算：扫描跑在「登录成功之后、相位还是非终态」这段时间里，
    #   卡住不动会让 `.login_in_flight` 哨兵一直挡着 main_round。每次 dump 都要
    #   2-3 秒，再叠加 adb 自己的 90 秒超时，没有预算的话最坏能拖很久。
    deadline = time.monotonic() + budget_seconds

    seen: dict[str, dict[str, str]] = {}
    new = _collect_job_cards(xml, seen)
    swipes = 0
    while new and swipes < max_swipe and time.monotonic() < deadline:
        phone.swipe(tuple(m["list"]["scroll_from"]), tuple(m["list"]["scroll_to"]))
        swipes += 1
        new = _collect_job_cards(phone.dump(), seen)

    jobs = list(seen.values())
    # 停下来了但这一屏还出新卡 → 要么滑到上限、要么时间用光，列表都没到底。
    # **绝不能拿它当全集**：上层据此只增不删（见 app/job_sync.py）。
    exhausted = swipes >= max_swipe or time.monotonic() >= deadline
    truncated = bool(new) and exhausted
    return {
        "ok": True,
        "reason": "",
        "jobs": jobs,
        "swipes": swipes,
        "truncated": truncated,
        "seen": phone.texts(xml),
    }


def _page_is_for(xml: str, title: str) -> bool:
    """详情页上的职位名是点的那张卡吗？——防止点歪了把 A 的 JD 记到 B 头上。

    只看页面上半部分：标题在 y≈331，而下半部分是 JD 正文，正文里**会**出现职位名，
    不限定区域会被正文里的字样骗过去。
    """
    want = title.replace("…", "").strip()
    for node in phone.nodes(xml):
        if node["bounds"][1] >= 600:
            continue
        got = phone.label(node).replace("…", "").strip()
        if len(got) < 4:
            continue
        if got == want or got.startswith(want) or want.startswith(got):
            return True
    return False


def fetch_jds(
    titles: list[str], *, max_jobs: int = 5, budget_seconds: float = 150.0
) -> dict[str, Any]:
    """逐个职位进详情页读 JD。**全程只读**。

    ★ **为什么走详情页而不是「编辑职位」表单**：实测点「编辑职位」会弹
      「该职位正在使用【邀请应聘】服务，修改职位将导致服务终止，确定修改职位吗？」
      ——进表单是有真实代价的副作用。而详情页的「职位介绍」里 JD 是**完整的**，
      且纯只读。

    ★ 详情页上**一次都不点**：只有 dump / 滚动 / BACK。底部并排的
      「关闭职位」与「编辑职位」两颗按钮因此根本不在可达路径里。

    ★ 这里**不判断 JD 够不够长**——只如实报告读到什么。阈值与「要不要启用」
      是 `app/job_sync.py` 的事，免得同一个数字在两地各写一遍。

    单个职位失败不影响其余；`failed` 里带原因，调用方如实上报。
    """
    jds: dict[str, str] = {}
    complete: dict[str, bool] = {}
    failed: dict[str, str] = {}
    skipped: list[str] = []
    visited = 0
    deadline = time.monotonic() + budget_seconds

    seen: dict[str, dict[str, Any]] = {}
    _collect_job_cards(_goto_job_list("抓 JD"), seen)

    for title in titles:
        if visited >= max_jobs or time.monotonic() >= deadline:
            skipped.append(title)
            continue

        card = seen.get(title)
        if card is None:
            # 列表可能已经滚过、或这次没扫到——重扫一屏再找
            seen.clear()
            _collect_job_cards(phone.dump(), seen)
            card = seen.get(title)
        if card is None:
            failed[title] = "not_on_list"
            continue

        visited += 1
        phone.tap(_card_tap_point(card["bounds"]), pause=3)
        detail = phone.dump()
        _guard_risk(detail, "职位详情页")

        if not phone.find_exact(detail, _JD_LABEL):
            failed[title] = "wrong_page"
            phone.back()
            continue
        # ★ 身份核对：读到的是**这个**职位的 JD 吗
        if not _page_is_for(detail, title):
            failed[title] = "title_mismatch"
            phone.back()
            continue

        text, done = _extract_jd()
        got_jd = bool(text.strip())
        if got_jd:
            jds[title] = text.strip()
            complete[title] = done
        else:
            failed[title] = "no_content"

        phone.back()
        back_xml = phone.dump()
        if not all(m in " ".join(phone.texts(back_xml)) for m in _JOB_PAGE_MARKERS):
            # 退不回列表就别在错的页面上继续了——把已经拿到的带回去，如实说明
            if not got_jd:
                failed[title] = "back_failed"
            break

    return {
        "ok": True,
        "jds": jds,
        "complete": complete,
        "failed": failed,
        "skipped": skipped,
        "visited": visited,
    }
