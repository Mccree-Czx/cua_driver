# hr_workbuddy（仓库目录名 `cua_driver`）

猎聘（Liepin）HR 招聘自动化系统（**B 方案**）：由外部 AI 代理 **Pi**（DeepSeek）经 MCP 工具操作一台**真实安卓手机**上的猎聘「招聘端」App（`com.lietou.mishu`），替 HR 完成「筛选候选人 → 打招呼 / 索要简历 → 收附件简历（真实 PDF）归档 → 状态记账」的招聘闭环；HR 在网页看板（OutIn HR Portal）上复核结果。

> 第一代架构（桌面 Chrome + `services/cua-agent` / `services/scheduler` / arq 队列 / HTTP 回调编排）已随 commit `aeacfe5`（2026-10-10「Pi 全权内核重构」）整体删除。`services/` 下现在**只有** `pipeline` 与 `screening` 两个服务。

## 四机分工

| 机器 | 职责 |
|---|---|
| **Mac**（10.99.31.81） | `agent/` 下的轮次脚本 + adb + **真机**；launchd 三个任务拉起 main（300s）/ 登录轮询（20s）/ daily（19:50）——**现停用**，真机让给了 BOSS |
| **云机执行端**（10.99.40.66） | 同一套 `agent/` 脚本，但驱动的是多多云里的**云手机**；启动与调度见 [`scripts/cloud/README.md`](scripts/cloud/README.md) |
| **CT104**（10.99.40.99） | MySQL / Redis / MinIO + pipeline(:8000) + screening(:8001) + hr-console(:3000)，systemd 常驻 |
| 开发机（Windows） | 编码、测试、git push；可本地起 compose 栈 + 服务做开发验证 |

CT104 连不到 Mac（Mac 在 NAT 后），两边只靠 **Redis** 见面（登录请求、配额桶、风控熔断）。

> 仓库里还有**第二套系统**（BOSS直聘，`agent_boss/` + `services/boss-pipeline` + `apps/boss-console`），
> 与猎聘共用同一部真机。两套的库、Redis db、端口全部刻意错开，见 [`CLAUDE.md`](CLAUDE.md)。

## 仓库结构

```
cua_driver/
├── agent/                  # Pi 侧：system-prompt.md / rounds.md（提示词即产品）
│                           #   run_round.py（轮次运行器）· login_watch.py（登录轮询器）
│                           #   flows.py / phone.py（确定性驱动与 adb 原语）· login_map.json（实测锚点）
├── services/pipeline/      # FastAPI :8000 + Alembic（schema 唯一 owner）+ hr-tools MCP（tools_mcp_server.py）
├── services/screening/     # FastAPI :8001：硬规则 → LLM 1-5 星评分 → degraded 降级
├── apps/hr-console/        # OutIn HR Portal（Next.js + antd）
├── packages/contracts/     # 共享契约（CandidateStatus 11 态 / MinimalResume / ScreenRequest…）
├── infra/                  # docker-compose（开发机数据底座；worlds/ 为第一代遗留 mock 剧本）
├── scripts/                # backup.py · hr_report.py · deploy/（launchd 安装脚本）
└── docs/                   # runbook.md（运维手册）+ architecture/（架构建模产物）
```

## 快速开始（开发机）

```bash
uv sync --all-packages --all-groups
docker compose -f infra/docker-compose.yml --profile docker-mysql up -d   # 开发机数据底座（MySQL 带 profile，裸 up 不启 MySQL）
uv run uvicorn app.main:app --app-dir services/pipeline --host 127.0.0.1 --port 8000
uv run uvicorn app.main:app --app-dir services/screening --host 127.0.0.1 --port 8001
cd apps/hr-console && corepack pnpm install && corepack pnpm dev   # :3000，/api/* 代理到 pipeline
```

环境变量见根 `.env.example`——**注意其中仍含一批第一代遗留键（`CUA_*` / `WORK_WINDOW_*` 等），当前无代码消费**；实际被消费的变量表见 `docs/runbook.md` §3.4。

## 权威资料

- `agent/system-prompt.md` + `agent/rounds.md` —— Pi 的实际提示词：工具清单、铁律、数据契约、各轮次指令（**行为以它们为准**）
- `docs/runbook.md` —— 运维手册（部署拓扑、轮次语义、登录/换号、常见故障表）
- `docs/architecture/` —— 架构理解报告 / C4 模型 / 业务闭环图 / 证据索引（成文于 2026-10-14；其中调度一节写的是 Windows 任务计划，已被 Mac launchd 取代）
- `AGENTS.md` —— 给 AI 编码助手的仓库指引

## 技术栈

- 后端：Python 3.12 / FastAPI / SQLAlchemy 2.0 / Alembic / minio SDK / Redis / MCP（stdio）
- 前端：Next.js 16 + React 19 + antd 6 + Tailwind 4（pnpm，走 corepack，版本由根 `package.json` 的 `packageManager` 固定）
- LLM：DeepSeek（`deepseek-flash`）——Pi 主循环 + screening 评分
- 移动端：mobile-mcp + adb（真机，WiFi 连接）
