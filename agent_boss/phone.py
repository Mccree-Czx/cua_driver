"""BOSS直聘执行端的 adb 原语（= 共享层 + BOSS 自己的底部 tab）。

通用实现在 `shared/android_ui.py`，与猎聘的 `agent/phone.py` 共用同一份。
**本文件只补 BOSS 专有的那点东西。**

## ★ 与猎聘最大的不同（写 BOSS 流程前必读）

BOSS 是**原生 Android**，dump 里带完整 `resource-id`
（`com.hpbr.bosszhipin:id/xxx`）。猎聘是 Flutter，几乎没有稳定 ID。
所以：

- **BOSS 一律优先用 `find_rid` / `find_rid_text`**，文字匹配只在没有 rid 的节点上兜底
- 危险按钮（发布 / 开放 / 立即沟通 / 编辑）**必须用 `find_rid_text` 双重校验**，
  因为 BOSS 大量复用 rid：发布表单里四个招聘类型按钮的 rid 全是 `btn_city`

锚点表见 `agent_boss/boss_map.json`。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_SHARED = Path(__file__).resolve().parents[1] / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from android_ui import *  # noqa: F401,F403
from android_ui import (  # noqa: F401
    ADB,
    AdbError,
    adb,
    back,
    clear_field,
    current_activity,
    current_device,
    current_package,
    discover_cloud_phone,
    dump,
    dump_stable,
    edittexts,
    field_at,
    find,
    find_exact,
    find_rid,
    find_rid_text,
    home,
    key,
    label,
    list_devices,
    nodes,
    open_app,
    reconnect,
    risk_markers,
    screen_size,
    set_dump_path,
    shell,
    swipe,
    swipe_v,
    tap,
    tap_node,
    texts,
    type_into,
    use_device,
    ensure_main_tabs as _ensure_main_tabs,
)

PKG = "com.hpbr.bosszhipin"

#: 与猎聘分开，免得两套同时跑时互相覆盖。（两套执行端都用同一部手机上的 /sdcard。）
_DUMP = "/sdcard/boss_dump.xml"
set_dump_path(_DUMP)
# ★ 上面那句改的是 `android_ui.DUMP_PATH`（`dump()` 读的就是它，功能上已生效）；
#   但 `from android_ui import *` 早把旧值**复制**进了本模块的命名空间，
#   不重新绑一下的话 `phone.DUMP_PATH` 会是个骗人的旧值。
DUMP_PATH = _DUMP

# ——— BOSS 专有 ———

#: BOSS 的底部导航。★ **不能按文字找**——`tv_tab_N` 的 bounds 是 `[0,0][0,0]`，
#: 而 `nodes()` 会跳过零尺寸节点。只能按可点容器 `cl_tab_N` 的 rid 找。
MAIN_TAB_RIDS = ("cl_tab_1", "cl_tab_2", "cl_tab_3", "cl_tab_4")

#: 底部四个 tab 的名字（顺序即 cl_tab_N 的 rid 顺序）。
TAB_NAMES = ("牛人", "搜索", "消息", "我的")

#: tab 名 → 可点容器的 rid。
#: ★★ 坐标**不再写死**（2026-10-19）：原来这里存的是真机 1080x2244 下
#:    `cl_tab_N` 的实测中心（y=2126），而云机是 720x1280 —— 那个 y 在云机上
#:    直接点出屏幕。rid 是稳定的、坐标不是，所以改成现场 dump 一次再取 bounds 中心。
_TAB_RID = {
    "牛人": "cl_tab_1",
    "搜索": "cl_tab_2",
    "消息": "cl_tab_3",
    "我的": "cl_tab_4",
}


def _locate_tabs(xml: str) -> list[str]:
    return [rid for rid in MAIN_TAB_RIDS if find_rid(xml, rid)]


def ensure_main_tabs(package: str = PKG, max_back: int = 5, settle: float = 6.0) -> dict:
    """退到 BOSS 的主界面（按 rid 判定，理由见 MAIN_TAB_RIDS 的注释）。"""
    return _ensure_main_tabs(package, max_back=max_back, settle=settle, locate=_locate_tabs)


def goto_tab(name: str, settle: float = 7.0) -> None:
    """切底部 tab。★ 点的是 `cl_tab_N` 容器，不是文字节点（后者尺寸为 0）。

    ★★ 坐标现场取（2026-10-19）：rid 稳定、坐标不稳定——真机 1080x2244 的
      y=2126 在云机 720x1280 上会点到屏幕外。所以先 dump、按 rid 找 bounds 中心。
    """
    node = find_rid(dump_stable(), _TAB_RID[name])
    if node is None:
        raise RuntimeError(f"当前屏上找不到底部 tab「{name}」（{_TAB_RID[name]}）")
    tap_node(node, pause=settle)


def active_tab(xml: str) -> str | None:
    """当前选中哪个底部 tab（`牛人`/`搜索`/`消息`/`我的`），识别不出返回 None。

    ★ 为什么不能用 `nodes()` 找：`selected="true"` 挂在 `tv_tab_N` 上，
      而它的 bounds 是 `[0,0][0,0]`（标签被隐藏），`nodes()` 会跳过零尺寸节点。
      所以只能直接在原始 XML 上正则。
    """
    for raw in re.findall(r"<node\b[^>]*>", xml):
        if 'selected="true"' not in raw:
            continue
        rid = re.search(r'resource-id="([^"]*)"', raw)
        if not rid:
            continue
        short = rid.group(1).rsplit("/", 1)[-1]
        if short in _TAB_BY_RID:
            return _TAB_BY_RID[short]
    return None


#: ★ 两份 rid 都要认：可点的容器是 `cl_tab_N`，但 `selected="true"` 挂在
#: **`tv_tab_N`** 上（实测）。只写 cl_tab_N 的话 active_tab() 永远返回 None，
#: 于是 ensure_tab() 每次都点一下——正是它要避免的那个刷新。
_TAB_BY_RID = {
    "cl_tab_1": "牛人", "tv_tab_1": "牛人",
    "cl_tab_2": "搜索", "tv_tab_2": "搜索",
    "cl_tab_3": "消息", "tv_tab_3": "消息",
    "cl_tab_4": "我的", "tv_tab_4": "我的",
}


def ensure_tab(name: str, settle: float = 7.0) -> str:
    """确保停在某个底部 tab，返回那一屏的 dump。**已经在该 tab 就不点。**

    ★★ 必须这样（2026-10-16 实测代价）：**点已经激活的 tab 会刷新那一页**——
      「牛人」列表上实测：点一下当前 tab，`郭鑫` 变成 `郭宁`，精选计数从 10 跳到 14。
      跟「往回滚」是同一类坑：**任何多余的交互都会让推荐流换一批人**。
      所以「先拿姓名、再调一个会自己导航的函数」这种写法必然踩雷，
      凡是操作列表里某个具体对象的流程，都必须**先确认已在目标 tab**，
      在同一个 dump 里拿到目标再动手。
    """
    current = active_tab(dump_stable())
    if current != name:
        goto_tab(name, settle=settle)
    return dump_stable()


#: BOSS 上「点一下就有真实后果」的文字。**任何自动化流程在点之前都该拿它自检。**
RISK_TEXTS = (
    "发布", "提交", "保存", "完成", "确认",            # 发布职位
    "开放职位", "立即开启",                            # 开放（后者还会扣直豆）
    "立即沟通", "打招呼", "开聊",                      # 给候选人发消息
    "编辑", "关闭职位", "下架", "删除",                # 改/撤在跑的职位
    "解绑实名", "注销", "退出登录",
)


#: 按钮标签的长度上限。★ 这个上限是**被误报逼出来的**（2026-10-16）：
#: 只做子串匹配的话，候选人自我介绍里那句「独立**完成**场景化智能方案设计」会命中
#: RISK_TEXTS 里的「完成」，于是几百字的简历正文被当成危险按钮弹了出来。
#: 按钮标签都很短，用长度把它和正文分开。
DANGER_LABEL_MAX = 20


def danger_nodes(xml: str) -> list[dict]:
    """屏上所有「点了有真实后果」的节点。探查工具与写流程都拿它做自检。

    ★ 双重过滤，缺一不可：
      1. `clickable == true`（按钮的可点容器）
      2. 文字长度 ≤ `DANGER_LABEL_MAX`（把正文排除掉——见上面那段注释）
    """
    out = []
    for node in nodes(xml):
        text = label(node)
        if not text or len(text) > DANGER_LABEL_MAX:
            continue
        if not node["clickable"]:
            continue
        if any(risk in text for risk in RISK_TEXTS):
            out.append(node)
    return out
