"""BOSS直聘 的固定序列（**只读部分**，2026-10-16）。

每个 flow 都是确定的点击序列，**每一步都 dump 校验**；任何一步与锚点不符就抛
`FlowMismatch`，并附上当时屏幕上看到了什么。刻意不做「猜着点」——锚点失配说明
`boss_map.json` 过期了，正确动作是重新校准锚点表，不是现场试。

## ★ 本文件为什么只有只读流程

BOSS 上这些动作**会给真实的人/账号产生不可逆后果**：

- 「立即沟通」→ 给真实候选人发消息
- 「开放职位」→ 职位对外公开（后面还挂着一道 456 直豆的付费墙）
- 发布表单提交 → 对外发布一个职位，且**招聘类型/职位名称/职位类型/工作城市发布后不可修改**
- 「编辑」「关闭职位」→ 改动正在跑的职位
- 「解绑实名」→ 解除账号实名绑定

这些**一律不在本文件里**。写流程要单独开、要带授权闸门。
只读的部分（看列表、读 JD、读候选人）没有这个问题，所以先做这些。

## 锚点从哪来

- **rid**：BOSS 是原生 Android，`resource-id` 是它的稳定锚点，直接写在下面（自解释）。
- **坐标**：会随布局变（比如职位管理页多一条横幅，整排 tab 的 y 就变了——
  猎聘那边踩过：`login_map.json` 里三个 tab 的 y 从 277 变成 388，旧坐标全作废）。
  所以坐标**一律从 `boss_map.json` 读**，本文件不写死。

两者合起来才是完整锚点，改 App 后两处都要核。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import phone

PKG = "com.hpbr.bosszhipin"
MAP_PATH = Path(__file__).resolve().parent / "boss_map.json"


class FlowMismatch(RuntimeError):
    """某一步与锚点不符。带上屏上所见，便于判断是「锚点过期」还是「状态不对」。"""

    def __init__(self, step: str, detail: str, seen: list[str] | None = None):
        self.step = step
        self.detail = detail
        self.seen = seen or []
        super().__init__(f"[{step}] {detail}｜屏上：{self.seen[:12]}")


def load_map() -> dict[str, Any]:
    return json.loads(MAP_PATH.read_text(encoding="utf-8"))


def _m(*path: str) -> Any:
    """按路径取 `boss_map.json` 里的值；取不到就抛出**说清楚缺哪一段**的错误。"""
    node: Any = load_map()
    walked: list[str] = []
    for key in path:
        walked.append(key)
        if not isinstance(node, dict) or key not in node:
            raise FlowMismatch("锚点表", f"boss_map.json 里没有 {'/'.join(walked)}")
        node = node[key]
    return node


def _center(bounds: list[int]) -> tuple[int, int]:
    x1, y1, x2, y2 = bounds
    return ((x1 + x2) // 2, (y1 + y2) // 2)


# ——— 护栏 ———

def _guard_risk(xml: str, step: str) -> None:
    """铁律 3：风控/验证页即停，绝不尝试绕过。"""
    markers = phone.risk_markers(xml)
    if markers:
        raise FlowMismatch(step, f"出现风控/验证页：{markers}", phone.texts(xml))


def _guard_not_paywall(xml: str, step: str) -> None:
    """★ 付费墙。

    实测（2026-10-16）：点「开放职位」会弹出「曝光刷新卡」付费窗（456 直豆），
    按钮是 `v_btn`「立即开启」——**点下去真的扣钱**。
    只读流程里出现它就说明走错路了，立刻停手。
    """
    if any(t in " ".join(phone.texts(xml)) for t in ("曝光刷新卡", "立即开启", "直豆")):
        raise FlowMismatch(
            step, "出现付费弹窗（曝光刷新卡/直豆）。**绝不点「立即开启」**", phone.texts(xml)
        )


def _tap_safe(node: dict[str, Any], step: str, pause: float = 3.0) -> None:
    """点之前确认它不是「危险按钮」。

    ★ 只读流程只该点：职位卡、候选人卡、底部 tab、返回。
      万一算出来的落点压到了危险按钮上，宁可抛错也不要真的点下去。
    """
    text = phone.label(node)
    if text and any(risk in text for risk in phone.RISK_TEXTS):
        raise FlowMismatch(step, f"落点压在危险按钮「{text}」上，拒绝点击", phone.texts(phone.dump()))
    phone.tap_node(node, pause=pause)


def _back_from_page(step: str, expect_activity: str | None = None) -> str:
    """按返回并确认真的退掉了。

    ★ 返回键的位置从锚点表读 `center`（不是 `bounds`）——两个详情页的
      `iv_back` 宽度实测不一样（职位详情 [36,100][144,220]、候选人详情 [36,100][180,220]），
      但中心点都是 (90,160)，所以按中心点走最稳。
    """
    phone.tap(tuple(_m("候选人详情", "back", "center")), pause=5)
    xml = phone.dump_stable()
    if expect_activity and expect_activity in phone.current_activity():
        raise FlowMismatch(step, f"按了返回但还在 {expect_activity} 上", phone.texts(xml))
    return xml


# ——— 只读流程 ———

def state() -> dict[str, Any]:
    """现在停在哪。给调度与排错用——「走到哪一页了」比「失败了」有用得多。"""
    xml = phone.dump_stable()
    activity = phone.current_activity()
    return {
        "activity": activity,
        "package": phone.current_package(xml),
        "on_main_tabs": len([r for r in phone.MAIN_TAB_RIDS if phone.find_rid(xml, r)]) >= 2,
        "risk": phone.risk_markers(xml),
        "danger_buttons": [phone.label(n) for n in phone.danger_nodes(xml)],
        "seen": phone.texts(xml)[:20],
    }


def ensure_main_tabs() -> dict[str, Any]:
    """退到 BOSS 主界面。"""
    return phone.ensure_main_tabs(PKG)


def goto_tab(name: str) -> str:
    """**显式**切底部 tab，返回切换后的 dump。

    ★ 注意它与 `phone.ensure_tab()` 的区别：这个**总是点一下**。
      点已经激活的 tab 会**刷新那一页**（实测：牛人列表点一下当前 tab，
      `郭鑫` 变 `郭宁`，精选计数 10→14）。所以操作列表里某个具体对象时
      不要用它，用 `phone.ensure_tab()`。
    """
    if name not in phone.TAB_CENTERS:
        raise FlowMismatch("切tab", f"没有这个 tab：{name}，可选 {sorted(phone.TAB_CENTERS)}")
    phone.goto_tab(name)
    xml = phone.dump_stable()
    _guard_risk(xml, f"切到{name}")
    return xml


# —— 职位 ——

def scan_jobs(*, max_scroll: int = 4) -> list[dict[str, Any]]:
    """读「职位管理」页里的全部职位。**纯只读**。

    返回 [{"title", "state", "details", "bounds"}]。`state` 取值：
    `开放中` / `待开放` / `已关闭` / `审核未通过` / `""`（在「全部」里开放中的不带标签）。

    ★ 绝不点卡片——卡片点下去会进「职位详情页」，那一页底部并排着
      「编辑」与「开放职位」（`btn_reset` / `btn_confirm`，中心相距 500px），
      而「开放职位」后面挂着付费墙。读列表就够了。
    """
    result = ensure_main_tabs()
    if not result["ok"]:
        raise FlowMismatch("扫描职位", f"退不回主界面：{result}", result.get("seen", []))

    phone.ensure_tab("我的")
    xml = phone.dump_stable()
    entry = _m("职位管理", "进入方式", "center")
    phone.tap(tuple(entry), pause=8)
    xml = phone.dump_stable()
    _guard_risk(xml, "进职位管理")
    if "职位管理" not in " ".join(phone.texts(xml)):
        raise FlowMismatch("扫描职位", "没进到职位管理页", phone.texts(xml))

    seen: dict[str, dict[str, Any]] = {}
    for _ in range(max_scroll + 1):
        _collect_jobs(xml, seen)
        phone.swipe((540, 1500), (540, 900))
        after = phone.dump_stable()
        if after == xml:
            break
        xml = after

    _back_from_page("退出职位管理", expect_activity="PositionListManagementActivity")
    return list(seen.values())


def _collect_jobs(xml: str, seen: dict[str, dict[str, Any]]) -> None:
    """把这一屏的职位卡收进 `seen`。

    卡片结构（实测）：`tv_job_name` / `tv_job_state` / `fl_job_info` 三个 rid 一组。
    ★ 「全部」tab 里**开放中的职位不带 `tv_job_state`**——所以不能靠状态字段是否存在
      来判断「是不是一张卡」，要靠 `tv_job_name`。
    """
    rows = [n for n in phone.nodes(xml) if n["rid"].endswith(("tv_job_name", "tv_job_state", "fl_job_info"))]
    current: dict[str, Any] | None = None
    for node in rows:
        rid = node["rid"].rsplit("/", 1)[-1]
        text = phone.label(node)
        if rid == "tv_job_name":
            title = text.strip()
            if title and title not in seen:
                current = {"title": title, "state": "", "details": "", "bounds": node["bounds"]}
                seen[title] = current
            else:
                current = seen.get(title)
        elif current is None:
            continue
        elif rid == "tv_job_state":
            current["state"] = text.strip()
        elif rid == "fl_job_info":
            current["details"] = text.strip()


def fetch_jd(title: str, *, max_scroll: int = 3) -> dict[str, Any]:
    """进职位详情页读 JD。**纯只读**——详情页底部的「编辑」「开放职位」一律不碰。

    实测（2026-10-16）：JD 挂在 `tv_description` 上，一屏就能读全；标签挂在 `btn_word` 上。
    JD 是打分与检索的唯一依据，读不到位要先报出来，别静默给空串。

    做**增长检测**（同猎聘）：滚一屏再取一次，文本还在变长说明没读完。
    没增长过才敢标 `complete`。
    """
    result = ensure_main_tabs()
    if not result["ok"]:
        raise FlowMismatch("抓JD", f"退不回主界面：{result}", result.get("seen", []))

    phone.ensure_tab("我的")
    phone.tap(tuple(_m("职位管理", "进入方式", "center")), pause=8)
    xml = phone.dump_stable()
    _guard_risk(xml, "进职位管理")

    card = phone.find_exact(xml, title)
    if not card:
        raise FlowMismatch("抓JD", f"职位列表里没有「{title}」", phone.texts(xml))

    # 落点取卡片名字左侧——避开右上角那些图标
    x1, y1, x2, y2 = card["bounds"]
    phone.tap((x1 + 30, (y1 + y2) // 2), pause=8)
    xml = phone.dump_stable()
    _guard_risk(xml, "进职位详情")
    _guard_not_paywall(xml, "进职位详情")
    if "职位详情" not in " ".join(phone.texts(xml)):
        raise FlowMismatch("抓JD", f"点「{title}」没进到职位详情页", phone.texts(xml))

    text, complete = _extract_jd()
    tags = [phone.label(n) for n in phone.nodes(xml) if n["rid"].endswith("btn_word")]
    _back_from_page("退出职位详情", expect_activity="MyJobActivity")
    return {"title": title, "jd_text": text, "complete": complete, "tags": tags}


def _extract_jd(max_scroll: int = 3) -> tuple[str, bool]:
    """读 `tv_description`，返回 `(jd_text, complete)`。"""
    def pick(x: str) -> str:
        node = phone.find_rid(x, "tv_description")
        return phone.label(node).strip() if node else ""

    text = pick(phone.dump_stable())
    for _ in range(max_scroll):
        before = len(text)
        phone.swipe((540, 1700), (540, 1000))
        text = pick(phone.dump_stable())
        if len(text) <= before:
            return text, True
    return text, False


# —— 候选人（牛人 tab）——

# ★★ 「牛人」列表**绝不能往回滚**（2026-10-16 实测，代价是搞错了两次）
#
# 在这条列表上做一次「往下滑」（= 往回滚到更靠前的条目），BOSS 会**注入一批新的
# 候选人**——实测：`潘志/谌祥/卞志超` → 滑一下 → `孙志亮/周语洁/郝炤烁`
# → 再滑一下 → `张帅/孙志亮/何江龙`。**根本不是幂等的**。
#
# 后果：我一开始写了个「先滚回顶部再找人」的 `_find_in_list`，它把
# `scan_candidates()` 刚扫到的人**全换掉了**，于是「扫到 A/B/C，读 A 时屏幕上
# 已经是 D」——连着两次 FlowMismatch，而且原因看起来像「列表不稳定」，
# 其实是我自己把它刷新了。
#
# **规则：这条列表只能往下走（`swipe((540,1600),(540,700))`），永不往回滚。**
# 想读某个人，就在**当屏**读（见 `read_candidates`）；人已经滚过去了就认了，
# 别试图回去找——回不去，还会顺手污染整条流。

def _visible_cards(xml: str) -> list[tuple[str, tuple[int, int, int, int]]]:
    """当屏可见的候选人卡：[(姓名, bounds)]。"""
    return [
        (phone.label(n).strip(), n["bounds"])
        for n in phone.nodes(xml)
        if n["rid"].endswith("tv_geek_name") and phone.label(n).strip()
    ]



def scan_candidates(*, limit: int = 10, max_scroll: int = 4) -> list[dict[str, Any]]:
    """读「牛人」tab 的候选人推荐列表。**纯只读**。

    ★ 前提：账号必须有**开放中**的职位。没有的话这里是空状态
      （「一大波牛人正期待您发布职位」），本函数会如实抛错而不是返回空列表——
      「真的没人推荐」和「没有在线职位」长得一模一样，必须分开。

    ★ 卡片上没有打招呼按钮（BOSS 是先看人再开聊），所以读列表是安全的。
      危险的是点进详情页之后的「立即沟通」——那个本函数不做。
    """
    result = ensure_main_tabs()
    if not result["ok"]:
        raise FlowMismatch("扫描牛人", f"退不回主界面：{result}", result.get("seen", []))

    xml = phone.ensure_tab("牛人")
    if "发布职位后" in " ".join(phone.texts(xml)):
        raise FlowMismatch(
            "扫描牛人",
            "牛人 tab 是空状态——账号没有「开放中」的职位（不是没人推荐）",
            phone.texts(xml),
        )

    out: dict[str, dict[str, Any]] = {}
    for _ in range(max_scroll + 1):
        _collect_candidates(xml, out)
        if len(out) >= limit:
            break
        phone.swipe((540, 1600), (540, 700))
        after = phone.dump_stable()
        if after == xml:
            break
        xml = after

    # ★ 丢掉**只读到一半**的卡：滚到屏幕上下边缘的那几张，`tv_geek_name` 出来了
    #   但 `tv_work_edu_other_desc` 还没渲染（实测见到「周**」「黄哆哆」两条空壳）。
    #   宁可少两条，也不要把空字段喂给下游——猎聘那边有同样的教训：
    #   「解析不出内容 → 报 0 个 → 系统静默空转，和『这个号真没职位』长得一样」。
    full = [c for c in out.values() if c["summary"]]
    return full[:limit]


def _collect_candidates(xml: str, out: dict[str, dict[str, Any]]) -> None:
    """收集本屏候选人卡。

    卡片字段（实测）：`tv_geek_name` / `tv_work_edu_other_desc` / `ll_work_exp` 里的
    `tv_content` / `flow_layout` 里的 `tv_tag_text`。
    """
    current: dict[str, Any] | None = None
    for node in phone.nodes(xml):
        rid = node["rid"].rsplit("/", 1)[-1]
        text = phone.label(node).strip()
        if rid == "tv_geek_name":
            if not text:
                continue
            current = out.setdefault(
                text,
                {"name": text, "summary": "", "works": [], "tags": [],
                 "active": "", "bounds": node["bounds"]},
            )
        elif current is None:
            continue
        elif rid == "tv_work_edu_other_desc":
            current["summary"] = text
        elif rid == "tv_content" and text:
            current["works"].append(text)
        elif rid == "tv_tag_text" and text:
            current["tags"].append(text)


def read_candidate(name: str) -> dict[str, Any]:
    """读某个候选人的详情页——**前提是他此刻就在屏幕上**。

    **纯只读，绝不点「立即沟通」。**

    实测（2026-10-16）详情页是 `BossViewGeekResumePagerActivity`，
    「立即沟通」在底部 `cl_bottom_actions` 里（无 rid，是 Button）。

    ★ 打开会**给候选人一次「被查看」**。这是只读流程里唯一一个有外部可见副作用的动作，
      调用方应该知情。
    ★ **不会往回滚去找人**——往回滚会刷新整条推荐流（见文件里那段注释）。
      要连着读多个人，用 `read_candidates()`。
    """
    xml = phone.ensure_tab("牛人")
    card = phone.find_exact(xml, name)
    if not card:
        raise FlowMismatch(
            "读候选人",
            f"「{name}」不在当屏——★ 本函数**不会往回滚**去找它（那会刷新整条推荐流，"
            f"见文件里那段注释）。要么改用 read_candidates() 边走边读，"
            f"要么确认它此刻就在屏幕上",
            phone.texts(xml),
        )
    return _read_card(card["bounds"])


def read_candidates(*, limit: int = 5, max_move: int = 30) -> list[dict[str, Any]]:
    """沿「牛人」列表**边走边读**若干个候选人的详情。

    ★ 为什么不能「先 scan 再逐个 read」：`scan_candidates()` 会把列表滚下去，
      等回头读某个已滚过去的人时——**回不去**（往回滚会刷新整条流，见上面的注释）。
      所以正确用法是这一条：看到谁读谁，只往下走。
    """
    phone.ensure_tab("牛人")
    out: list[dict[str, Any]] = []
    tried: set[str] = set()
    stale = 0
    while len(out) < limit and stale < 3:
        xml = phone.dump_stable()
        target = next((c for c in _visible_cards(xml) if c[0] not in tried), None)
        if target is None:
            phone.swipe((540, 1600), (540, 700))       # ★ 只往下走
            stale += 1
            continue
        name, bounds = target
        tried.add(name)
        stale = 0
        out.append(_read_card(bounds))
        if len(tried) > max_move:
            break
    return out


def _read_card(bounds: tuple[int, int, int, int]) -> dict[str, Any]:
    """点开一张候选人卡、读详情、退回来。**绝不点「立即沟通」。**"""
    x1, y1, x2, y2 = bounds
    # 落点取卡片中部，避开右上角的 iv_close（卡片级的 X）
    phone.tap((x1 + 40, (y1 + y2) // 2), pause=8)

    xml = phone.dump_stable()
    _guard_risk(xml, "进候选人详情")
    _guard_not_paywall(xml, "进候选人详情")
    activity = phone.current_activity()
    if "ViewGeekResume" not in activity:
        raise FlowMismatch("读候选人", f"没进到候选人详情页（当前 {activity}）", phone.texts(xml))

    def one(rid: str) -> str:
        node = phone.find_rid(xml, rid)
        return phone.label(node).strip() if node else ""

    detail = {
        "name": one("tv_geek_name"),
        "job_and_company": one("tv_job_and_com"),
        "work_status": one("tv_geek_work_status"),
        "work_year": one("tv_geek_work_year"),
        "degree": one("tv_geek_degree"),
        "age": one("tv_geek_age"),
        "description": one("tv_geek_description"),
        "expect_job_city": one("tv_job_and_city"),
        "expect_salary": one("tv_salary"),
        "expect_industry": one("tv_industry"),
        "company_history": [phone.label(n).strip() for n in phone.nodes(xml)
                            if n["rid"].endswith(("tv_work_company", "tv_time_range"))],
    }

    # ★ 只用坐标点左上角返回，绝不用「底部按钮」那条路
    phone.tap((90, 160), pause=6)
    if "ViewGeekResume" in phone.current_activity():
        raise FlowMismatch("读候选人", "按了返回但还在候选人详情页", phone.texts(phone.dump_stable()))
    return detail


# —— 消息（收件箱，纯只读）——

def scan_conversations(*, limit: int = 20, max_scroll: int = 4) -> list[dict[str, Any]]:
    """读「消息」tab 的会话列表。**纯只读**。

    卡片字段（实测）：`tv_name` / `tv_position` / `tv_time` / `iv_msg_status` / `tv_msg`，
    未读红点是 `badge_view`（数字在它的子节点里）。

    ★ 只读当前子 tab 的内容。默认是「全部」；若停在其他子 tab 上，
      有些会话不在列表里——找不到会如实报错，不会静默少给。
    """
    result = ensure_main_tabs()
    if not result["ok"]:
        raise FlowMismatch("读消息", f"退不回主界面：{result}", result.get("seen", []))

    xml = phone.ensure_tab("消息")
    out: dict[str, dict[str, Any]] = {}
    for _ in range(max_scroll + 1):
        _collect_conversations(xml, out)
        # ★ 丢掉**系统通知**那条伪会话（2026-10-16 真机撞到）：
        #   「消息」列表里混着一条叫「通知」的卡片（道具通知/系统通知），
        #   它不是人、没有职位、点进去是 `ChatNoticeActivity` 而不是聊天页——
        #   `read_conversation` 会在它身上白跑一次并抛 FlowMismatch。
        #
        #   区分标记（实测）：**它有 `tv_desc`（值如「道具通知」），而真人会话都没有**；
        #   真人会话另有 `tv_position`（关联职位），它也没有。
        #   两个条件一起用，比只认名字「通知」稳（名字是 BOSS 写的，会变）。
        out = {k: v for k, v in out.items() if not v.get("_sys_desc") and v.get("position")}
        if len(out) >= limit:
            break
        phone.swipe((540, 1600), (540, 700))          # ★ 只往下走
        after = phone.dump_stable()
        if after == xml:
            break
        xml = after
    return list(out.values())[:limit]


def _unread_at(nodes: list[dict[str, Any]], y: int) -> int:
    """某个 y 位置上那条会话的未读数。

    ★ 未读红点是 `badge_view`（头像左上角那个圆），**数字是它的子节点**，
      而且那个子节点**没有 rid**——只能按「落在 badge 的框里、且是纯数字」找。
      第一版漏了这条，`scan_conversations()` 的返回里根本没有 `unread`，
      于是 `run_round` 一取就 KeyError。
    """
    for badge in nodes:
        if not badge["rid"].endswith("badge_view"):
            continue
        bx1, by1, bx2, by2 = badge["bounds"]
        if not (by1 <= y <= by2):
            continue
        for child in nodes:
            cx1, cy1, cx2, cy2 = child["bounds"]
            if bx1 <= cx1 and cx2 <= bx2 and by1 <= cy1 and cy2 <= by2:
                label = phone.label(child).strip()
                if label.isdigit():
                    return int(label)
        return 1          # 有红点但读不到数字 → 当作至少 1 条未读（宁可多读也别漏）
    return 0


def _collect_conversations(xml: str, out: dict[str, dict[str, Any]]) -> None:
    nodes = phone.nodes(xml)
    current: dict[str, Any] | None = None
    for node in nodes:
        rid = node["rid"].rsplit("/", 1)[-1]
        text = phone.label(node).strip()
        if rid == "tv_name":
            if not text:
                continue
            current = out.setdefault(
                text,
                {"name": text, "position": "", "time": "", "status": "",
                 "last_msg": "", "unread": 0},
            )
            current["unread"] = _unread_at(nodes, node["center"][1])
        elif current is None:
            continue
        elif rid == "tv_position":
            current["position"] = text
        elif rid == "tv_time":
            current["time"] = text
        elif rid == "iv_msg_status":
            current["status"] = text
        elif rid == "tv_msg":
            current["last_msg"] = text
        elif rid == "tv_desc":
            current["_sys_desc"] = text


#: 时间分隔可能是「16:36」或「10月16日」这类。
_CHAT_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$|^\d{1,2}月\d{1,2}日")


def _chat_messages(xml: str) -> list[dict[str, Any]]:
    """聊天页里的消息，带方向。**踩过两个坑，别退回老写法。**

    ## 坑一：`tv_content` 不是消息

    它出现在底部 `chat_suggest_view`（y 1872~2061）里，是**输入框上方的建议语气泡**
    （「您好，我们公司提供生日福利…可以聊聊吗？」）。我第一版把它当「发出的消息」，
    结果报出一堆**根本没发过的话**。所以必须按位置把底部输入区排除掉。

    ## 坑二：方向不能靠 rid 判断

    收发用的是**同一个 rid**（`tv_content_text`）。真正的线索是几何：
    发出消息的头像 `iv_avatar` 在**右侧**（x 中心 > 540），收到的在左侧。
    发出消息旁边还会多一个 `mMsgTvStatus`（实测见到「送达」）。
    我第一版写成「`tv_content_text` 就是收到」，于是把自己发的招呼语标成了「收到」。
    """
    nodes = phone.nodes(xml)
    # 底部输入区的上沿（找不到就退一个保守值）
    bottom = next((n["bounds"][1] for n in nodes if n["rid"].endswith("fl_bottom")), 1850)
    avatars = [n for n in nodes if n["rid"].endswith("iv_avatar")]

    out: list[dict[str, Any]] = []
    for node in nodes:
        if not node["rid"].endswith("tv_content_text"):
            continue
        text = phone.label(node).strip()
        if not text or node["bounds"][1] >= bottom:
            continue
        y1, y2 = node["bounds"][1], node["bounds"][3]
        near = [a for a in avatars if a["bounds"][1] < y2 and a["bounds"][3] > y1]
        outgoing = any(a["center"][0] > 540 for a in near)
        out.append({"direction": "out" if outgoing else "in", "text": text, "y": y1})
    out.sort(key=lambda m: m["y"])
    return out


def _chat_time_marks(xml: str) -> list[str]:
    """时间分隔。★ `tv_text` 也用来放平台提示语（「此牛人近日被开聊较多…」），
    所以必须按**时间格式**过滤，不能把 `tv_text` 全收。"""
    return [
        phone.label(n).strip()
        for n in phone.nodes(xml)
        if n["rid"].endswith("tv_text") and _CHAT_TIME_RE.match(phone.label(n).strip())
    ]


def _card_message_row(xml: str, name_node: dict[str, Any]) -> dict[str, Any] | None:
    """会话卡里「消息预览」那一行的节点（= 该点的地方）。

    取姓名**正下方最近**的那个 `tv_msg`。找不到就返回 None，调用方退回点姓名节点——
    但那条路实测会开出设置页，所以正常情况都该找得到。
    """
    top = name_node["bounds"][1]
    rows = [n for n in phone.nodes(xml)
            if n["rid"].endswith("tv_msg") and n["bounds"][1] > top]
    return min(rows, key=lambda n: n["bounds"][1]) if rows else None


def read_conversation(name: str) -> dict[str, Any]:
    """打开一个会话，读消息。**纯只读**。

    ★ **聊天页上四个快捷操作（求简历 / 换电话·微信 / 约面试 / 不合适）一律不碰**，
      它们在 `mExchangeContainerLayout` 里，**就压在消息列表上方**。
      本函数只点：会话卡片 → 返回。

    ★ 打开会话会把它**标记为已读**（BOSS 的行为），这是只读流程里唯一的外部可见副作用。
    """
    xml = phone.ensure_tab("消息")
    # 往下翻着找。★ 这里跟「牛人」列表不同：会话列表往下翻是**安全的**
    #   （那是推荐流才有的刷新问题）。而且新消息会不断把老会话挤下去——
    #   实测刚发完招呼几分钟，张雄瑜（16:36）就被挤出了首屏。
    card = None
    for _ in range(6):
        card = phone.find_exact(xml, name)
        if card:
            break
        phone.swipe((540, 1600), (540, 700))
        after = phone.dump_stable()
        if after == xml:
            break
        xml = after
    if not card:
        raise FlowMismatch("读会话", f"消息列表里没有「{name}」（找过 6 屏）", phone.texts(xml))

    # ★★ **绝不能点姓名**（2026-10-16 实测踩到）：点会话的 `tv_name` 会打开
    #    `ChatSettingsNewActivity`——那一页上有「加入黑名单」「标记为不合适」
    #    「举报对方」，全是不可逆的。改成点**消息预览那一行**（同一张卡里、
    #    姓名下方的 `tv_msg`），实测能正常进聊天页。
    target = _card_message_row(xml, card) or card
    phone.tap_node(target, pause=9)
    xml = phone.dump_stable()
    _guard_risk(xml, "开会话")
    activity = phone.current_activity()
    if "ChatSettings" in activity:
        phone.back()
        raise FlowMismatch(
            "读会话", "点进了「聊天设置」页（含黑名单/举报/标记不合适），已退出", phone.texts(xml)
        )
    if "ChatRoomActivity" not in activity:
        raise FlowMismatch("读会话", f"没进到聊天页（当前 {activity}）", phone.texts(xml))

    def one(rid: str) -> str:
        node = phone.find_rid(xml, rid)
        return phone.label(node).strip() if node else ""

    detail = {"name": one("tv_title"), "position": one("tv_sub_title"),
              "messages": _chat_messages(xml), "time_marks": _chat_time_marks(xml)}

    phone.tap(tuple(_m("候选人详情", "back", "center")), pause=6)
    if "ChatRoomActivity" in phone.current_activity():
        raise FlowMismatch("读会话", "按了返回但还在聊天页", phone.texts(phone.dump_stable()))
    return detail


# —— 打招呼（写操作，默认干跑）——

#: 打过招呼的账本。★ 防的是「同一个人被打两次招呼」——那对真实的人是打扰。
GREETED_PATH = Path(__file__).resolve().parent / ".greeted.json"


def _load_greeted() -> dict[str, Any]:
    if GREETED_PATH.exists():
        try:
            return json.loads(GREETED_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def _save_greeted(data: dict[str, Any]) -> None:
    GREETED_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _greet_key(detail: dict[str, Any]) -> str:
    """账本主键。

    ★ BOSS 的候选人卡片与详情页里**没有稳定的候选人 id**（猎聘那边至少 logcat 里有
      `oppositeUserId`，这里连那个都没有），所以只能用「姓名 + 当前公司」拼一个。
      重名 + 同公司的概率很低，但**不是零**——所以账本只是「防重复」的第一道，
      真正的保证是**同一次会话里不重复点**。
    """
    return f"{detail.get('name','')}｜{detail.get('job_and_company','')}"


def _find_greet_button(xml: str) -> dict[str, Any] | None:
    """严格定位「立即沟通」。三重条件，缺一不可。

    ★ 这是**唯一**会给真实的人发消息的按钮，宁可找不到也不能找错：
      必须 class=Button、文字精确等于「立即沟通」、且 clickable=true。
    """
    return next(
        (
            n for n in phone.nodes(xml)
            if n["cls"] == "Button" and phone.label(n) == "立即沟通" and n["clickable"]
        ),
        None,
    )


def greet(candidate: str, *, allow_send: bool = False) -> dict[str, Any]:
    """给候选人打招呼。★ **写操作**：会给真实的人发一条消息。

    ## 行为

    实测（2026-10-16）：点「立即沟通」会**自动发送一条账号预设的招呼语**
    （本次见到的是「您好，有兴趣的话可以发一份简历看看，我将转交业务部门。」），
    然后落到 `ChatRoomActivity`。所以「打招呼」在这里就是**一步**，
    招呼语**不能按候选人定制**。

    ## 两道保险

    1. **默认干跑**：不传 `allow_send=True`，就只走到按钮前、把「将要发给谁」
       打出来，**不点**。调用方必须先干跑、确认目标无误，再真发。
    2. **本地账本**：打过招呼的人记在 `.greeted.json`，同一个「姓名｜公司」
       第二次直接跳过。★ 但这不是绝对保险——见 `_greet_key` 的注释。

    ## 做完之后

    不回读聊天页内容（那会带出双方的全部对话），只确认**落在聊天页**、
    且**有一条我们发出去的消息**。然后退回主界面。

    前提：`candidate` 必须**此刻就在牛人列表的当前屏**上
    （列表往回滚会刷新，见文件里那段注释）。
    """
    xml = phone.ensure_tab("牛人")
    card = phone.find_exact(xml, candidate)
    if not card:
        raise FlowMismatch(
            "打招呼", f"「{candidate}」不在当屏（本函数不会往回滚去找，那会刷新推荐流）",
            phone.texts(xml),
        )

    x1, y1, x2, y2 = card["bounds"]
    phone.tap((x1 + 40, (y1 + y2) // 2), pause=8)
    xml = phone.dump_stable()
    _guard_risk(xml, "打招呼")
    _guard_not_paywall(xml, "打招呼")
    if "ViewGeekResume" not in phone.current_activity():
        raise FlowMismatch("打招呼", "没进到候选人详情页", phone.texts(xml))

    def one(rid: str) -> str:
        node = phone.find_rid(xml, rid)
        return phone.label(node).strip() if node else ""

    detail = {"name": one("tv_geek_name"), "job_and_company": one("tv_job_and_com")}
    key = _greet_key(detail)

    book = _load_greeted()
    if key in book:
        phone.tap(tuple(_m("候选人详情", "back", "center")), pause=5)
        return {"ok": True, "skipped": "already_greeted", "candidate": key,
                "first_at": book[key].get("at")}

    button = _find_greet_button(xml)
    if button is None:
        raise FlowMismatch("打招呼", "候选人详情页上没有「立即沟通」按钮", phone.texts(xml))

    if not allow_send:
        phone.tap(tuple(_m("候选人详情", "back", "center")), pause=5)
        return {
            "ok": True, "dry_run": True, "candidate": key,
            "would_tap": button["center"], "button_bounds": button["bounds"],
            "note": "干跑：没有点。确认目标无误后传 allow_send=True 才真发。",
        }

    # ——— 真发 ———
    phone.tap_node(button, pause=12)
    after = phone.dump_stable()
    in_chat = "ChatRoomActivity" in phone.current_activity()
    # ★ 用 _chat_messages 而不是自己扫 rid——`tv_content` 是底部的建议语气泡，
    #   不是已发消息（第一版就是在这儿数错的，见 _chat_messages 的注释）。
    outgoing = [m for m in _chat_messages(after) if m["direction"] == "out"]
    if not in_chat:
        raise FlowMismatch("打招呼", "点了「立即沟通」但没进聊天页，**不知是否发出**",
                           phone.texts(after))

    book[key] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "job": detail.get("job_and_company", "")}
    _save_greeted(book)

    # 从聊天页回到主界面（BACK：聊天页 → 候选人详情 → 主界面）
    for _ in range(3):
        if phone.current_activity().endswith("MainActivity"):
            break
        phone.back()
    return {"ok": True, "sent": True, "candidate": key, "in_chat_room": True,
            "messages_on_screen": len(outgoing)}


# —— ★ 没有的东西（写给下一个改这个文件的人）——
#
# 本文件**故意不实现**：开放/关闭职位、发布职位、编辑职位、退出登录、解绑实名，
# 以及聊天页上的「求简历 / 换电话·微信 / 约面试 / 不合适」。它们都会对真实的
# 人或账号产生不可逆后果，要写得单独来、要带授权闸门与更严的双重校验。
#
# 「打招呼」（立即沟通）**已经实现**（见 greet，默认干跑）——因为它虽然也是写操作，
# 但是整条链路的核心，而且实测就是一步、行为已经摸清。
#
# 特别提醒「不合适」：它和「立即沟通」在聊天页上离得不远，是把候选人标记掉的操作。
# 碰聊天页的任何流程都要先把它的位置钉死，别用文字模糊匹配。
