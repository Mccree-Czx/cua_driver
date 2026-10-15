"""职位扫描的解析与护栏测试（2026-10-15）。

`agent/flows.py::scan_jobs()` 是唯一会去读 App 职位列表的代码。它错的方式都很贵：

- 解析不出内容 → 报「0 个职位」→ 系统静默空转（和「这个号真没职位」长得一样）
- 误点职位卡 → 进真实候选人的数据页，那屏上有「不合适」「看简历」
- 误入「发职位」表单并提交 → **真的对外发布一个职位**

所以这里用**固件 XML**（照 2026-10-11 实测的卡片格式手写）把解析和护栏钉死，
全程不碰手机。
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "agent"))

import flows  # noqa: E402
import phone  # noqa: E402

# 照实测格式：content-desc 第一行职位名，第二行「地点 学历 经验 薪资」。
# Android 的 dump 里换行是 &#10;，phone.nodes() 会 unescape 回真换行。
JOB_PAGE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" package="com.lietou.mishu" bounds="[0,0][1080,2244]">
    <node index="1" text="招聘中 2" class="android.widget.Button" bounds="[0,217][237,337]" clickable="true"/>
    <node index="2" text="待发布 0" class="android.widget.Button" bounds="[237,217][471,337]" clickable="true"/>
    <node index="3" text="发职位" class="android.widget.Button" bounds="[810,109][1044,193]" clickable="true"/>
    <node index="4" text="" content-desc="机械结构工程师&#10;上海-黄浦区 本科 5年以上 25-45k" class="android.view.View" bounds="[0,337][1080,930]" clickable="true"/>
    <node index="5" text="" content-desc="海外ToB渠道销售(出海品牌)&#10;上海-浦东新区 本科 3-5年 20-35k&#183;14薪" class="android.view.View" bounds="[0,930][1080,1523]" clickable="true"/>
    <node index="6" text="" content-desc="看过我&#10;12" class="android.view.View" bounds="[0,950][300,1000]"/>
    <node index="7" text="" content-desc="沟通过&#10;3" class="android.view.View" bounds="[300,950][600,1000]"/>
    <node index="8" text="" class="android.widget.Button" bounds="[0,2085][270,2159]"/>
    <node index="9" text="职位" class="android.widget.Button" bounds="[540,2085][810,2159]" clickable="true"/>
    <node index="10" text="我的" class="android.widget.Button" bounds="[810,2085][1080,2159]" clickable="true"/>
  </node>
</hierarchy>
"""

PUBLISH_FORM_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" package="com.lietou.mishu" bounds="[0,0][1080,2244]">
    <node index="1" text="" content-desc="职位描述&#10;AI帮写&#10;请填写职位描述" class="android.view.View" bounds="[0,1465][1080,1722]"/>
    <node index="2" text="发布职位" class="android.widget.Button" bounds="[540,2000][900,2100]" clickable="true"/>
  </node>
</hierarchy>
"""

ONBOARDING_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" package="com.lietou.mishu" bounds="[0,0][1080,2244]">
    <node index="1" text="创建招聘名片" class="android.view.View" bounds="[0,300][1080,500]"/>
    <node index="2" text="更多操作" class="android.view.View" bounds="[0,1300][300,1380]" clickable="true"/>
  </node>
</hierarchy>
"""

LOGIN_PAGE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" package="com.lietou.mishu" bounds="[0,0][1080,2244]">
    <node index="1" text="手机号登录/注册" class="android.view.View" bounds="[0,1000][1080,1100]"/>
    <node index="2" text="下一步" class="android.widget.Button" bounds="[0,1250][1080,1320]" clickable="true"/>
  </node>
</hierarchy>
"""


def _stub_screen(monkeypatch, xml: str) -> None:
    monkeypatch.setattr(phone, "home", lambda *a, **k: None)
    monkeypatch.setattr(phone, "open_app", lambda *a, **k: None)
    monkeypatch.setattr(phone, "dump", lambda: xml)


# ——— 卡片解析 ———


def test_collects_only_real_job_cards():
    """两张职位卡都收进来；「看过我/沟通过」这类计数行不是职位。"""
    seen: dict = {}
    assert flows._collect_job_cards(JOB_PAGE_XML, seen) == 2
    assert seen["机械结构工程师"]["details"] == "上海-黄浦区 本科 5年以上 25-45k"
    assert seen["海外ToB渠道销售(出海品牌)"]["details"] == "上海-浦东新区 本科 3-5年 20-35k·14薪"


def test_dedupes_across_screens():
    """同一次扫描里重复出现的卡片只算一次（滑动翻页时必然重叠）。"""
    seen: dict = {}
    flows._collect_job_cards(JOB_PAGE_XML, seen)
    assert flows._collect_job_cards(JOB_PAGE_XML, seen) == 0
    assert len(seen) == 2


def test_recruiting_tab_has_a_count_suffix():
    """★ 那个 tab 的文字实测是「招聘中 N」——`find_exact` 匹配不上，必须用前缀。"""
    assert phone.find_exact(JOB_PAGE_XML, "招聘中") is None
    node = flows._find_startswith(JOB_PAGE_XML, "招聘中")
    assert node is not None and node["text"] == "招聘中 2"


# ——— 护栏 ———


def test_publish_form_guard_backs_out_and_raises(monkeypatch):
    """★ 误入新建表单必须立刻退出——提交了就是真的对外发布一个职位。"""
    pressed: list[str] = []
    monkeypatch.setattr(phone, "back", lambda: pressed.append("BACK"))

    with pytest.raises(flows.FlowMismatch) as excinfo:
        flows._guard_publish_form(PUBLISH_FORM_XML)

    assert pressed == ["BACK"]
    assert "发职位" in str(excinfo.value)


def test_publish_form_guard_is_quiet_on_normal_page():
    flows._guard_publish_form(JOB_PAGE_XML)  # 不该抛


# ——— 导航到职位列表 ———


def test_goto_job_list_does_not_tap_the_tab_when_already_there(monkeypatch):
    """★ 已经在「招聘中」列表上，就**一次都别点**底部「职位」tab。

    猎聘的「职位」tab 是「回到该 tab **上次那页**」，不是「回到 tab 根」。
    2026-10-15 真机复现：`ensure_main_tabs()` 已经把手机带到了「招聘中」列表，
    脚本又去点了一下 tab —— 那一下把它送回了 Pi 上一轮留在那里的
    「某个职位的候选人页」，于是判定「没进到职位管理页」，抓 JD 整段失败。

    这条测试钉死的是「**不要多此一举地点击**」。
    """
    taps: list = []
    monkeypatch.setattr(
        phone, "ensure_main_tabs", lambda pkg, **kw: {"ok": True, "marks": ["推荐", "职位", "我的"]}
    )
    monkeypatch.setattr(phone, "dump", lambda: JOB_PAGE_XML)
    monkeypatch.setattr(phone, "tap_node", lambda node, **kw: taps.append(node["center"]))
    monkeypatch.setattr(phone, "tap", lambda *a, **k: taps.append(a))
    monkeypatch.setattr(phone, "back", lambda: taps.append("BACK"))

    flows._goto_job_list("测试")

    assert "BACK" not in taps, "已经在列表上了还按了 BACK"
    assert (675, 2122) not in taps, "已经在列表上了还点了底部「职位」tab"
    # 唯一该点的是「招聘中」筛选 tab
    assert taps == [(118, 277)]


def test_goto_job_list_backs_out_of_a_remembered_subpage(monkeypatch):
    """不在列表上时：点 tab → 还不在 → BACK 一层 → 直到回到列表。"""
    screens = iter([SUBPAGE_XML, SUBPAGE_XML, JOB_PAGE_XML])
    taps: list = []
    monkeypatch.setattr(
        phone, "ensure_main_tabs", lambda pkg, **kw: {"ok": True, "marks": ["推荐", "职位", "我的"]}
    )
    monkeypatch.setattr(phone, "dump", lambda: next(screens, JOB_PAGE_XML))
    monkeypatch.setattr(phone, "tap_node", lambda node, **kw: taps.append(node["center"]))
    monkeypatch.setattr(phone, "tap", lambda *a, **k: taps.append(a))
    monkeypatch.setattr(phone, "back", lambda: taps.append("BACK"))

    flows._goto_job_list("测试")

    assert "BACK" in taps, "没从记住的子页退出来"
    assert (118, 277) in taps, "最后没点「招聘中」"


# ——— 登录态判定（必须与「真的没职位」区分开）———


# ——— JD（照 2026-10-15 真机实测的职位详情页结构手写）———

JD_BODY = (
    "·截止招聘日期：2027年09月17日\n·招聘人数：1人\n\n【岗位职责】\n"
    "1.负责从需求分析、方案设计到量产导入的全流程硬件工作，主导关键元器件选型、认证与成本控制。\n"
    "2.完成原理图设计、PCB Layout评审与优化。\n\n【任职要求】\n"
    "1.本科及以上学历，电子类相关专业，3年以上消费电子产品的完整硬件开发经验。"
)

# ★ 标题节点里**故意**用与 JD 正文相同的职位名——用来验证 _page_is_for 只看上半页
_DETAIL_TMPL = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" package="com.lietou.mishu" bounds="[0,0][1080,2244]">
    <node index="1" text="高级硬件工程师" class="android.view.View" bounds="[84,290][640,373]"/>
    <node index="2" text="30-50k&#183;13薪" class="android.view.View" bounds="[700,290][996,356]"/>
    <node index="3" text="职位介绍" class="android.view.View" bounds="[84,1428][300,1503]"/>
    <node index="4" text="" content-desc="{jd}" class="android.view.View" bounds="[84,1539][996,2040]"/>
    <node index="5" text="关闭职位" class="android.widget.Button" bounds="[48,2064][426,2159]" clickable="true"/>
    <node index="6" text="编辑职位" class="android.widget.Button" bounds="[462,2064][1031,2159]" clickable="true"/>
  </node>
</hierarchy>
"""


# 不是职位管理页、也没有「职位介绍」——模拟猎聘「职位」tab 记住的候选人子页
SUBPAGE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" package="com.lietou.mishu" bounds="[0,0][1080,2244]">
    <node index="1" text="资深亚马逊运营" class="android.view.View" bounds="[0,100][1080,200]"/>
    <node index="2" text="看过我 78&#10;第 1 个标签，共 3 个" class="android.view.View" bounds="[0,250][360,400]"/>
    <node index="3" text="推荐" class="android.widget.Button" bounds="[0,2085][270,2159]"/>
    <node index="4" text="职位" class="android.widget.Button" bounds="[540,2085][810,2159]" clickable="true"/>
    <node index="5" text="我的" class="android.widget.Button" bounds="[810,2085][1080,2159]"/>
  </node>
</hierarchy>
"""


def _detail_xml(jd: str) -> str:
    """详情页固件。`jd` 直接放进 content-desc——**变长必须变在这里**，
    加在 XML 注释里是没用的（解析取的是节点文字，不是原始 XML）。"""
    return _DETAIL_TMPL.replace("{jd}", jd.replace("\n", "&#10;"))


DETAIL_XML = _detail_xml(JD_BODY)
DETAIL_XML_SHORT = _detail_xml(JD_BODY[:20])


def test_jd_from_screen_picks_the_node_under_the_label():
    """「职位介绍」下方那个节点就是 JD——一次给全 543 字（实测）。"""
    got = flows._jd_from_screen(DETAIL_XML)
    assert got == JD_BODY
    assert "【任职要求】" in got


def test_page_is_for_ignores_the_jd_body():
    """★ 身份核对只看页面上半部分。

    JD 正文里**会**出现职位名（这个 fixture 就是照实写的），不限定区域的话，
    随便打开哪个职位都能「核对通过」，身份核对就白做了。
    """
    assert flows._page_is_for(DETAIL_XML, "高级硬件工程师") is True
    assert flows._page_is_for(DETAIL_XML, "资深亚马逊运营") is False


def test_extract_jd_detects_truncation_by_scrolling(monkeypatch):
    """★ 滚动后文本变长 = 刚才那份是截断的 → complete 必须是 False。

    「半截 JD」比「空 JD」更危险：它看起来像有依据，LLM 会拿它当真去筛真人。
    所以只有「滚了也不再变长」才敢说读全了。
    """
    # 第一屏短 → 滚一次变长 → 再滚不再变：这才是「一开始被截断了」
    screens = iter([DETAIL_XML_SHORT, DETAIL_XML, DETAIL_XML])
    monkeypatch.setattr(phone, "dump", lambda: next(screens))
    monkeypatch.setattr(phone, "swipe", lambda *a, **k: None)

    text, complete = flows._extract_jd()
    assert complete is True
    assert text == JD_BODY


def test_extract_jd_is_incomplete_when_never_settles(monkeypatch):
    """每滚一屏都更长 → 到上限就停，且**不许说 complete**（宁可不启用）。"""
    screens = iter([
        _detail_xml(JD_BODY[:20]),
        _detail_xml(JD_BODY[:40]),
        _detail_xml(JD_BODY[:60]),
        _detail_xml(JD_BODY[:80]),
    ])
    monkeypatch.setattr(phone, "dump", lambda: next(screens))
    monkeypatch.setattr(phone, "swipe", lambda *a, **k: None)

    _text, complete = flows._extract_jd()
    assert complete is False


def test_card_tap_point_avoids_the_boost_button_and_counters():
    """★ 落点必须避开右上角「火爆刷」付费推广按钮和下部三个计数按钮。

    实测卡片 bounds [0,448][1080,906]，火爆刷在 x 786~996 / y 526~580，
    计数按钮在 y 735~858。
    """
    x, y = flows._card_tap_point((0, 448, 1080, 906))
    assert not (786 <= x <= 996 and 526 <= y <= 580), "落在火爆刷上了"
    assert not (735 <= y <= 858), "落在计数按钮那一行了"
    assert 0 <= x <= 1080 and 448 <= y <= 906, "落在卡片外面了"


@pytest.mark.parametrize(
    ("xml", "reason"),
    [(ONBOARDING_XML, "onboarding"), (LOGIN_PAGE_XML, "not_logged_in")],
)
def test_scan_reports_why_it_could_not_scan(monkeypatch, xml, reason):
    """★ 新号登录后落在「创建招聘名片」引导页，那时它**还没有职位管理页**。

    不区分的话会把新号报成「0 个在招职位」，而系统随后静默空转——
    和「这个号确实没职位」长得一模一样，没人能看出区别。
    """
    _stub_screen(monkeypatch, xml)
    result = flows.scan_jobs()
    assert result["ok"] is False
    assert result["reason"] == reason
    assert result["jobs"] == []
