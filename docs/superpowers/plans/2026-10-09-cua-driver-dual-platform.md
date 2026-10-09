# CUA 驱动双平台支持 实施计划（阶段 1）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `CuaLiepinDriver` 的平台相关逻辑抽到 `PlatformAdapter` 接口后，使其在 Windows 上可用（阶段 1 完成到 `check_login()` 真机返回 `True`）。

**Architecture:** 新增 `app/drivers/platform/` 子包：`base.py` 定义 `Role` 枚举、`WindowRef` 与 `PlatformAdapter` 协议；`macos.py` 把现有 macOS 原语机械搬移；`windows.py` 新写 Windows 原语（SDK `ClickPosition.COORDINATES` 取代 osascript，`chrome.exe`/`msedge.exe` 取代 macOS 应用名）。`CuaLiepinDriver` 保留全部流程编排，平台相关调用收敛为 `self._plat.*`，对外契约不变。

**Tech Stack:** Python 3.12 / uv workspace / pytest / `cua-driver` 0.30.4（PyPI，进程内 Rust runtime）

**Spec:** `docs/superpowers/specs/2026-10-09-cua-driver-dual-platform-design.md`

## Global Constraints

- 所有测试命令必须在 `services/cua-agent/` 目录内执行：`cd services/cua-agent && uv run pytest tests -q`（仓库根跑 pytest 有跨服务收集冲突，属既有问题）。
- 装依赖用 `uv sync --all-packages`（裸 `uv sync` 会把 `.venv` 修剪到仅根 dev 组）。
- `CuaLiepinDriver` 的**类名、无参构造签名、对外方法集**（协议 9 方法 + `capture_desktop_png` / `click_text_contains` / `click_at_screenshot_px`）保持不变 —— worker / executor / fallback 不得需要改动。
- macOS 路径**纯机械搬移，无行为变更**；相关 commit message 标注 `refactor: 机械搬移，无行为变更`。
- macOS 机器不可得，搬移后**无法实盘回归** —— 这是已知且被接受的敞口（spec §6/§10）。
- 真实账号有风控历史（两次「账号行为异常」）。阶段 1 只跑**只读**的 `check_login`，不触碰任何候选人；写操作属阶段 2/3。

## Review Focus

以下几类输入/失败模式由 spec 隐含、但不属于任何任务的正面测试路径，最可能在实际使用中咬人。每条都已在下方指定任务中补了对应测试：

1. **多标签窗口**：Chrome 开着一个非猎聘标签且它不是活动标签时，`_probe_liepin` 探不到 URL；若标签切换失败，`check_login()` 会**误报未登录**（真实账号已登录却判 False）。→ Task 3
2. **浏览器进程名变体**：Windows 上可能是 `chrome.exe` / `Chrome.exe`，或同名多进程；过滤过严会漏掉真正的猎聘窗口。→ Task 4
3. **失效 pid / 已关闭窗口**：`is_on_screen()` 遇到已消失的 pid 应返回 `False`，不应抛异常中断整个登录检测。→ Task 4
4. **`sys.platform` 非预期值**（WSL、未知平台）：`create_adapter()` 应明确抛错，不得静默选到错误的平台实现。→ Task 5
5. **DPI 缩放误判**：`screenshot_px_per_point` 若沿用 macOS 的 `2.0`，LLM 兜底的截图像素坐标点击会系统性地打偏。→ Task 4

---

### Task 1: 平台基础类型与角色映射

**Files:**
- Create: `services/cua-agent/app/drivers/platform/__init__.py`
- Create: `services/cua-agent/app/drivers/platform/base.py`
- Create: `services/cua-agent/app/drivers/platform/macos.py`（本任务只含角色映射）
- Create: `services/cua-agent/app/drivers/platform/windows.py`（本任务只含角色映射）
- Test: `services/cua-agent/tests/test_platform_roles.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `Role`（`StrEnum`）：`TEXT` `BUTTON` `RADIO` `CHECKBOX` `IMAGE` `LINK` `TEXT_INPUT` `TEXT_AREA` `WEB_AREA`
  - `WindowRef`（`dataclass(frozen=True)`）：`pid: int`、`window_id: int`、`app_name: str`、`title: str`、`is_on_screen: bool`
  - `PlatformAdapter`（`Protocol`）：`name: str`、`screenshot_px_per_point: float`、`candidate_windows(windows) -> list[WindowRef]`、`url_of(state) -> str`、`is_on_screen(windows, pid, window_id) -> bool`、`activate(windows, pid) -> None`、`raise_window(pid, window_id) -> bool`、`role_name(role: Role) -> str`、`click_point(pid, window_id, x, y) -> None`、`switch_to_first_tab() -> None`
  - `MACOS_ROLE_MAP: dict[Role, str]`、`WINDOWS_ROLE_MAP: dict[Role, str]`

- [ ] **Step 1: 写失败测试**

```python
# services/cua-agent/tests/test_platform_roles.py
"""角色映射表：把平台无关 Role 翻成各平台的树角色名。"""

from app.drivers.platform.base import Role
from app.drivers.platform.macos import MACOS_ROLE_MAP
from app.drivers.platform.windows import WINDOWS_ROLE_MAP


def test_role_enum_is_complete():
    assert {r.name for r in Role} == {
        "TEXT", "BUTTON", "RADIO", "CHECKBOX", "IMAGE",
        "LINK", "TEXT_INPUT", "TEXT_AREA", "WEB_AREA",
    }


def test_macos_map_covers_every_role():
    assert set(MACOS_ROLE_MAP) == set(Role)


def test_macos_map_matches_verified_ax_names():
    """取值来自 cua_sdk.py 中已在真实页面校准过的角色名。"""
    assert MACOS_ROLE_MAP[Role.TEXT] == "AXStaticText"
    assert MACOS_ROLE_MAP[Role.BUTTON] == "AXButton"
    assert MACOS_ROLE_MAP[Role.RADIO] == "AXRadioButton"
    assert MACOS_ROLE_MAP[Role.CHECKBOX] == "AXCheckBox"
    assert MACOS_ROLE_MAP[Role.IMAGE] == "AXImage"
    assert MACOS_ROLE_MAP[Role.LINK] == "AXLink"
    assert MACOS_ROLE_MAP[Role.TEXT_INPUT] == "AXTextField"
    assert MACOS_ROLE_MAP[Role.TEXT_AREA] == "AXTextArea"
    assert MACOS_ROLE_MAP[Role.WEB_AREA] == "AXWebArea"


def test_windows_map_covers_every_role():
    assert set(WINDOWS_ROLE_MAP) == set(Role)


def test_windows_map_uses_uia_control_types():
    """UIA 标准 ControlType 名；阶段 2 校准时以真实树为准修正。"""
    assert WINDOWS_ROLE_MAP[Role.BUTTON] == "Button"
    assert WINDOWS_ROLE_MAP[Role.TEXT_INPUT] == "Edit"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/cua-agent && uv run pytest tests/test_platform_roles.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.drivers.platform'`

- [ ] **Step 3: 实现 `Role` / `WindowRef` / `PlatformAdapter` 于 `platform/base.py`**

`Role` 用 `StrEnum`（`from enum import StrEnum`）。`WindowRef` 用 `@dataclass(frozen=True)`，字段按 Interfaces 顺序。`PlatformAdapter` 用 `typing.Protocol`，每个方法只有签名与 `...` 体；`windows` / `elements` / `state` 参数类型标 `Sequence[Any]` / `Any`（元素对象是 SDK 的 `WindowElement` / `WindowStateOutput`，不做包装）。

- [ ] **Step 4: 实现两个映射表**

`macos.py` 与 `windows.py` 各自定义 `*_ROLE_MAP`（取值见测试）。macOS 取值照抄 `cua_sdk.py` 里已在真实页面校准过的角色名；Windows 取值用 UIA 标准 ControlType 名（`Text` / `Button` / `RadioButton` / `CheckBox` / `Image` / `Hyperlink` / `Edit` / `Document`；`TEXT_AREA` 与 `TEXT_INPUT` 都映射 `Edit`，`WEB_AREA` 映射 `Document`）。文件头加一行注释说明 Windows 取值依据是 UIA 规范、待阶段 2 校准验证。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd services/cua-agent && uv run pytest tests/test_platform_roles.py -q`
Expected: PASS（5 passed）

- [ ] **Step 6: Commit**

```bash
git add services/cua-agent/app/drivers/platform services/cua-agent/tests/test_platform_roles.py
git commit -m "feat(cua-agent): 平台抽象基础——Role/WindowRef/PlatformAdapter 协议与双平台角色映射"
```

---

### Task 2: 抽出 `MacOsAdapter` 并让 `CuaLiepinDriver` 走 adapter

> 这是一个**重构任务**（纯搬移 + 接线），不适用"先写失败测试"；它的回归网是现有的 90 个 cua-agent 测试。

**Files:**
- Modify: `services/cua-agent/app/drivers/platform/macos.py`（补 `MacOsAdapter` 实现）
- Modify: `services/cua-agent/app/drivers/cua_sdk.py`（平台相关调用改为 `self._plat.*`）

**Interfaces:**
- Consumes: Task 1 的 `Role` / `WindowRef` / `PlatformAdapter` / `MACOS_ROLE_MAP`
- Produces: `MacOsAdapter`（满足 `PlatformAdapter`），供 Task 5 的 `create_adapter()` 使用

- [ ] **Step 1: 记录基线**

Run: `cd services/cua-agent && uv run pytest tests -q`
Expected: `90 passed`（记录实际数字，作为重构后的比对基线）

- [ ] **Step 2: 把 macOS 原语机械搬进 `MacOsAdapter`**

搬移这些方法到 `macos.py`（**逻辑一字不改**，只改 `self._driver` 之类的依赖为构造注入）：
`_browser_windows` / `BROWSER_APPS` / `_activate_browser` / `_click_point` / `_hotkey_tab_1` / `_raise_via_window_menu` / `_is_window_on_screen` / `_browser_windows_all` / `_current_url` / `ensure_visible` 中的平台部分。

`MacOsAdapter.__init__(self, bridge)` 接收 `_RuntimeBridge`（点击/热键/菜单经 SDK 的 `call_tool` 时要用）。`screenshot_px_per_point = 2.0`。`role_name(role)` 查 `MACOS_ROLE_MAP`。

- [ ] **Step 3: 改造 `cua_sdk.py` 接线**

`CuaLiepinDriver.__init__` 增加 `self._plat = MacOsAdapter(self._bridge)`（阶段 1 先硬编码，Task 5 换成 `create_adapter()`）。把 `cua_sdk.py` 中所有平台相关调用原地替换为 adapter 调用，例如 `self._browser_windows()` → `self._plat.candidate_windows(self._browser_windows_all())`、`role="AXStaticText"` → `role=self._plat.role_name(Role.TEXT)`、`self._click_point(...)` → `self._plat.click_point(...)`。

`_find` 的 `role` 参数类型改为 `Role | None`，内部再翻成平台角色名比对。

- [ ] **Step 4: 跑测试确认无回归**

Run: `cd services/cua-agent && uv run pytest tests -q`
Expected: 与 Step 1 相同的通过数，0 failed

- [ ] **Step 5: 审查 diff 只含移动**

Run: `git diff --stat` 与 `git diff services/cua-agent/app/drivers/cua_sdk.py`
Expected: 删除行与新增行内容对应（搬移），`cua_sdk.py` 中不出现新的业务逻辑分支

- [ ] **Step 6: Commit**

```bash
git add services/cua-agent/app/drivers/platform/macos.py services/cua-agent/app/drivers/cua_sdk.py
git commit -m "refactor(cua-agent): 机械搬移 macOS 原语至 MacOsAdapter，CuaLiepinDriver 改走平台接口

无行为变更。mac 环境不可得，待将来实盘回归。"
```

---

### Task 3: 假 adapter 流程测试（补测试网）

**Files:**
- Test: `services/cua-agent/tests/test_flow_with_fake_adapter.py`

**Interfaces:**
- Consumes: Task 1 的 `PlatformAdapter` / `Role` / `WindowRef`；Task 2 改造后的 `CuaLiepinDriver`
- Produces: `FakePlatformAdapter`（测试内定义，供后续任务复用同一套伪造思路）

- [ ] **Step 1: 写失败测试**

```python
# services/cua-agent/tests/test_flow_with_fake_adapter.py
"""用假 adapter 驱动流程编排：让「找窗口→读树→判定」首次可单测。"""

from app.drivers.platform.base import Role, WindowRef


def test_check_login_true_when_backend_markers_present(fake_driver_with_tree):
    driver = fake_driver_with_tree(tree_text="人才推荐 搜索人才 职位管理")
    assert driver.check_login() is True


def test_check_login_false_on_login_page_markers(fake_driver_with_tree):
    driver = fake_driver_with_tree(tree_text="扫码登录 密码登录")
    assert driver.check_login() is False


def test_check_login_false_when_no_url_marker(fake_driver_with_tree):
    """多标签窗口：第一个窗口探不到 liepin.com → 不应误判为已登录。"""
    driver = fake_driver_with_tree(tree_text="人才推荐", url_marker_present=False)
    assert driver.check_login() is False


```

（`fake_driver_with_tree` 是 conftest fixture：构造一个 `CuaLiepinDriver`，把 `self._plat` 换成返回固定树/URL 的假 adapter，并打桩掉 SDK 桥。窗口过滤的用例见 Task 4。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/cua-agent && uv run pytest tests/test_flow_with_fake_adapter.py -q`
Expected: FAIL — fixture `fake_driver_with_tree` 未定义 / `WindowsAdapter` 不存在

- [ ] **Step 3: 实现 fixture 与 `_win` 助手**

在 `tests/conftest.py` 或测试文件内实现 `fake_driver_with_tree`：构造驱动对象后**直接替换** `driver._plat`（不启动 SDK runtime），假 adapter 的 `candidate_windows` 返回固定 `WindowRef`、`url_of` 按 `url_marker_present` 返回含或不含 `liepin.com` 的串、其余方法为空实现记录调用。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd services/cua-agent && uv run pytest tests/test_flow_with_fake_adapter.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 跑全量确认无回归**

Run: `cd services/cua-agent && uv run pytest tests -q`
Expected: 原基线 + 3，0 failed

- [ ] **Step 6: Commit**

```bash
git add services/cua-agent/tests/test_flow_with_fake_adapter.py services/cua-agent/tests/conftest.py
git commit -m "test(cua-agent): 假 adapter 驱动流程编排，补上真实驱动的测试网"
```

---

### Task 4: `WindowsAdapter`

**Files:**
- Modify: `services/cua-agent/app/drivers/platform/windows.py`（补 `WindowsAdapter` 实现）
- Test: `services/cua-agent/tests/test_platform_roles.py`（追加窗口过滤与健壮性用例）

**Interfaces:**
- Consumes: Task 1 的 `Role` / `WindowRef` / `PlatformAdapter` / `WINDOWS_ROLE_MAP`
- Produces: `WindowsAdapter`（满足 `PlatformAdapter`），供 Task 5 的 `create_adapter()` 使用

- [ ] **Step 1: 追加失败测试**

```python
def _win(app_name: str, pid: int):
    """构造 SDK WindowInfo 形状的最小对象。"""
    from types import SimpleNamespace
    return SimpleNamespace(app_name=app_name, pid=pid, window_id=pid,
                           title="", is_on_screen=True)


def test_candidate_windows_matches_process_name_variants():
    """chrome.exe / Chrome.exe 都应命中；无关进程不命中。"""
    from app.drivers.platform.windows import WindowsAdapter
    adapter = WindowsAdapter(bridge=None)
    windows = [
        _win("Chrome.exe", pid=1), _win("chrome.exe", pid=2),
        _win("Feishu.exe", pid=3), _win("msedge.exe", pid=4),
    ]
    assert [w.pid for w in adapter.candidate_windows(windows)] == [1, 2, 4]


def test_windows_is_on_screen_false_for_unknown_pid():
    """已关闭窗口/失效 pid：应返回 False，不得抛异常。"""
    from app.drivers.platform.windows import WindowsAdapter
    assert WindowsAdapter(bridge=None).is_on_screen([], pid=999999, window_id=1) is False


def test_windows_screenshot_scale_is_not_retina_default():
    """不得沿用 macOS 的 2.0 —— 否则兜底坐标点击系统性打偏。"""
    from app.drivers.platform.windows import WindowsAdapter
    scale = WindowsAdapter(bridge=None).screenshot_px_per_point
    assert scale > 0 and scale <= 4
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/cua-agent && uv run pytest tests/test_platform_roles.py -q -k windows`
Expected: FAIL — `WindowsAdapter` 不存在

- [ ] **Step 3: 实现 `WindowsAdapter`**

- `candidate_windows(windows)`：按**进程名**过滤，`app_name.lower()` 属 `{"chrome.exe", "msedge.exe", "chromium.exe", "firefox.exe"}`，映射为 `WindowRef`。
- `url_of(state)`：从 `elements` 里找 `role == "Edit"` 且 `label` 含「地址」的元素，取 `value`（实测：`label="地址和搜索栏"`）。
- `is_on_screen(windows, pid, window_id)`：在 `windows` 中按 `(pid, window_id)` 查，找不到返回 `False`（**不抛错**）。
- `activate(windows, pid)` / `raise_window(pid, window_id)`：走 SDK `call_tool`（`bring_to_front`），失败静默返回 `False`（沿用 macOS 的 best-effort 口径）。
- `role_name(role)`：查 `WINDOWS_ROLE_MAP`。
- `web_area_roots(elements)`：取 `role == "Document"` 的元素。
- `click_point(pid, window_id, x, y)`：SDK `ClickInput` + `ClickPosition.COORDINATES` + `ActionTarget.WINDOW(pid, window_id)`（**取代 osascript**）。
- `switch_to_first_tab()`：SDK `HotkeyInput`，`Ctrl+1`（取代 macOS 的 `Cmd+1`）。
- `screenshot_px_per_point`：从系统 DPI 缩放读取（`ctypes.windll.user32.GetDpiForSystem()` ÷ 96），初值不得硬编码 2.0。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd services/cua-agent && uv run pytest tests/test_platform_roles.py -q`
Expected: PASS（8 passed）

- [ ] **Step 5: 跑全量确认无回归**

Run: `cd services/cua-agent && uv run pytest tests -q`
Expected: 0 failed

- [ ] **Step 6: Commit**

```bash
git add services/cua-agent/app/drivers/platform/windows.py services/cua-agent/tests/test_platform_roles.py
git commit -m "feat(cua-agent): WindowsAdapter——UIA 角色映射、进程名匹配、SDK 原生坐标点击与热键"
```

---

### Task 5: `create_adapter()` 分发 + 真机 `check_login` 验证

**Files:**
- Create/Modify: `services/cua-agent/app/drivers/platform/__init__.py`
- Modify: `services/cua-agent/app/drivers/cua_sdk.py`（`self._plat` 改由工厂创建）
- Test: `services/cua-agent/tests/test_platform_roles.py`（追加分发用例）

**Interfaces:**
- Consumes: `MacOsAdapter`（Task 2）、`WindowsAdapter`（Task 4）
- Produces: `create_adapter(bridge) -> PlatformAdapter`

- [ ] **Step 1: 追加失败测试**

```python
def test_create_adapter_rejects_unknown_platform(monkeypatch):
    """未知平台必须明确抛错，不得静默选到错误实现。"""
    import sys
    from app.drivers.platform import create_adapter
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="不支持"):
        create_adapter(bridge=None)


def test_create_adapter_picks_windows(monkeypatch):
    import sys
    from app.drivers.platform import create_adapter
    from app.drivers.platform.windows import WindowsAdapter
    monkeypatch.setattr(sys, "platform", "win32")
    assert isinstance(create_adapter(bridge=None), WindowsAdapter)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd services/cua-agent && uv run pytest tests/test_platform_roles.py -q -k create_adapter`
Expected: FAIL — `ImportError: cannot import name 'create_adapter'`

- [ ] **Step 3: 实现 `create_adapter`**

`sys.platform == "win32"` → `WindowsAdapter(bridge)`；`"darwin"` → `MacOsAdapter(bridge)`；其余抛 `RuntimeError("不支持的平台：{sys.platform}")`。`cua_sdk.py` 的 `__init__` 改为 `self._plat = create_adapter(self._bridge)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd services/cua-agent && uv run pytest tests -q`
Expected: 0 failed

- [ ] **Step 5: 真机验证 `check_login`（只读）**

前置：Chrome 已登录猎聘企业版后台（任意已登录页面即可）。

Run: `cd cua_driver && $env:CUA_DRIVER_MODE="real"; uv run python services/cua-agent/scripts/smoke_real.py --step login`
Expected: `check_login() → True`

若为 `False`：用 `.scratch/diag_sdk_win.py` 的思路打印实际窗口列表与树，核对该窗口的 `app_name` 与地址栏元素是否与 `WindowsAdapter` 的判据一致，修正后重跑本步（这属于阶段 2 校准的前哨）。

- [ ] **Step 6: Commit**

```bash
git add services/cua-agent/app/drivers/platform/__init__.py services/cua-agent/app/drivers/cua_sdk.py services/cua-agent/tests/test_platform_roles.py
git commit -m "feat(cua-agent): create_adapter 按平台分发，Windows 真机 check_login 通过"
```

---

## 阶段 1 完成的判据

1. `cd services/cua-agent && uv run pytest tests -q` 全绿（基线 90 + 新增用例）
2. `smoke_real.py --step login` 在本机输出 `check_login() → True`
3. `cua_sdk.py` 中不再出现 `osascript` / `open -b` / `AXStaticText` 等平台专有字面量
4. mac 路径为纯搬移，`git diff` 可核对

**阶段 2/3（不在本计划内）**：按 spec §7 的校准工作流推进 —— 逐页保存真实 UIA 树 dump → 实现 → 固化为 fixture。顺序：读简历 → 发消息 → 推荐人列表 → 向TA索要；写操作仅对自备测试候选人，且最后做。
