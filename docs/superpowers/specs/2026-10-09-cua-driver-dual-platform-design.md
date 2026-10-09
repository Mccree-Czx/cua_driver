# CUA 驱动双平台支持设计（macOS / Windows）

> 状态：设计定稿（2026-10-09）。范围：`services/cua-agent/app/drivers/` 的平台抽象改造。
> 关联：`docs/runbook.md`（真实运营记录）、`liepin-hr-assistant-spec-v1.6.md`（部署形态）。

## 1. 背景与目标

`CuaLiepinDriver`（`app/drivers/cua_sdk.py`，1434 行）是真实页面的 CUA 驱动，
已在真实猎聘账号上验证通过（runbook 记录 2026-10-06 冒烟 ①③ 通过）。但它是按
**macOS** 写的，而 spec v1.6 §部署形态 与 runbook 假设的是 **Windows 主机**部署。
两者不一致，导致当前在 Windows 机器上真实模式不可用。

**目标**：`CuaLiepinDriver` 同时支持 macOS 与 Windows，Windows 达到**完整对等**
（协议 9 方法 + LLM 兜底 3 方法全部可用）。

**约束**：

- Windows 是唯一实盘环境；macOS 版本冻结保留，本次之后不再在 macOS 上使用。
- 真实驱动**零单元测试覆盖**（`tests/` 下只有 `test_fake_driver.py`），改造没有测试网兜底。
- 账号有风控风险：runbook 记录过两次「账号行为异常」安全验证页（连续高频操作触发）。

## 2. 实测事实（2026-10-09，本机 Windows 10）

| 项 | 实测结果 |
|---|---|
| CUA SDK（`cua-driver` 0.30.4） | **Windows 上完全可用**：`CuaDriver.create()` 成功；`list_windows()` 枚举到全部窗口；`get_window_state()` 读到完整 UIA 树，元素带 `[id=view_N actions=[invoke]]` |
| `osascript` / `open` | **不存在**（macOS 专有） |
| 浏览器进程名 | Windows：`chrome.exe` / `msedge.exe`；macOS 代码里写的是 `"Google Chrome"` |
| 地址栏元素 | Windows：`role="Edit"` `label="地址和搜索栏"` `value="lpt.liepin.com/recommend"`；macOS 代码判据是 `role="AXTextField"` + label 含「地址」 |
| Windows 树角色（样本） | `Window` / `Pane` / `Button` / `Edit` / `Document` —— 与 macOS 的 `AX*` 命名体系完全不同 |

**`check_login()` 假阴性根因**（当前最直接的症状）：`BROWSER_APPS` 写的是 macOS 应用名
（`"Google Chrome"` 等），Windows 上 `app_name` 是进程名 `chrome.exe` → 窗口过滤后列表为空 →
直接返回 `False`。**与登录态无关**（实测时 Chrome 确实停留在 `lpt.liepin.com/recommend`）。

**SDK 的跨平台能力**（消除了"需要引入 pyautogui"的顾虑）：
`ClickPosition.COORDINATES`（坐标点击）、`HotkeyInput`、`PressKeyInput`、`MoveCursorInput`、
`ScrollInput`、`InvokeMenuInput`、`SetWindowFrameInput` 均为 SDK 原生，不需外部工具。

## 3. 平台差异分布

| 分类 | 内容 | 处理 |
|---|---|---|
| **平台无关**（可共享） | `_RuntimeBridge`、会话管理、全部错误类型、元素级点击 `_press`、输入 `_press_key` / `_type_text`、`window_state`、`capture_desktop_png`，**以及大部分流程编排**（`_reach_batch_page`、`_candidate_detail`、`read_online_resume` 的步骤序列等） | 留共享层 |
| **平台相关**（必须分叉） | 窗口/应用匹配、地址栏判据 `_current_url`、`_find` 的角色名、树锚点解析（`_tab_names` / `_recommend_cards` / `_detail_liepin_id` / `_value_after_icon` / `_preview_name` …）、`_click_point`、`_hotkey_tab_1`、`_activate_browser`、`ensure_visible`、截图缩放系数 | 下沉 PlatformAdapter |

关键判断：**流程编排（占大头）本身平台无关**，它只调用定位原语。平台差异可收敛到一个窄接口。

## 4. 架构

### 4.1 模块结构

```
app/drivers/
  fake.py                 # 不动
  cua_sdk.py              # CuaLiepinDriver：流程编排 + 平台无关原语（瘦身）
  platform/
    __init__.py           # create_adapter() —— 按 sys.platform 分发（win32→windows，darwin→macos）
    base.py               # PlatformAdapter 协议 + Role 枚举 + WindowRef
    macos.py              # macOS 原语（从 cua_sdk.py 机械搬移）
    windows.py            # Windows 原语（新增，待校准）
```

### 4.2 语义角色（取代硬编码角色名）

```python
class Role(StrEnum):
    TEXT; BUTTON; RADIO; CHECKBOX; IMAGE; LINK; TEXT_INPUT; TEXT_AREA; WEB_AREA

# macos.py:   Role.TEXT -> "AXStaticText", Role.BUTTON -> "AXButton", Role.RADIO -> "AXRadioButton",
#             Role.CHECKBOX -> "AXCheckBox", Role.IMAGE -> "AXImage", Role.LINK -> "AXLink",
#             Role.TEXT_INPUT -> "AXTextField", Role.TEXT_AREA -> "AXTextArea", Role.WEB_AREA -> "AXWebArea"
# windows.py: 待校准（实测样本已知 Button / Edit / Pane / Document）
```

流程代码改写为 `_find(state, role=Role.BUTTON, label="发送")`。角色映射表是**纯数据 → 可单测**。

### 4.3 PlatformAdapter 协议

| 组 | 方法 | macOS 实现 | Windows 实现 |
|---|---|---|---|
| 窗口 | `candidate_windows()` | 现有 `BROWSER_APPS` 匹配 | `chrome.exe` / `msedge.exe` 匹配 |
| 窗口 | `url_of(state)` | `AXTextField` + 含「地址」 | `Edit` + 「地址和搜索栏」 |
| 窗口 | `is_on_screen(pid, wid)` / `activate(pid)` / `raise_window(pid, wid)` | `open -b` + `invoke_menu` | 待定（SDK `SetWindowFrameInput` / `bring_to_front` / `SetForegroundWindow`） |
| 解析 | `semantic_role(element)` | `AX*` 角色表 | UIA 角色表（待校准） |
| 解析 | `web_area_roots(state)` | `AXWebArea` 判据 | `Document` / `Pane` 判据（待校准） |
| 动作 | `click_point(pid, wid, x, y)` | `osascript` System Events | SDK `ClickPosition.COORDINATES` |
| 动作 | `hotkey(keys)` | `keystroke ... using {command down}` | SDK `HotkeyInput`（Cmd → Ctrl） |
| 动作 | `screenshot_px_per_point` | `2.0`（Retina 实测） | 待校准（DPI 缩放系数） |

协议的方法签名与返回类型在 `base.py` 定义。元素对象**沿用 SDK 原生的 `WindowElement`**
（两侧同源、字段一致），不额外包装——避免多一层适配。`candidate_windows()` 返回
`list[WindowRef]`，`WindowRef` 是 SDK `WindowInfo` 的窄化视图，只保留
`pid` / `window_id` / `app_name` / `title` / `is_on_screen`。

### 4.4 数据流

`CuaLiepinDriver.__init__` → `create_adapter()` → 注入 `self._plat`。所有平台相关调用收敛为
`self._plat.*`；其余流程编排一行不变。`CuaLiepinDriver` 的类名、构造签名（无参）与对外方法集
保持不变，**worker / executor / fallback 无需改动**。

## 5. 关键设计决策

1. **抽象粒度：先细后粗。** 只下沉「角色名、窗口判据、动作通道」；树结构解析（如"选项卡是
   webarea 的直接子级"）先留在共享流程里，用 `web_area_roots()` 这类根判据适配。校准中若发现
   某段解析两边差异过大，再单独下沉——避免一上来就为每个页面流程定义平台专属解析方法，
   导致接口膨胀。
2. **macOS 路径纯机械搬移，无行为变更。** 逐方法搬进 `macos.py`，逻辑一行不改；commit 标注
   `refactor: 机械搬移，无行为变更`，便于将来有 mac 环境时针对性回归。
3. **角色映射表是纯数据**，两侧都可单测。
4. **保持 `CuaLiepinDriver` 对外契约不变**，改造对上层零影响。

## 6. 测试策略

| 新增测试 | 作用 |
|---|---|
| `test_platform_roles.py` | 断言 mac / windows 角色映射表（纯数据） |
| `test_flow_with_fake_adapter.py` | 假 adapter + 伪造 `WindowStateOutput` 驱动流程编排——让"找按钮→点击→校验"这类流程**首次可单测** |

- 现有 `test_fake_driver.py` 与其余 90 个 cua-agent 测试不受影响。
- **macOS 搬移无回归验证**（无树 dump、无 mac 机器）——这是本次改造唯一的风险敞口，
  缓解手段仅为"纯机械搬移 + diff 审查"。

## 7. Windows 校准工作流

对每个页面流程：

1. 保存真实 UIA 树 dump 到 `.scratch/windows_probe/<页面>.txt`（含关键元素列表）
2. 据此实现 `windows.py` 的定位逻辑、补齐 `Role` 映射
3. 将 dump 脱敏固化为测试 fixture（`tests/fixtures/win_*.json`）→ Windows 路径逐步获得离线回归能力

顺序（先只读，写操作最后）：`check_login` → 读简历 → 发消息 → 推荐人列表 → 向TA索要。

**风控缓解**：读链只读相对安全；写链（发消息 / 索要）务必最后做，仅对自备测试候选人，
保持动作间隔（沿用现有 `CUA_TASK_GAP_SECONDS` 节奏闸）。

## 8. 分阶段交付

| 阶段 | 内容 | 前置 |
|---|---|---|
| 1 | 平台抽象层 + mac 机械搬移 + 两个新测试（可离线）；Windows `check_login` 跑通（需 Chrome 登录着猎聘，只读） | 大部分离线，末项需页面在 |
| 2 | Windows 读简历 + 发消息 | 需真实账号在场配合 |
| 3 | Windows 推荐人列表 + 向TA索要（M2） | 需真实账号在场配合 |

## 9. 待校准项（实现阶段落地）

- Windows 的 UIA 角色名 → `Role` 完整映射
- Windows 的窗口激活/前置手段选型
- Windows 的 `screenshot_px_per_point`（受 DPI 缩放影响）
- 树结构锚点在 Windows UIA 树上的实际形态（选项卡、候选人卡、附件区等）

## 10. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| macOS 搬移无回归验证 | 可能静默改变 mac 行为 | 纯机械搬移；mac 路径冻结；commit 标注待回归 |
| 平台风控（账号行为异常） | 账号被安全验证页拦截 | 先只读；写链最后做；仅自备测试候选人；保持节奏闸 |
| 校准依赖人工配合 | 阶段 2/3 无法自动完成 | 阶段 1 先交付；阶段 2/3 约时间在场 |
| 抽象粒度选错 | 接口膨胀或共享流程难以适配 | 先细后粗，校准中按需下沉 |
