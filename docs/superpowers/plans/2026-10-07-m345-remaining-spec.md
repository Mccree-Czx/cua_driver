# 待执行 Spec：M2 完整校准 + M3 工作台最小可用 + M4 可观测（2026-10-07 拟定）

> 状态：**待账号静置期结束后执行**。拟定于 2026-10-07 凌晨（第三次风控事件前夜），
> 因账号进入第三轮静置而整体顺延。授权口径与边界见下，执行时直接照此推进。

## 授权口径（2026-10-07 用户已确认，静置结束后仍然有效）

- 「立即沟通」：允许点击一次作语义校准（知悉可能对 1 位真实候选人产生不可撤回触达）
- M2 首批 ≤2 人 greet：授权直发（按既定话术，无需逐条确认）；执行时点取工作窗口内
- 触达之外沿用既定纪律：探针只读、≥120s 间隔、单批 ≤3、风控熔断即停

## A. M2 剩余

1. 修 `scripts/deploy/install.sh` 的 `$label（` 多字节 bug（`${label}`）→ 重装四 plist（解释器直连版）
2. 第二轮推荐人页探针（只读 + 授权点击各一次）：
   - Cmd+1 → 「人才推荐」(74,209) → recommend 页
   - 定位双通道：AX frame 多轮重试 / 截图像素目测（px÷2=屏幕点）
   - 点击首卡姓名区 → 是否打开含「简历编号」详情；点一次「立即沟通」→ 记录语义
   - 已有安全脚本：`.scratch/probe_recommended2.py`（含「距沟通类按钮 <60pt 拒绝」安全校验）
3. 按结论实装 real `list_recommended`（替换 NotImplementedError 桩）+ 单测 + mock 回归 + 真实只读验证
4. 若「立即沟通」= 可自定义发送通道 → 同步修正 M2 发送链设计（设计稿 §7）+ 代码/测试
5. 全量回归（6 套件）+ E2E 四剧本 → 提交

## B. M3 最小可用版

6. HR 面 API（`services/pipeline/app/hr_api.py` 新 router）：
   overview / candidates / 两级详情（预签名 ≤15min）/ review 复核推翻（OVERRIDE+ReviewOverride）/
   rerun 手动重跑 / daily 日报 + pipeline 测试
7. `apps/console`（Next.js App Router + TS + Tailwind；pnpm workspace 已备）：
   `/` 漏斗+日报+告警条；`/candidates` 列表+详情抽屉（截图/JSON/理由/PDF/复核/重跑）；
   rewrites 代理 8000 免 CORS；`pnpm build` 通过 + curl 冒烟

## C. M4 可观测最小闭环

8. 人工队列：`GET /api/hr/manual-queue` + `POST .../{task_id}/ack` → console `/manual`
9. 告警：`GET /api/hr/alerts`（登录态/熔断/配额/今日转人工与风控计数）→ 首页告警条
10. 阈值回流：`PATCH /api/hr/jobs/{id}`（llm_threshold）+ 日报通过率/评分分布 → 首页调参面板

## D. 收尾

11. 全量回归 + E2E → 分块提交
12. 定时任务改为「验证轮 + 首批直发」；常驻部署（待 FDA「桌面文件夹」授权就绪——python3.12
    已于 2026-10-07 00:42 获得，直接重装即可验证）
13. 台账/文档更新 + 汇报

## 前置与依赖

- 账号：静置期（第三次风控：2026-10-07 检出，建议 ≥48h 零操作）结束 + 用户完成安全验证
- 常驻：`scripts/deploy/install.sh`（修复版）重装后，四服务应即起（python3.12 已获桌面目录授权）
- 环境：Docker（Redis/MinIO）+ MySQL 常驻

## 进度更新（2026-10-07 中午）

- A1 done：install.sh 多字节 bug 已修（`${label}`）
- A2/A3 done：第二轮探针完成 + **real `list_recommended(limit)` 已实装**（契约 limit 透传、
  fake 对齐、executor `_recommend_limit`、单测 +2）；307 单测全绿
- A4 部分：设计稿 §7 已更新校准结论；「立即沟通」语义测试与推荐人 read/send 链适配
  留待静置期后（用户已授权点一次）
- 其余（M3/M4/console）未动，待账号静置与工程排期

## 进度更新（2026-10-07 下午）——B/C/D 完成

- B/C done：hr_api.py（M3 全端点 + M4 manual-queue/alerts/阈值）+ 9 测试
- D done：apps/hr-console 三页 + 构建 + 冒烟（真实数据对账）
- A 补充：推荐人 read 回退已实装（预览页定位+字段提取+单测）——真实端到端仍待静置后
- 回归：323 单测 + E2E 四剧本全绿
- 剩：账号侧三件（立即沟通语义 / 端到端验证 / 首批直发）——按静置纪律排队
