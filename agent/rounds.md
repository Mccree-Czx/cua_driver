# Pi 招聘助手 · 轮次指令（外部调度触发）

每个轮次由外部定时器以 `pi -p --append-system-prompt system-prompt.md "<本文件对应轮次指令>"` 方式触发。
工作窗口 08:00–20:00 由外部调度判断；Pi 只执行。

## 1. main_round（outbound 单轮 35 卡 / inbound 单轮 10 会话 / 心跳每 2 分钟，串行锁保证同一时刻仅一轮）

**严格顺序：先打满每日 100 次打招呼，期间完全不碰未读；打满后才处理未读（inbound）。**

每轮开头先调 `quota_status()` 查当日剩余打招呼额度（remaining）：
- **remaining > 0** → 本轮只走 1a outbound 打招呼（推荐 → 最新 tab），**不处理未读**（即便推荐暂未刷新出新卡也留待下一轮，不切 inbound）
- **remaining == 0（今日已打满 100 次）** → 走 1b inbound 处理未读

### 1a. outbound（打招呼，remaining > 0 时）
先调 `job_list_active()` 读 active 职位列表（id + title）。推荐页顶部有**职位 tab**（content-desc 为职位名截断，如「机械结构工程…」「海外ToB渠道…」，紧邻搜索框下方），下方才是「推荐」「最新」两个候选人 tab（content-desc 分别为「推荐」「最新」，紧邻「筛选」）。**先 tap 职位 tab 选定职位，再处理该职位的候选人**：

打招呼按**职位均分**（防主职位霸占额度）：
1. `job_list_active()` 得 N 个职位；每职位日配额 = floor(100/N)（余数不固定，哪个职位有候选人就打）。
2. `job_quota_status()` 读每职位今日已打数，得「已打 < 配额 且 还有候选人」的职位清单，按此清单轮转。
3. 对轮转清单中的职位：tap 该职位的职位 tab（content-desc 匹配 title 前几字；读操作普通 tap）→ 该职位下先「推荐」tab、耗尽后切「最新」tab，逐个处理（**单轮上限 35 卡防会话过长，跨所有职位累计**）：读简历 → 建档（candidate_get_or_create，source='recommended'）→ **jc_get_or_create(job_id=该职位 id)** → 评分（screen）→ ensure_one_message + candidate_has_out_message 双查 → gated_tap(touch=True) 点「立即沟通」（默认 counts_daily=True，计入每日 100）→ 每打 3-5 个后批量 dump 验证上屏 → interaction_log(out) → state_transition → 落账 tasklog_add。跳过已触达者（candidate_has_out_message=true）。
4. 该职位打满配额 或 推荐/最新耗尽（候选人不够）→ 从轮转清单移除，其剩余额度让给其他未满职位。
5. 所有职位满配额 或 全部候选人耗尽 或 单轮 35 卡到顶 或 gated_tap 返回 rate_limited → 本轮 outbound 结束（零产出属正常），**不转 inbound**，留待下一轮继续。

gated_tap 返回 rate_limited 时**立即停止打招呼、转 1b inbound**。

### 1b. inbound（未读消息，仅 remaining == 0 时，即今日已打满 100 次）
用 mobile_mcp 打开猎聘，进入消息页，**先 tap「未读」过滤 tab**，**只处理「未读」列表里的会话**（已处理自动变已读、从列表消失，禁止处理已读，防止跨轮重复建档/重复触达）。对每个未读会话（**单轮上限 10 个，超出的留待下一轮**）：
- 读候选人简历字段（tap 进详情，dump 提取 7 字段）
- candidate_get_or_create（时间戳 ID，source='inbound'）→ candidate_update_snapshot → **读详情页「沟通职位」得到职位名 → job_list_active() 按 title 模糊匹配 → jc_get_or_create(job_id=匹配到的职位 id)；匹配不到则 job_upsert(title=沟通职位, jd_text="") 录入后拿新 id 再 jc_get_or_create**
- risk_check 熔断则停；screen 评分
- **已带附件简历（会话有附件气泡[文件名] 或 详情页有「附件简历」区 + 文件名；「看简历」打开的在线简历[带「在线」标签]不算）→ 直收：会话里点对方头像进资料页 → 划到底 → 点「附件简历」进 PDF 查看器（右上角有「转发」按钮）→ download_attachment(job_id, liepin_user_id, name, job_title) 下载真实 PDF 并归档 → 拿到 object_key 后再 interaction_log(in, attachment) + state_transition(receive_resume)；跳过触达（对方已发简历，无需再索要）。注意：只有「头像→资料页→附件简历」路径打开的 PDF 才有「转发」按钮；若进不了附件 PDF → 如实报告「未找到附件」，不推进 receive_resume**
- 无附件简历且判定通过 → ensure_one_message + candidate_has_out_message 双查 → gated_tap(touch=True, counts_daily=False) 索要简历（响应性触达，不计入每日 100）→ 验证上屏 → **先 interaction_log(out) 再 state_transition**（顺序不可颠倒）。**无附件时严禁 receive_resume / interaction_log(msg_type='attachment')——在线简历截图不是附件，工具会因无附件证据拒绝**
- 判定拒绝 → state_transition(reject_hard/reject_llm)
- 落账 tasklog_add

输出：本轮 outbound 打招呼几人、inbound 处理几个未读会话、每个的结果（含已收简历者的 PDF 归档键）。

**末尾必须单独输出一行机器可读结果**（供调度脚本判定空轮退避，格式固定、数字为整数）：
`@@RESULT@@ inbound=<N> outbound=<M>`
- inbound = 本轮实际处理的未读会话数（走 1a outbound 或无未读时填 0）
- outbound = 本轮打招呼人数（走 1b inbound 时填 0）

## 2. daily_round（每天 19:50）

一次性收尾，顺序执行：

1. **72h 关闭 + 追附件**：调用 close_stale_awaiting()（72h 关闭 awaiting_resume）；再 list_awaiting()，对每个 awaiting 的 jc：手机进对应会话查是否已收到附件简历；有 → 点对方头像进资料页 → 划到底 → 点「附件简历」→ download_attachment(job_id, liepin_user_id, name, job_title) 归档真 PDF + state_transition(receive_resume) + interaction_log(in, attachment)；无 → 不动。全程无触达。
2. **登录检查**：手机打开猎聘，dump 判断登录态。
   - 未登录（登录页锚点）→ `risk_set()` + 报告「猎聘已掉登录：请到网页端的『账号管理』里发起登录，或人工在手机上登录」。
   - 已登录 → `risk_clear()`。
3. **配额对账**：调 quota_status() 报当日已打招呼 used_today/100。触顶（remaining==0）→ 报告「当日 100 次打招呼已用完，明日 08:00 恢复」。本轮只报告，不操作手机。
4. **延期重判**：查 jc_status 中 status=new 且 judge_reason 含 deferred 的 jc，用已存 online_resume_minimal 重新 screen；通过者按 inbound/outbound 路径补触达（同 §1 的闸门流程）。
5. **职位检测**：手机打开猎聘 → tap 底部「职位」tab → tap「招聘中」tab → dump 职位列表（提取每个职位的职位名）→ `job_list_active()` 读 DB 已有职位名 → 对比，发现**新职位**（DB 无此 title）→ 点进该职位 → 找「编辑职位」入口 → dump「职位描述」字段得到 jd_text → `job_upsert(title=职位名, jd_text=JD)` 录入。无新职位则报告「无新职位」。全程无触达。

## 3. backfill_round（一次性回填：历史截图 → 真实 PDF）

把历史上用「截图方案/误判」处理的候选人，逐个补下载真实 PDF 附件。**全程无触达**（不打招呼、不发消息、不索要）。

1. 调 `list_backfill_candidates()` 得待回填名单（name / liepin_user_id / job_id / job_title，已去重）。
2. 手机进消息页「**全部**」tab（不是「未读」——这些是已读老会话）。逐个按**姓名 + 岗位**在列表里找对应会话（姓名模糊匹配、岗位辅助确认；同名者务必以岗位区分，避免挂错人）。
3. 对每个候选：点进会话 → 确认有「**附件简历**」气泡/按钮：
   - **有附件** → 点对方头像进资料页 → 划到底 → 点「附件简历」进 PDF 查看器（右上角有「转发」按钮）→ `download_attachment(job_id, liepin_user_id, name, job_title)` 下载真 PDF。
   - **无附件**（只有在线简历/打招呼，没有附件气泡）→ 跳过，报告「误判：{name} 无附件」。
4. 单轮上限 20 个，超出的留待下一轮（下一轮再调 `list_backfill_candidates()` 会只剩未完成的）。
5. 输出：本轮回填几人、成功几人、误判几人（列出误判名单）。

## 4. login_round（代登录猎聘账号，按需触发，**不受工作窗口限制**）

由 Mac 上的 `login_watch.py` 发现 Redis 里有登录请求时拉起。用户在网页上选好登录方式（`password` 全自动 / `code` 需人工填一次码）后发起。

**每轮开头两件事，顺序不能换**：

1. `login_map_get()` —— 拿界面锚点。**`stale=true` 就停手**，报告「锚点过期，需重新探查」，不要照着半年前的坐标去点。
2. `login_req_get()` —— 拿本轮要做什么：`phase` / `method` / `phone` / `resend_seq` / `notice`。

**本文件不区分「第一次调用/第二次调用」**——一切以 `phase` 为准，因为两段之间隔着一个不可预测的人工填码时长。

全程**只碰登录界面**：不碰消息、不碰候选人、不打招呼。手机此刻可能处于未登录状态，任何触达都不可能、也不允许。

### 4x. 通用规则（比写死步骤更耐 App 升级）

- **按钮点不动就先勾协议**。猎聘要求勾上「我已阅读并同意」之后按钮才可用。不要去猜勾选框的坐标：dump 里找 `我已阅读并同意` 附近那个方框节点，点它，再重试按钮。这条适用于「下一步」「登录」以及任何灰着的按钮。
- **锚点失配就重新 dump，并把你看到的新锚点回写进 `login_map.json`**，不要硬点看不到的东西。
- 每步之间保持 1 秒以上间隔（铁律 7）。
- **凡是点不动、找不到、看不懂，都如实写进 `login_req_set(..., note)`**，让用户在前端看到原因，而不是只在日志里。

### 4a. `phase = pending`

`mobile_open_app(packageName="com.lietou.mishu")` 打开猎聘，等渲染后 `mobile_dump_ui`，**先判断现在是什么页面**（这一步决定后面所有动作，不要跳过）：

- **已是招聘首页**（底部有「职位」「消息」等 tab）→ 手机上还登着别的号。**绝不尝试登出**（自动化登出属于另一个批次，风险更高），直接 `login_req_set("failed", "手机上当前登录的是其他账号，请先在猎聘 App 里退出登录，再重新发起")`，结束本轮。
- **是登录页**（有手机号输入框）→ 继续下面的「发出验证码」。

#### 发出验证码（唯一路径）

> **密码登录已停用**（2026-10-15 实测：填完账号密码点「登录」会弹滑块验证码，走不通）。
> `login_map.json` 里那个 `password_login` 段落**不要用**——它是历史锚点，留作参考。
> 唯一可用路径就是下面这条验证码登录。

1. 确认当前在 `login_map.json` 的 `login_page_anchor`（「手机号登录/注册」）页；
   不在就用 `password_login.back_to_code_login` 切回。
2. tap `phone_login.phone_field` → `login_fill_phone()` → **必须确认 `ok=true` 且 `verified=true`**。
   `verified=false` 说明号码没真正上屏，**停下报失败**，不要接着往下走。
   （登录页的手机号是**预填**的，工具会先清空再输——直接敲会变成两个号粘一起。）
3. 勾协议（见 4x 的「按钮点不动就先勾协议」）。
4. `login_req_set("dispatched")` → 点 `next_button`（「下一步」）→ 等渲染 → dump。
5. **不要去找「获取验证码」按钮**——登录页没有这个按钮。实测点完「下一步」验证码就
   **自动发出**，界面直接跳到「输入验证码」页（标题「验证码已发送到+86 …」+ 倒计时）。
   dump 确认确实进了这个页面。
6. 确认无误后 `login_req_set("awaiting_code")`，**本轮结束**。
   **不要等待验证码、不要轮询、不要反复 dump**。验证码发到的是用户自己的另一台手机，
   Pi 物理上等不到；把控制权交回前端，用户填码后会有新的一轮。
- **出现「图形验证码」「猎聘安全中心」「账号行为异常」** → 按铁律 3：`risk_set()` → `login_req_set("failed", "遇到风控验证，需人工处理")` → 结束。
- **其它不认识的页面** → 重新 dump 一次；仍不认识 → `login_req_set("failed", "停在无法识别的页面：<把你看到的关键文本写进来>")`。

### 4b. `phase = awaiting_code`——把人工填的码提交掉

1. `login_req_claim_code()` 取码。
   - 返回 `code: None` → `login_req_set("failed", "验证码不可用（未填、已用过或已过期）")`，结束。**不要重试这个工具。**
2. `mobile_open_app` → dump，确认仍在验证码等待页。
   - 若已不在该页：dump 判断——已是招聘首页 → 直接 `login_req_set("success")`；回到登录页（页面超时重置）→ `login_req_set("failed", "登录页已重置，请重新发起")`。
3. tap 验证码输入框 → `mobile_type(code)` → dump 确认已上屏。
4. `login_req_set("verifying")` → 点「登录」（点不动就先勾协议）→ 等渲染 → dump。
5. 判定：
   - 进入招聘首页（底部 tab 齐全）→ `login_req_set("success")`。该工具会自动把此账号写为登录态、并置为系统生效账号。
   - 出现「验证码错误」「已失效」→ `login_req_set("failed", "验证码错误或已失效，请重新发起")`。
   - 出现图形验证码 / 风控页 → `risk_set()` + `login_req_set("failed", "遇到风控验证，需人工处理")`。
   - 其它 → 重新 dump 一次；仍无法判定 → `login_req_set("failed", "提交后停在无法识别的页面：<关键文本>")`。

### 4c. 本轮的铁律

- **不点「重发」循环**。重发只由用户在前端触发；`resend_seq` 变了才点一次。
- **不尝试登出**、不切换账号、不改任何账号设置。自动登出是另一个批次的功能，有它自己的回滚前置条件。
- **不碰消息与候选人**：本轮没有任何触达，不需要 `gated_tap`。
- 勾「我已阅读并同意」是在**代用户做同意动作**。这是登录流程的必经步骤（不勾按钮就不亮），但汇报时要如实提一句，不要当成没发生过。


