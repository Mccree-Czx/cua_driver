# hr_workbuddy

猎聘 HR 招聘自动化系统（spec v1.6）——以 CUA（Computer-Use Agent）驱动猎聘企业版后台的自动化招聘助理：
在线简历筛选 → 自动索要简历 → PDF 归档 MinIO → 候选人关系落 MySQL → HR 工作台消费。

## 仓库结构

```
hr_workbuddy/
├── pyproject.toml          # uv workspace 根（dev 依赖 + pytest 配置）
├── infra/                  # 数据底座：docker-compose（MySQL/MinIO/Redis）+ 冒烟测试
├── packages/contracts/     # 共享 pydantic 契约（Task 2）
├── services/               # pipeline / screening / scheduler / cua-agent（Task 3-10）
├── tests/e2e/  scripts/    # M1 E2E 验收（Task 11）
```

## 快速开始（数据底座）

```bash
cp infra/.env.example infra/.env      # 可选：覆盖 compose 默认值
docker compose -f infra/docker-compose.yml up -d --wait
uv run pytest infra/tests -m infra    # 冒烟验证 MySQL / MinIO / Redis
```

应用级环境变量见根 `.env.example`；本仓库不含任何真实密钥。

## 技术栈（spec §7）

- 后端：Python 3.12 / FastAPI / SQLAlchemy 2.0 / Alembic / minio SDK / APScheduler + Redis
- 前端（M3）：Next.js + TypeScript
- 数据底座：Docker Desktop（WSL2 后端）跑 MySQL 8.4 + MinIO + Redis 7
- 包管理：uv workspace + pnpm（apps/*）
# cua_driver
