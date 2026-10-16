# BOSS 系统的常驻与调度

这套东西让 BOSS 那套**不用人管、自己跑**。分两头：

| 在哪 | 跑什么 | 怎么起 |
|---|---|---|
| **Windows（本机）** | 后端 `boss-pipeline`(8100) + 前端 `boss-console`(3200) | 两个计划任务，登录即启 |
| **Mac（10.99.31.81）** | 三个轮次：main / daily / login | 三个 launchd 任务 |

## Windows 侧

两个 `.cmd` 是**常驻包装**：挂了等 10 秒自己重拉。计划任务只是登录时把它们叫起来。

### 装

```powershell
schtasks /create /tn "BossPipeline" /tr "cmd /c \"<仓库>\scripts\boss\pipeline.cmd\"" /sc onlogon /rl highest /f
schtasks /create /tn "BossConsole"  /tr "cmd /c \"<仓库>\scripts\boss\console.cmd\""  /sc onlogon /rl highest /f
```

### 看 / 停

```powershell
schtasks /query /tn BossPipeline /v /fo list
schtasks /end   /tn BossPipeline
schtasks /run   /tn BossPipeline
```

日志在 `<仓库>\.run\logs\boss-{pipeline,console}.log`。

### ★ 两个脚本里都先查端口，那不是多余的

**旧进程还占着端口时，新进程会静默退出**——`EADDRINUSE` 只写进日志、
控制台什么都不说。于是你以为重启了，其实还在看旧的。

2026-10-16 一天里在 8100 和 3200 上各踩了一次，两次都表现为
「改了代码怎么没生效」。所以脚本启动前先 `netstat` 查，
占了就报出来、等一等，而不是闷头去撞。

### ★ 前端改了必须重新 build

`next start` 服务的是 `.next/` 里的**预构建产物**，`restart` 不重建。
改了 `apps/boss-console/` 之后要：

```powershell
corepack pnpm --filter boss-console build
```

`console.cmd` 只在**没有 build 产物**时自动 build 一次，不负责每次重建。
（猎聘那边 2026-10-15 因为这条丢掉了一整天的前端改动。）

### ★ 重启要用 `schtasks /end` + `/run`，别直接 taskkill 子进程

实测（2026-10-16）：为了加载新的前端构建，我 `taskkill /F` 掉了 `next start` 的 node 进程。
**结果后端（另一个任务）也跟着 `^C` 掉了**——它的日志停在 `^C` 上，端口不再监听。

两个包装脚本的进程树看起来是独立的（不同的父进程、不同的控制台），
所以**原因我没有查实**，只记现象。稳妥做法：

```powershell
schtasks /end /tn BossConsole
schtasks /run /tn BossConsole
```

`/end` 会让包装脚本整体退出，`/run` 再干净地拉起一个。

## Mac 侧

三个 plist 在 `~/Library/LaunchAgents/com.outin.boss-round-*.plist`。

| 任务 | 触发 | 跑什么 |
|---|---|---|
| `boss-round-main` | 每 **900** 秒 | `run_round.py main_round` |
| `boss-round-daily` | 每天 19:50 | `run_round.py daily_round` |
| `boss-round-login` | 每 **120** 秒 | `run_round.py login_round` |

### ★ 为什么是 900 秒而不是照抄猎聘的 300

一轮 `main_round` 实测 **415 秒**。按 300 秒排的话，每一轮都会撞上全局轮次锁
（`RoundLock`）然后被 `[skip]`——**看起来在跑，其实大半轮次什么都没做**。

### ★ 用的是系统 `python3`，不是 venv

猎聘那边必须用 `~/outin-hr/.venv/bin/python`（它要 redis）。
**BOSS 的 `run_round.py` 是纯标准库**，所以用系统 python3——
**venv 坏掉也不影响轮次**。这是刻意保持的性质，别为了图方便往里引 redis。

### 装 / 看 / 停

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.outin.boss-round-main.plist
launchctl list | grep boss-round
launchctl kickstart -k gui/$(id -u)/com.outin.boss-round-main    # 立刻跑一次
launchctl bootout   gui/$(id -u)/com.outin.boss-round-main       # 停
tail -f ~/outin-hr/.run/logs/boss-main.out.log
```

## 与猎聘的关系

**猎聘的三个 launchd 任务现在是停着的，而且要保持停着。**

原因：两套驱动的是**同一部手机**（`10.99.22.219`）。同时跑会互相抢屏——
一套把手机切到猎聘的页面，另一套就会把它当成「状态不对」抛 `FlowMismatch`，
然后两边一直互相打断。而 BOSS 一轮 415 秒 > 猎聘的 300 秒间隔，
**重叠是必然的，不是偶发**。

两套的库（`boss_workbuddy` / `hr_workbuddy`）、Redis db（1 / 0）、
端口（8100/3200 / 8000/3000）、launchd 前缀（`com.outin.boss-*` / `com.hr-workbuddy.*`）
全都错开。

## 已知的脆弱点

- **Mac 依赖本机的 IP**（现在 `10.99.22.99`）。IP 一变轮次就全连不上，
  而表现是「轮次照常跑、只是推不上去」。这个地址**只写在三个 plist 里**。
- **本机关机/睡眠就断**。这是「先本机跑通」的固有代价，上 197 才能根治。
- **自动打招呼仍然是关的**（`BOSS_GREET_ENABLED` 不设）。要开是单独一步。
