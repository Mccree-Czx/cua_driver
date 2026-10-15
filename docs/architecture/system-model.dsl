workspace "hr_workbuddy · 猎聘 HR 招聘自动化" "Pi 内核 + 确定性工具链操作真实安卓手机完成猎聘招聘闭环（当前态：main @ 4a7f939，2026-10-14 建模）" {

    model {

        hrOps = person "HR 运营" "系统的唯一人类用户：在 OutIn HR Portal 复核候选人、处理转人工队列、维护岗位评分偏好"

        scheduler = softwareSystem "Windows 任务计划程序" "外部调度：hr-pi-main 每 2 分钟、hr-pi-daily 每天 19:50 触发 run_round.py" "External"

        piCli = softwareSystem "Pi 内核（pi-coding-agent CLI）" "外部 AI 代理运行时（配置在 ~/.pi/agent）；DeepSeek v4 flash；唯一决策者与编排者" "External"

        mobileMcp = softwareSystem "mobile-mcp（外部 MCP 服务）" "npx mobile-mcp；经 ADB 控制安卓设备，9 个手机工具（dump UI / tap / type / screenshot / …）" "External"

        liepinApp = softwareSystem "猎聘 App 企业版（com.lietou.mishu）" "真实业务平台：全部读取与触达都发生在手机端 App" "External"

        deepseek = softwareSystem "DeepSeek API" "Pi 推理（v4 flash）+ 简历评分 LLM" "External"

        hrw = softwareSystem "hr_workbuddy 系统" "确定性数据层 + 运营面（Python 3.12 uv workspace / Next.js）" {

            runner = container "轮次运行器 run_round.py" "组装系统提示 + 轮次指令 → pi -p；工作窗口门 / 全局轮次锁 / 空轮退避 / token 落账" "Python"

            hrTools = container "hr-tools MCP server" "26 个确定性工具：建档 / 状态机 / 一人一消息 / 评分 / 风控闸门 / gated_tap / 附件归档 / 成本落账（stdio）" "Python · MCP"

            pipeline = container "pipeline 服务" "FastAPI :8000；MySQL/MinIO 唯一写入口；Alembic schema owner；/api/jobs + /api/hr/*" "FastAPI · SQLAlchemy 2.0"

            screening = container "screening 服务" "FastAPI :8001；硬规则短路 → LLM 评星（1-5 星）+ LLM 不可用降级重判" "FastAPI"

            console = container "hr-console（OutIn HR Portal）" "漏斗总览 / 候选人 / 评分 / 日志 / 人工队列 / 告警条" "Next.js · TypeScript · antd"

            mysql = container "MySQL 8.4" "jobs / candidates / job_candidate / interactions / review_overrides / task_logs（11 态状态机）" "MySQL"

            minio = container "MinIO" "在线简历快照 / 简历 HTML / 附件 PDF 归档；预签名 URL ≤15min" "对象存储"

            redis = container "Redis 7" "触达令牌桶 + 风控熔断键 cua:risk:paused + 派发登记（遗留兼容）" "Redis"
        }

        scheduler -> runner "定时触发（hr-pi-main 每 2 分钟 / hr-pi-daily 19:50）"
        runner -> piCli "启动轮次（pi -p + system-prompt/rounds）"
        piCli -> mobileMcp "手机感知与操作（MCP）"
        piCli -> hrTools "状态/数据/闸门调用（MCP）"
        mobileMcp -> liepinApp "ADB：dump UI / tap / type / screenshot"
        hrTools -> liepinApp "gated_tap 服务端闸门通过后直发 adb 点击"
        hrTools -> screening "POST /screen 评分"
        hrTools -> mysql "SQLAlchemy 读写"
        hrTools -> minio "归档 put / 预签名"
        hrTools -> redis "令牌桶 / 风控键"
        hrTools -> pipeline "复用同仓模块（models/state_machine/messaging/db，import 非 HTTP）"
        hrOps -> console "使用"
        console -> pipeline "GET /api/hr/*（只读）+ review / ack / PATCH jobs"
        pipeline -> mysql "读写（唯一写入口）"
        pipeline -> minio "预签名 / 归档"
        screening -> deepseek "评分调用"
        piCli -> deepseek "推理"
    }

    views {

        systemContext hrw "SystemContext" {
            include *
            autoLayout lr
        }

        container hrw "Containers" {
            include *
            autoLayout tb
        }
    }
}
