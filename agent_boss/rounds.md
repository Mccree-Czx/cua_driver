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

### 1b. inbound（未读消息）
1. 切「消息」tab，读会话列表（未读 badge rid `badge_view`），只处理未读会话（单轮上限由外部 `BOSS_ROUND_MAX_ITEMS` 控制）。
2. 对每个未读会话：
   - 点进会话读对方消息（BOSS 打开会话会自动标已读）
   - `candidate_get_or_create(name)` 建档（inbound，不评分）
   - 对方已发简历 → `state_transition(jc_id, "receive_resume", source="inbound")`
   - 对方想发附件简历 → 检测「附件简历」气泡 → 汇报让确定性路径下载，不逐步微操
   - 需要回应 → `greet_check` → `gated_tap(touch=True)` → `interaction_log(out)`
   - 落账 tasklog_add

输出：本轮 outbound 打招呼几人、inbound 处理几个未读会话、每个的结果（含已收简历者的去向）。

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
