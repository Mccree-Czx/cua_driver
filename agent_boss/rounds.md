# Pi 招聘助手 · BOSS 直聘轮次指令（外部调度触发）

每个轮次由外部定时器以 `pi -p --append-system-prompt system-prompt.md "<本文件对应轮次指令>"` 方式触发。工作窗口 08:00–23:00、设备检查、登录关卡在途、全局轮次锁等闸门由外部调度（`run_round.py`）判断；Pi 只执行。

## 1. main_round（外部每 5 分钟拉起一次；全局锁保证同一时刻仅一轮）

每轮开头先 `quota_status()` 查当日剩余打招呼额度 remaining：
- **remaining > 0** → 先走 1a outbound 打招呼；配额耗尽后转 1b inbound
- **remaining == 0（当日已打满）** → 直接走 1b inbound

### 1a. outbound（打招呼）
1. `job_list_active()` 读可用职位（id + title）。没有可用职位就报告并跳过 outbound。
2. 切「牛人」tab，读候选人推荐列表。逐个处理（**单轮上限由外部 `BOSS_ROUND_MAX_ITEMS` 控制，默认不设**）：
   - mobile_dump_ui 读候选人卡（rid `tv_geek_name`=姓名，`tv_content`/`tv_tag_text`=摘要/标签）
   - `candidate_get_or_create(name, job_title)` 建档（boss_key 留空，name 兜底）
   - 点卡进详情页（`BossViewGeekResumePagerActivity`）读详情字段 → 再 `candidate_get_or_create` 补详情（空值不盖已有）
   - `jc_get_or_create(job_id, candidate_id)`（或建档时给 job_title 顺带建 jc）
   - `greet_check(jc_id)` → `allowed=false` 就停手该人（评分不过 / 已打过 / 配额尽 / 风控），如实报告
   - `allowed=true` → 找详情页底部「立即沟通」按钮 → `gated_tap(x, y, touch=True)`
   - 验证上屏 → `interaction_log(jc_id, "out", "greet")`（顺带推进状态）
   - 退回牛人列表继续；**绝不往回滚**（往回滚会刷新推荐流）
3. 每打 3-5 个批量 dump 验证上屏；漏发者标「可能未发送」，绝不补发/重试。

### 1b. inbound（未读消息）——**先「仅沟通」后「新招呼」**

切「消息」tab；子 tab 按 `tv_title` 文本找「仅沟通」「新招呼」切换（无 selected 标记、切子 tab 不清筛选）。单轮上限由外部 `BOSS_ROUND_MAX_ITEMS` 控制。

**1b-1 仅沟通（高优先）**：切「仅沟通」→ 逐条未读会话：
- 点进读对方消息（打开自动标已读）→ `candidate_get_or_create(name)` 建档
- 对方想发附件简历（「对方想发送附件简历…」/「[附件简历]」气泡）→ 找「同意」按钮（`tv_dialog_btn_right` 文字精确「同意」）→ `gated_tap(touch=True)`；下载留确定性路径，不微操
- 对方已直接发简历 → `state_transition(jc_id, "receive_resume", source="inbound")`

**1b-2 新招呼（低优先）**：切「新招呼」→ 逐条未读会话：
- 读消息 → 点**第一条消息里嵌着的候选人卡片**（`mRootViewLayout`，含 mJobIntent/mSalary/mGeekInfo/mContentView）开「在线简历」（`BossViewGeekResumePagerActivity`）→ 读详情 → `candidate_get_or_create(name, job_title)` 建档（job_title 取会话 `tv_position`/聊天页 `tv_sub_title`）。★别点顶部姓名 tv_title（进的是聊天设置 ChatSettingsNewActivity）
- 无职位关联 → 跳过评分/求简历，报告（fail-closed）
- `screen(jc_id, source="inbound")`：
  - `screened_pass` → 触达索要简历：先「求简历」按钮 `gated_tap(touch=True)`（快捷操作区 mTextView 文字精确「求简历」，clickable=false，tap 其中心）；找不到/点不了 → `mobile_type`「您能发一份简历给我们吗」→ 点「发送」`gated_tap(touch=True)`（输入框 editText_with_scrollbar 右侧 clickable 无 rid 的 LinearLayout，随键盘浮动现找）。然后 `interaction_log(out, "request_resume")` → `state_transition(jc_id, "request_resume", source="inbound")`（先落账后推进）
  - `rejected_*` / 服务不可用 → 不发，报告

输出：本轮 outbound 打招呼几人、inbound 处理几个未读会话（仅沟通几个 / 新招呼几个、各自同意/求简历/评分结果）。

**末尾必须单独输出一行机器可读结果**（供调度脚本判定空轮退避，格式固定、数字为整数）：
`@@RESULT@@ inbound=<N> outbound=<M>`
- inbound = 本轮实际处理的未读会话数（走 1a outbound 或无未读时填 0）
- outbound = 本轮打招呼人数（走 1b inbound 时填 0）

## 2. daily_round（每天 19:50 一次性收尾）

1. **收尾**：`daily_sweep()` —— 72h 无人回应的 awaiting_resume 关闭 + 给没评分的 new 补评分（上限 20）。
2. **登录检查**：手机打开 BOSS，dump 判断登录态。
   - 未登录（登录页锚点）→ `risk_set()` + 报告「BOSS 已掉登录：请人工在手机上登录」。
   - 已登录 → `risk_clear()`。
3. **配额对账**：`quota_status()` 报当日 used_today/daily_cap；remaining==0 报告「当日额度用完，明日 08:00 恢复」。本轮只报告，不操作手机打招呼。

## 3. backfill_round（一次性回填：补历史候选人的评分/状态）

全程无触达（不打招呼、不发消息、不索要）。

1. 对库里 status=new 且没评分的 jc 补评分：`screen(jc_id)`。
2. `daily_sweep()` 关闭超时未回的 awaiting_resume。
3. 单轮上限由外部 `BOSS_ROUND_MAX_ITEMS` 控制（超出的留待下一轮）。
4. 输出：本轮回填几人、评分几人、关闭几人。

## 4. login_round（**人工兜底，不经 Pi**）

BOSS 登录有三道人工关卡（易盾滑块 / 短信 / 人脸实名），**正常登录路径不经过 Pi**。`run_round.py` 的 login_round 只做一件事：dump 判断是否停在登录/风控页，是则标记 `boss:login_gate_in_flight` 等人过，**绝不自动登录、绝不提交验证码**。本轮不需要 Pi。
