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
    """切底部 tab，返回切换后的 dump。"""
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

    phone.goto_tab("我的")
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

    phone.goto_tab("我的")
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

def _find_in_list(xml: str, name: str, *, step: str, max_scroll: int = 6) -> dict[str, Any] | None:
    """在候选人列表里找某个人，先**滚回顶部**再往下找。

    ★ 为什么必须滚回顶部：`scan_candidates()` 会把列表**滚下去**，
      紧接着调 `read_candidate(第一名的名字)` 时那人早被滚出屏幕了——
      真机上连着跑必撞（实测：扫到「顾国庆/聂白甜/刘晓尚」，再读「顾国庆」时
      屏幕上已经是「赵川」了，直接 FlowMismatch）。
      列表滚上去是幂等的（到顶就不动了），所以这里放心滚。
    """
    for _ in range(max_scroll):
        phone.swipe((540, 700), (540, 1700))          # 往上翻 = 回到更靠前的条目
    xml = phone.dump_stable()
    card = phone.find_exact(xml, name)
    if card:
        return card
    for _ in range(max_scroll):                        # 还没找到就往下找
        phone.swipe((540, 1600), (540, 700))
        after = phone.dump_stable()
        if after == xml:
            break
        xml = after
        card = phone.find_exact(xml, name)
        if card:
            return card
    return None



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

    xml = goto_tab("牛人")
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
    """读某个候选人的详情页。**纯只读——绝不点「立即沟通」。**

    实测（2026-10-16）详情页是 `BossViewGeekResumePagerActivity`，
    「立即沟通」在底部 `cl_bottom_actions` 里（无 rid，是 Button）。

    ★ 打开会**给候选人一次「被查看」**。这是只读流程里唯一一个有外部可见副作用的动作，
      调用方应该知情。
    """
    xml = goto_tab("牛人")
    card = _find_in_list(xml, name, step="读候选人")
    if not card:
        raise FlowMismatch("读候选人", f"牛人列表里没有「{name}」", phone.texts(xml))

    x1, y1, x2, y2 = card["bounds"]
    # 落点取卡片中部，避开右上角的 iv_close（它是卡片级的 X）
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


# —— ★ 没有的东西（写给下一个改这个文件的人）——
#
# 本文件**故意不实现**：打招呼（立即沟通）、开放/关闭职位、发布职位、编辑职位、
# 退出登录、解绑实名。它们都会对真实的人或账号产生不可逆后果，
# 要写得单独来、要带授权闸门与更严的双重校验（见 phone.find_rid_text 的注释）。
#
# 尤其「立即沟通」：它点下去到底发生什么（直接发消息？进聊天页？有没有默认招呼语？）
# **还没有在真机上验证过**，写之前必须先问清楚，别照着猜。
