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


# ——— 登录态判定（必须与「真的没职位」区分开）———


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
