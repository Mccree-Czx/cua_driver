# Pi 招聘助手 · 轮次指令（外部调度触发）

每个轮次由外部定时器以 `pi -p --append-system-prompt system-prompt.md "<本文件对应轮次指令>"` 方式触发。
工作窗口 08:00–20:00 由外部调度判断；Pi 只执行。

## 1. main_round（每 10 分钟）

**严格顺序：先打满每日 100 次打招呼，期间完全不碰未读；打满后才处理未读（inbound）。**

每轮开头先调 `quota_status()` 查当日剩余打招呼额度（remaining）：
- **remaining > 0** → 本轮只走 1a outbound 打招呼（推荐 → 最新 tab），**不处理未读**（即便推荐暂未刷新出新卡也留待下一轮，不切 inbound）
- **remaining == 0（今日已打满 100 次）** → 走 1b inbound 处理未读

### 1a. outbound（打招呼，remaining > 0 时）
推荐页顶部有「推荐」「最新」两个 tab（content-desc 分别为「推荐」「最新」，紧邻「筛选」）。先「推荐」tab 处理，耗尽后切「最新」tab：

1. 当前 tab 连续 dump 逐个处理（单轮上限 50 卡防会话过长）：读简历 → 建档（candidate_get_or_create，source='recommended'）→ jc_get_or_create → 评分（screen）→ ensure_one_message + candidate_has_out_message 双查 → gated_tap(touch=True) 点「立即沟通」（默认 counts_daily=True，计入每日 100）→ 每打 3-5 个后批量 dump 验证上屏 → interaction_log(out) → state_transition → 落账 tasklog_add。跳过已触达者（candidate_has_out_message=true）。
2. 当前 tab 的卡全部已建档/已处理（无新卡）→ 若还在「推荐」tab，tap「最新」tab（读操作，用普通 tap，不经 gated_tap）→ 回到步骤 1 继续处理「最新」tab。
3. 「最新」tab 也耗尽 → 本轮 outbound 结束（零产出属正常），**不转 inbound**，留待下一轮推荐刷新继续打。

gated_tap 返回 rate_limited 时**立即停止打招呼、转 1b inbound**。

### 1b. inbound（未读消息，仅 remaining == 0 时，即今日已打满 100 次）
用 mobile_mcp 打开猎聘，进入消息页，**先 tap「未读」过滤 tab**，**只处理「未读」列表里的会话**（已处理自动变已读、从列表消失，禁止处理已读，防止跨轮重复建档/重复触达）。对每个未读会话：
- 读候选人简历字段（tap 进详情，dump 提取 7 字段）
- candidate_get_or_create（时间戳 ID，source='inbound'）→ candidate_update_snapshot → jc_get_or_create(job_id)
- risk_check 熔断则停；screen 评分
- **已带附件简历（详情页有「附件简历」区 + 文件名）→ 直收：进 PDF 查看器（「去查看」→ 点 PDF 文件卡）→ 先 `mobile_dump_ui` 确认当前屏是 PDF 查看器（有「第 X 页/共 X 页」或 PDF 内容特征）→ 确认后再 capture_attachment_pages(liepin_user_id) 归档 → 拿到 object_keys 后再 state_transition(receive_resume) + interaction_log(in, attachment)；跳过触达（对方已发简历，无需再索要）。若 dump 确认不是 PDF 查看器（仍停在在线简历/详情页）→ 不截图，如实报告「未找到附件」，不推进 receive_resume**
- 无附件简历且判定通过 → ensure_one_message + candidate_has_out_message 双查 → gated_tap(touch=True, counts_daily=False) 索要简历（响应性触达，不计入每日 100）→ 验证上屏 → **先 interaction_log(out) 再 state_transition**（顺序不可颠倒）
- 判定拒绝 → state_transition(reject_hard/reject_llm)
- 落账 tasklog_add

输出：本轮 outbound 打招呼几人、inbound 处理几个未读会话、每个的结果（含已收简历者的分页截图键）。

**末尾必须单独输出一行机器可读结果**（供调度脚本判定空轮退避，格式固定、数字为整数）：
`@@RESULT@@ inbound=<N> outbound=<M>`
- inbound = 本轮实际处理的未读会话数（走 1a outbound 或无未读时填 0）
- outbound = 本轮打招呼人数（走 1b inbound 时填 0）

## 2. daily_round（每天 19:50）

一次性收尾，顺序执行：

1. **72h 关闭 + 追附件**：调用 close_stale_awaiting()（72h 关闭 awaiting_resume）；再 list_awaiting()，对每个 awaiting 的 jc：手机进对应会话查是否已收到附件简历；有 → 进 PDF 查看器后 capture_attachment_pages(liepin_user_id) 分页截图归档 + state_transition(receive_resume) + interaction_log(in, attachment)；无 → 不动。全程无触达。
2. **登录检查**：手机打开猎聘，dump 判断登录态：未登录（登录页锚点）→ risk_set() + 报告「请扫码登录」；已登录 → risk_clear()。
3. **配额对账**：调 quota_status() 报当日已打招呼 used_today/100。触顶（remaining==0）→ 报告「当日 100 次打招呼已用完，明日 08:00 恢复」。本轮只报告，不操作手机。
4. **延期重判**：查 jc_status 中 status=new 且 judge_reason 含 deferred 的 jc，用已存 online_resume_minimal 重新 screen；通过者按 inbound/outbound 路径补触达（同 §1 的闸门流程）。
