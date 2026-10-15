#!/usr/bin/env bash
# CT104 上的发布收尾：拉代码 → 按需迁移 → 按需重建前端 → 重启**受影响**的服务。
#
# 用法（在 CT104 上）：
#   bash scripts/deploy/deploy.sh
#
# ## 为什么需要它
#
# 2026-10-15 踩到的坑：发布流程是「`git pull` + `systemctl restart`」，
# 对后端够用，**对前端不够**。
#
# `hr-console` 是 systemd 里的 `next start`（`NODE_ENV=production`），
# 服务的是 `.next/` 里的**预构建产物**——`restart` 只重启进程，**不会重建**。
# 那天改的四处前端（下拉框改只读、删顶栏账号名、软删除 UI）全都躺在源码里没生效，
# 而线上一直发的是上午那份旧包，看起来就像「改动没做上去」或者「浏览器缓存」。
#
# 所以：**前端有改动就必须 `pnpm build`**。这个脚本把它变成自动的——
# 由 `git diff` 决定该做什么，而不是靠人记得。
#
# ## 顺带解决的
#
# alembic 的 `env_file=".env"` 是**按 CWD** 解析的：在别处跑会读到默认值
# （退化成 127.0.0.1 的库）。这里显式带上 `DATABASE_URL`，不靠 cd 碰运气。

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"

log() { printf '\n=== %s ===\n' "$*"; }

# ——— 1. 拉代码，同时记下「这次到底改了什么」 ———
BEFORE="$(git rev-parse HEAD)"
log "当前 $BEFORE"
git pull --ff-only
AFTER="$(git rev-parse HEAD)"

if [ "$BEFORE" = "$AFTER" ]; then
  log "没有新提交（$AFTER），无事可做"
  exit 0
fi

CHANGED="$(git diff --name-only "$BEFORE" "$AFTER")"
echo "$CHANGED" | sed 's/^/  /'

touched() { echo "$CHANGED" | grep -q "^$1" ; }

# ——— 2. 后端：迁移 + 重启 ———
if touched "services/pipeline/"; then
  log "hr-pipeline 有改动"
  # ★ 显式带 DATABASE_URL：env_file=".env" 按 CWD 解析，从 services/pipeline
  #   跑会读不到根目录的 .env，静默退化成默认连接串。
  DB_URL="$(grep '^DATABASE_URL=' "$REPO/.env" | cut -d= -f2-)"
  ( cd "$REPO/services/pipeline" && DATABASE_URL="$DB_URL" "$REPO/.venv/bin/python" -m alembic upgrade head )
  systemctl restart hr-pipeline
  sleep 3
  systemctl is-active hr-pipeline
fi

if touched "services/screening/"; then
  log "hr-screening 有改动"
  systemctl restart hr-screening
  sleep 3
  systemctl is-active hr-screening
fi

# ——— 3. 前端：**必须重建**，restart 不够 ———
if touched "apps/hr-console/"; then
  log "hr-console 有改动 —— 重新构建（restart 不重建，这一步不能省）"
  OLD_BUILD="$(cat "$REPO/apps/hr-console/.next/BUILD_ID" 2>/dev/null || echo '(无)')"
  ( cd "$REPO/apps/hr-console" && corepack pnpm build )
  NEW_BUILD="$(cat "$REPO/apps/hr-console/.next/BUILD_ID" 2>/dev/null || echo '(无)')"
  echo "  BUILD_ID: $OLD_BUILD → $NEW_BUILD"
  [ "$OLD_BUILD" != "$NEW_BUILD" ] || { echo "  ⚠ BUILD_ID 没变，构建可能没生效" >&2; }
  systemctl restart hr-console
  sleep 8
  systemctl is-active hr-console
fi

log "完成：$BEFORE → $AFTER"
