# 猎聘 HR 辅助招聘系统 — Spec 方案 v1.6

> 版本：v1.6（2026-10-04，grill-me 第七轮修订）
> 基线：v1.5（2026-10-04）
> 定位：以 CUA（Computer-Use Agent）驱动猎聘企业版后台的自动化招聘助理，覆盖「主动咨询」与「系统推荐」双路径，实现在线简历筛选 → 自动索要简历 → PDF 归档 MinIO → 候选人关系落 MySQL → HR 工作台消费的全链路闭环。
> v1.6 修订要点：**技术栈定案（决策 11）**——纯 Python 后端全链（FastAPI/APScheduler/pydantic/Cua Driver SDK）+ Next.js 前端 + monorepo 仓库形态；明确否决 Java 与混合架构（实施团队无 Java 约束）。

---

## 1. 已决决策汇总（v1.1 更新）

| # | 分支 | 决策 | 变更 |
|---|---|---|---|
| 1 | 平台接入 | 猎聘对企业 HR 侧无官方 API，采用 **CUA 驱动**操作猎聘企业版后台 | 维持 |
| 1a | CUA 引擎选型 | **Cua Driver（trycua/cua，开源，本机后台驱动）做"手和眼"**——accessibility/CDP 快照 + 动作执行，不抢鼠标焦点；**"大脑"走云端视觉/规划 LLM API**。本机无独显（Intel Arc 核显 2GB），自部署视觉模型（UI-TARS 等）物理不可行，该路线已否决。CDP/辅助快照仅本地辅助定位，不外传，不视为违反"CUA 不碰 DOM"的初衷 | **新增** |
| 2 | 筛选逻辑 | **两层漏斗**：硬规则（学历/年限/城市/薪资/排除项，随 JD 配置生成）→ LLM 结构化评分（0-100，阈值默认 70 可调），判定理由落库，支持人工复核推翻 | 维持 |
| 2a | 动态阈值 | 调度器按**当日消息配额与 100 人打招呼下限双向调节 LLM 评分阈值**：通过人数不足 100 时下调阈值保下限，接近配额上限时上调阈值防超配；上下限之间配额优先花在最高分候选人；岗位配置的静态阈值是默认值 | 修正（v1.4 改为双向） |
| 3 | 交互边界 | **一个候选人终身只发 1 条消息**：打招呼与索要简历合并为单条模板消息，**无催促、无追发**——发送后 72h 无回复直接 `no_response` 关闭。话术 = 固定模板 + 变量填充（随 JD 配置，1-2 套）；全局限频 ≤20 条/小时 + 动作间随机延时（触达 10-60s，读操作 5-15s）；不通过者零触达 | 修正（v1.4 删除催促） |
| 4 | 数据存储 | 候选人以猎聘 userId 唯一去重；简历文件规范化重命名（`姓名_岗位_日期.pdf`，重名加时间戳）；MySQL 存 MinIO 对象键不存二进制；**在线简历截图同步归档 MinIO**（初筛证据，见决策 4a） | 微调 |
| 4a | 两级候选人视图 | **初筛**阶段：CUA 读在线简历时截取全页截图存 MinIO，HR 工作台详情页展示「截图 + 最小字段 JSON + 评分理由」，人工复核推翻有原始证据、LLM 提取错误可回溯；**二次筛选**阶段（候选人回传 PDF 后）：展示 PDF 在线预览。截图与最小字段快照同级合规口径（仅本地存储） | **新增** |
| 5 | 调度机制 | 定时批量巡检（工作窗口内密集轮询）+ 手动"立即运行"；浏览器 profile 持久化；掉登录自动暂停 + 通知人工扫码 | 微调（见 §5 吞吐约束） |
| 6 | 交付端 | HR 工作台网页：岗位漏斗视图、匹配分与理由、**两级候选人详情（初筛=截图+JSON+理由；二次筛选=PDF 在线预览，预签名 URL ≤15 分钟）**、人工复核推翻、手动重跑、**每日吞吐日报** | 微调 |
| 7 | 吞吐策略 | **唯一下限：每天 ≥100 人打招呼（outbound）**。查看量不设下限，是派生需求：100 ÷ 通过率 ≈ 285-670 次/天（通过率 15-35%），由动态阈值（决策 2a）调节。两笔账：(a) 100 条消息 + inbound 回复共享 ≤20 条/小时限频，窗口需 ≥5h 纯触达时间；(b) 账号套餐**推荐人触达次数 ≥100/天**是硬前提（实施前确认，§7）；查看额度按 100/通过率 估算，量级数百，不再是固定门槛。M2 验收产出真实索要→回传转化率，作为是否评估多子账号的依据 | 修正 |
| 8 | 交付范围 | **全链路系统**，按 M1→M4 推进，M1 先跑通单岗位端到端 | **新增** |
| 9 | 数据底座 | 本机 **Docker Desktop（WSL2 后端）**跑 MySQL 8 + MinIO 容器；代码层做存储抽象（SQL/对象存储接口），保留未来迁移余地 | **新增** |
| 10 | 合规立场 | 使用者知晓并接受猎聘协议封号风险（专门子账号承担）；简历数据仅本地存储不外传；修正 v1.0 内部矛盾——**在线简历只存判定所需最小字段快照**（姓名、userId、学历、年限、城市、薪资、经历摘要），与"仅存主动发送附件"的承诺对齐 | **新增** |
| 11 | 技术栈 | **纯 Python 后端全链（Python 3.12）**：pipeline=FastAPI + SQLAlchemy 2.0 + Alembic + minio SDK；scheduler=APScheduler + Redis(arq) 令牌桶限频；screening=pydantic + JSON Schema 强约束；cua-agent=Cua Driver Python SDK + OpenAI 兼容协议调云端视觉 LLM（便于换供应商）。**前端例外**：hr-console 用 Next.js + TypeScript + Tailwind + shadcn/ui（"纯 Python"指后端与 CUA 全链，前端不硬凹 Python）。仓库形态：**monorepo**（`apps/console` + `services/{pipeline,screening,scheduler,cua-agent}` + `infra/docker-compose.yml`）。测试：pytest + Playwright。已否决：Java/混合架构（实施团队无 Java 约束）、Streamlit 工作台（M3 交互复杂度不够）、K8s（单机 Compose 足够） | **新增** |

---

## 2. 系统架构

```
┌─────────────────────────────────────────────────────┐
│              调度器 (APScheduler / cron)              │
│     工作窗口 10-12h 内密集轮询 · 手动触发 · 限频队列     │
└──────────────┬──────────────────────────────────────┘
               │ 生成原子任务
┌──────────────▼──────────────────────────────────────┐
│           CUA 执行层                                  │
│  Cua Driver(本机,手和眼:快照/点击/输入/下载)            │
│    + 云端视觉/规划 LLM API(大脑:任务规划/成功判据校验)   │
│  原子任务 + 截图校验 + 重试≤3次 + 转人工队列            │
│  浏览器 profile 持久化 · 随机延时 · 全局限频            │
└──────────────┬──────────────────────────────────────┘
               │ 提取的结构化数据（JSON）
┌──────────────▼──────────────────────────────────────┐
│              业务逻辑层（普通代码）                     │
│  硬规则过滤 → LLM 评分 → 状态机推进 → 话术渲染          │
└──────┬───────────────────────────────┬──────────────┘
       ▼                               ▼
┌─────────────┐                ┌──────────────┐
│ MySQL 8      │                │ MinIO        │
│ (Docker)     │                │ (Docker)     │
│ 候选人/流程/  │                │ 简历 PDF 归档 │
│ 交互流水     │                │ resumes/...  │
└──────┬───────┘                └──────┬───────┘
       └──────────────┬───────────────┘
                      ▼
              ┌───────────────┐
              │  HR 工作台网页  │
              │ 漏斗/预览/复核  │
              └───────────────┘
```

**模块划分**（技术选型以决策 11 为准）

| 模块 | 职责 | 技术选型（v1.6 定案） |
|---|---|---|
| `liepin-cua-driver` | 原子任务执行：读未读消息、读在线简历、拉推荐人列表、发消息、下载附件 | **Cua Driver 0.30.x（宿主机常驻，需桌面会话）+ Python SDK 编排 + 云端视觉 LLM（OpenAI 兼容协议）**；不放进 Docker（需访问真实桌面浏览器） |
| `scheduler` | 定时巡检编排、任务队列、限频与延时控制、登录态健康检查 | APScheduler + Redis（arq 队列，令牌桶限频，Docker）；工作窗口可配置（默认 08:00-20:00） |
| `screening-engine` | 硬规则过滤 + LLM 评分 + 判定理由生成 | Python 服务，pydantic + JSON Schema 强约束输出（评分用文本模型即可，无需视觉） |
| `pipeline-service` | 状态机推进、话术渲染、去重、MinIO 上传、MySQL 读写 | FastAPI + SQLAlchemy 2.0 + Alembic + minio SDK（Docker） |
| `hr-console` | 工作台前端：漏斗视图、PDF 预览（预签名 URL ≤15min）、复核操作 | Next.js + TypeScript + Tailwind + shadcn/ui（Docker）+ 与工作台同网段访问 MinIO |

**部署形态**：全部服务跑在一台 Windows 主机上——Docker Desktop 容器（MySQL/MinIO/Redis/pipeline/console/scheduler）+ 宿主机进程（Cua Driver + 浏览器）。CUA 与浏览器必须在宿主机桌面会话中，这是单容器化方案的唯一例外。

**仓库形态（monorepo）**：
```
hr-workbuddy/
├── apps/console/                # Next.js 工作台
├── services/
│   ├── pipeline/                # FastAPI 业务服务
│   ├── screening/               # 筛选引擎
│   ├── scheduler/               # 调度器
│   └── cua-agent/               # CUA 执行层（宿主机运行）
├── infra/docker-compose.yml     # MySQL/MinIO/Redis + 各服务
└── packages/contracts/          # 共享 pydantic 模型（原子任务/候选人 JSON 契约）
```

---

## 3. 核心流程

### 路径一：主动咨询者（inbound）

1. CUA 打开猎聘企业版后台消息列表，筛选未读会话。
2. 逐个打开会话，读取对方**在线简历**：(a) **截取全页截图** → 上传 MinIO（`snapshots/...`）；(b) 视觉提取为结构化 JSON（姓名、userId、学历、工作年限、城市、薪资、经历摘要——**仅此最小字段集**，见决策 10）。截图与 JSON 一并落库，作为初筛详情与复核证据。
3. 硬规则过滤 → 不通过：标记 `rejected_hard`，**不回复**。
4. 通过者 → LLM 评分 → 低于阈值：标记 `rejected_llm`，不回复。
5. 通过者 → 渲染岗位话术模板 → CUA 发送索要简历消息 → 状态 `resume_requested`。
6. 后续巡检检查 `awaiting_resume` 会话：收到 PDF 附件 → CUA 下载 → 规范化重命名 → 上传 MinIO → MySQL 写入候选人 × MinIO 对象键关系 → 状态 `resume_received`。
7. **72h 无附件 → 直接 `no_response` 关闭（不催促、不追发，决策 3）。**

### 路径二：系统推荐人（outbound，必须项）

1. CUA 打开猎聘"推荐/智能匹配"页面，按在招岗位拉取推荐候选人列表。
2. 逐个打开在线简历（必读项），同样执行**截图归档 + 最小字段提取**，再同路径一第 3-4 步两层判定。
3. 通过者 → CUA 执行**打招呼 + 索要简历**（模板话术）→ 状态 `greeted → resume_requested`，进入与路径一相同的 `awaiting_resume` 等待与归档流程。
4. 推荐人有平台触达次数成本，未通过判定者零触达。

### 吞吐容量估算（v1.5：唯一下限 ≥100 打招呼/天）

| 环节 | 量级/天 | 耗时/资源 | 约束判断 |
|---|---|---|---|
| 读在线简历（含截图归档） | 派生需求：100 ÷ 通过率 ≈ **285-670 次** | 5-15s/次 → 0.4-2.8h | 无固定下限；查看额度按此量级确认即可 |
| 两层漏斗判定 | 同查看量 | 本地代码 + LLM API，分钟级 | 无瓶颈 |
| outbound 打招呼+索要（单条，决策 3） | **≥100 条（唯一下限）** | 限频 20 条/小时 → ≥5h 触达窗口 | 动态阈值（决策 2a）保下限、防超配 |
| inbound 索要回复 | 视咨询量 | 与 outbound 共享 ≤20 条/小时限频 | 每个候选人同样只发 1 条；与 outbound 合计 ≤240 条/天 |
| 简历回收 | 不设指标 | — | M2 验收产出真实转化率 |

**推论**：单账号串行从容成立——触达 ≥5h 是主轴，查看穿插在限频等待间隙；"一人一消息"原则使消息总量天然可控（无催促流量），240 条/天上限留给 inbound 的余地充裕。**推荐人触达次数 ≥100/天是唯一平台侧硬前提**。

---

## 4. 数据模型（MySQL）

**`jobs`**：id, title, jd_text, hard_rules(JSON), template_msgs(JSON), llm_threshold(默认70), status, created_at

**`candidates`**：id, **liepin_user_id UNIQUE**, name, online_resume_minimal(JSON，仅最小字段快照), **snapshot_object_key(在线简历全页截图 → MinIO)**, source ENUM(inbound, recommended), created_at, updated_at

**`job_candidate`**：id, job_id, candidate_id, match_score, judge_reason, status, minio_object_key, resume_downloaded_at, last_touch_at, created_at
　UNIQUE(job_id, candidate_id)

**`interactions`**：id, job_candidate_id, direction ENUM(out, in), msg_type ENUM(greet_request, reply, attachment), content, sent_at
　注：outbound 方向每行 job_candidate 至多 1 条 out 消息（决策 3，一人一消息），greet_request 即合并后的打招呼+索要

**`review_overrides`**：id, job_candidate_id, old_status, new_status, operator, reason, created_at（人工复核流水，回流调阈值）

**状态机（v1.1 分路径修正）**：

```
inbound:   new → screened_pass | rejected_hard | rejected_llm
           screened_pass → resume_requested → awaiting_resume
           awaiting_resume → resume_received | no_response → closed

outbound:  new → screened_pass | rejected_hard | rejected_llm
           screened_pass → greeted → resume_requested → awaiting_resume
           awaiting_resume → resume_received | no_response → closed

resume_received → hr_reviewed → closed   (工作台复核后段, v1.1 补齐)
```

注：`greeted` 仅存在于 outbound（inbound 是对方先开口）；复核推翻可跨任意非终态，写入 `review_overrides` 并直接改 status。

**MinIO 对象键**：
- PDF 简历：`resumes/{job_id}/{liepin_user_id}/{姓名}_{岗位}_{日期}[_{timestamp}].pdf`
- 在线简历截图：`snapshots/{liepin_user_id}/{日期}[_{timestamp}].png`（同一人重复截图保留历史，详情页默认展示最新）

---

## 5. 风控与容错设计

| 风险 | 对策 |
|---|---|
| 账号风控/封号 | 专门招聘子账号跑机器人；触达限频 ≤20 条/小时；触达动作间 10-60s、读操作间 5-15s 随机延时；工作窗口 10-12h 而非常驻；"一人一消息"天然降低骚扰画像；高频浏览风险由使用者知晓并接受（决策 10） |
| 平台额度截断 | 实施前确认：推荐人触达次数 ≥100/天（唯一硬前提）、每日简历查看额度 ≥100/通过率（约 285-670）、消息发送限频；调度器每日对账实际额度消耗，触及上限即停并告警，不硬闯 |
| 登录态失效 | profile 持久化；每轮开始检测登录态，失效即暂停全部任务并通知人工扫码恢复 |
| CUA 操作失误 | 原子任务 + 明确成功判据 + 操作后截图校验（云端视觉模型复核）；失败重试 ≤3 次转人工队列 |
| 猎聘改版 | Cua Driver 走 accessibility/CDP 快照 + 视觉双通道，不依赖硬编码选择器；原子任务判据失效时告警 |
| 简历合规 | 仅存储候选人主动发送的附件 PDF；在线简历仅存判定最小字段快照 + 全页截图（决策 10/4a，用途限于初筛判定与人工复核证据）；话术模板零承诺性表述；数据全部本地存储不外传 |
| CUA 大脑 API 不可用 | 云端 LLM 故障时任务进入暂停队列，不盲操作；读操作可降级为纯快照落盘延后判定 |
| 成本失控 | 云端视觉/规划调用按任务计费，调度器记录每轮 token 用量，日预算超阈值自动降频并告警 |

---

## 6. 里程碑

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| M1 | Cua Driver 驱动 + 登录态 + 路径一全链路（读消息→筛选→索要→下载→入库） | 单岗位端到端跑通，PDF 落 MinIO、关系落 MySQL |
| M2 | 路径二（推荐人）+ 限频/延时策略 + 吞吐爬坡至 ≥100 打招呼/天 | 双路径并行稳定运行 1 周；**产出真实索要→回传转化率** |
| M3 | HR 工作台（漏斗、**两级候选人详情**、复核、日报） | HR 可在工作台完成全部日常操作：初筛看截图+评分理由、二次筛选看 PDF、看日报 |
| M4 | 人工队列、阈值回流、监控告警（含 token 成本） | 异常可观测、判定质量可持续调优 |

---

## 7. 待实施前确认项（实施方落实，不阻塞设计）

1. 猎聘企业版子账号准备与登录方式（扫码/Cookie）。
2. **【关键前置】账号套餐额度确认：推荐人触达次数 ≥100 次/天（唯一硬前提）、每日简历查看 ≥100/通过率（约 285-670 次）——不达标则 100 打招呼下限不成立，需升套餐或降目标。**
3. 云端 LLM 供应商与模型选型：**两个用途分开选**——(a) CUA 大脑需视觉/规划能力（截图理解+动作决策）；(b) 简历评分用文本模型即可。量级：CUA 大脑约 285-670 读 + 100-240 写任务/天，评分同查看量。
4. Docker Desktop（WSL2）安装与资源分配（建议 ≥8GB 内存划给 WSL2）。
5. Cua Driver 宿主机常驻方式（已装 0.30.4，`-NoAutoStart` 安装；如需开机自启另行注册；遥测可用 `cua-driver telemetry disable` 关闭）。
6. 工作台访问入口：本机 localhost 或内网穿透（预签名 URL 需与访问入口同域可达）。

---

## 附：v1.5 → v1.6 变更记录

1. **决策 11（新增）技术栈定案**：纯 Python 后端全链（Python 3.12；FastAPI/SQLAlchemy 2.0/Alembic、APScheduler + Redis arq 令牌桶、pydantic + JSON Schema、Cua Driver Python SDK + OpenAI 兼容协议）；前端例外为 Next.js + TS + Tailwind + shadcn/ui；monorepo 仓库形态（含 `packages/contracts` 共享 pydantic 契约）；测试 pytest + Playwright。已否决并记录：Java/混合架构、Streamlit 工作台、K8s。
2. **§2 模块划分表与仓库结构同步更新**。

## 附：v1.4 → v1.5 变更记录

1. **打招呼下限 150 → 100**：唯一下限调整为 **≥100 人打招呼/天**；派生数字全部重算——查看量 ≈285-670 次/天、触达窗口 ≥5h、平台硬前提 ≥100 次/天、LLM 调用量级同步更新。决策 2a/7、容量估算表、风控表、M2、§7 共 7 处同步修订。

## 附：v1.3 → v1.4 变更记录

1. **决策 7 修正**：取消 1000 次查看/天下限——查看量改为派生需求（150 ÷ 通过率 ≈ 430-1000 次/天）；唯一下限保留 **≥150 人打招呼/天**；平台硬前提从三项收敛为一项（推荐人触达次数 ≥150/天）。
2. **决策 3 重大修正——"一人一消息"原则**：每个候选人终身只发 1 条消息（打招呼+索要合并单条，v1.3 已合并、v1.4 进一步**删除 72h 催促**），72h 无回复直接 `no_response` 关闭。副作用全部是正面的：消息总量天然可控、骚扰画像降低、状态机简化。
3. **决策 2a 修正**：动态阈值从"只上不下防超配"改为**双向调节**——通过人数不足 150 时下调阈值保下限，接近配额时上调防超配。
4. **数据模型**：`interactions.msg_type` 枚举删除 `remind`，合并 `greet`+`request` 为 `greet_request`；注明每 job_candidate 至多 1 条 out 消息。
5. **流程/风控/里程碑同步**：inbound 第 7 步删除催促；风控表"一人一消息降低骚扰画像"；M2 删除催促策略。

## 附：v1.2 → v1.3 变更记录

1. **决策 7 再修正**：从"无硬性指标"改为双下限——**每天 ≥1000 次在线简历查看 + ≥150 人 outbound 打招呼**；明确三项平台侧额度为硬前提（查看 ≥1000/天、触达 ≥150/天、消息限频），列入 §7 关键前置与风控表"平台额度截断"行。
2. **决策 2a（新增）动态阈值**：按当日剩余消息配额自动上调 LLM 评分阈值（只上不下），保证 1000 查看下通过率收在 ≥15% 且不爆 240 条/天消息上限，配额优先给最高分候选人。
3. **决策 3 修正**：outbound 打招呼与索要**合并为单条模板消息**——拆两条则 150×2=300 条 > 240 条/天上限，预算不成立。
4. **容量估算表重写**：含双下限的满载账——单账号串行成立但接近满载（查看 1.4-4.2h 穿插 + 触达 ≥7.5h，窗口 10-12h）。
5. **§7 待确认项**：新增账号套餐三项额度确认为关键前置；LLM 调用量级更新为 1000 读/1000 评/天。

## 附：v1.1 → v1.2 变更记录

1. **决策 7 修正**：吞吐从硬性指标（400 筛/50 简历）改为"限频与风控约束内最大化"策略，原数字降级为容量估算参考；新增每日吞吐日报（决策 6）。
2. **决策 4a（新增）**：在线简历全页截图归档 MinIO；HR 工作台分两级候选人视图——初筛详情展示截图+最小字段 JSON+评分理由（复核证据），二次筛选（回传 PDF 后）展示 PDF 预览。
3. **数据模型**：`candidates` 表新增 `snapshot_object_key`；MinIO 新增 `snapshots/` 键前缀规范。
4. **流程**：inbound/outbound 读简历步骤均加入截图归档动作。
5. **里程碑**：M2 验收从"复核 ≥35% 假设"改为"产出真实转化率"；M3 验收覆盖两级详情视图与日报。

## 附：v1.0 → v1.1 变更记录

1. **决策 1a（新增）**：CUA 引擎定案为 Cua Driver + 云端 LLM 大脑；否决本机自部署视觉模型（无 GPU）。
2. **决策 7/8/9/10（新增）**：吞吐目标量化、全链路交付范围、Docker 数据底座、合规立场与最小字段快照。
3. **状态机修正**：分 inbound/outbound 双路径，`greeted` 归位 outbound；补齐 `resume_received → hr_reviewed → closed` 后段。
4. **§5 风控表**：新增云端 API 不可用降级、token 成本预算两条；延时策略按读/写分级。
5. **§3 新增吞吐预算表**：400 筛 → 50 简历的资源账，转化率假设列入 M2 验收。
6. **§6 预签名 URL**：有效期收敛至 ≤15 分钟。
7. **§7 待确认项**：LLM 选型拆分为 CUA 大脑/评分两用途；Docker、Cua Driver、工作台入口落地。
