#!/bin/bash
# 安装 launchd 常驻守护（四服务：pipeline / screening / scheduler / worker）。
#
# 2026-10-07 修订：plist 直连 venv 解释器（不经 /bin/bash）。
#   背景：launchd 拉起的 bash 无「桌面文件夹」访问权（TCC），四服务 exit 126；
#   直连解释器后只需给解释器一次性「完全磁盘访问权限」（拖入 python3.12），
#   TCC 归属判定对象=被执行的二进制，路径收敛且稳定。
#   run_service.sh 保留供手动启动/调试用。
#
# 前置：Docker（redis/minio）与 MySQL 已运行；.env 已配好（根目录）；
#       解释器已获 FDA（否则服务 exit 126，日志含 "Operation not permitted"）。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"
PY="$REPO/.venv/bin/python"
PY_REAL="$(python3 -c "import os;print(os.path.realpath('$PY'))")"
mkdir -p "$AGENTS_DIR" "$REPO/.run/logs"

# 通用环境变量（plist EnvironmentVariables）
env_common() {
  cat <<EOF
    <key>NO_PROXY</key><string>127.0.0.1,localhost</string>
    <key>no_proxy</key><string>127.0.0.1,localhost</string>
    <key>PYTHONUTF8</key><string>1</string>
    <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>DATABASE_URL</key><string>mysql+pymysql://hr_user:hr_dev_pw@127.0.0.1:3306/hr_workbuddy</string>
    <key>REDIS_URL</key><string>redis://127.0.0.1:6379/0</string>
    <key>PIPELINE_URL</key><string>http://127.0.0.1:8000</string>
    <key>SCREENING_URL</key><string>http://127.0.0.1:8001</string>
    <key>MINIO_ENDPOINT</key><string>127.0.0.1:9000</string>
    <key>MINIO_ACCESS_KEY</key><string>minioadmin</string>
    <key>MINIO_SECRET_KEY</key><string>minioadmin</string>
    <key>MINIO_BUCKET</key><string>hr-workbuddy</string>
EOF
}

write_plist() {
  # $1 = label 后缀名（也是日志名）；其余 = ProgramArguments
  local name="$1"; shift
  local label="com.hr-workbuddy.$name"
  local plist="$AGENTS_DIR/$label.plist"
  local extra_env=""
  case "$name" in
    scheduler) extra_env="    <key>PYTHONPATH</key><string>$REPO/services/scheduler</string>" ;;
    worker)
      extra_env="    <key>PYTHONPATH</key><string>$REPO/services/cua-agent</string>
    <key>CUA_DRIVER_MODE</key><string>real</string>
    <key>CUA_TASK_GAP_SECONDS</key><string>30</string>"
      ;;
  esac
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0">'
    echo '<dict>'
    echo "  <key>Label</key><string>$label</string>"
    echo '  <key>ProgramArguments</key>'
    echo '  <array>'
    for arg in "$@"; do
      echo "    <string>$arg</string>"
    done
    echo '  </array>'
    echo "  <key>WorkingDirectory</key><string>$REPO</string>"
    echo '  <key>EnvironmentVariables</key>'
    echo '  <dict>'
    env_common
    [ -n "$extra_env" ] && echo "$extra_env"
    echo '  </dict>'
    echo '  <key>RunAtLoad</key><true/>'
    echo '  <key>KeepAlive</key><true/>'
    echo '  <key>ThrottleInterval</key><integer>15</integer>'
    echo "  <key>StandardOutPath</key><string>$REPO/.run/logs/$name.out.log</string>"
    echo "  <key>StandardErrorPath</key><string>$REPO/.run/logs/$name.err.log</string>"
    echo '</dict>'
    echo '</plist>'
  } > "$plist"
  launchctl bootout "gui/$UID_NUM" "$plist" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_NUM" "$plist"
  echo "已安装并启动：${label}（解释器直连）"
}

write_plist pipeline  "$PY" -m uvicorn app.main:app --app-dir "$REPO/services/pipeline"  --host 127.0.0.1 --port 8000 --log-level info
write_plist screening "$PY" -m uvicorn app.main:app --app-dir "$REPO/services/screening" --host 127.0.0.1 --port 8001 --log-level info
write_plist scheduler "$PY" -m app.main
write_plist worker    "$PY" -m arq app.worker.WorkerSettings

echo
echo "解释器：$PY"
echo "真实二进制（FDA 授权对象）：$PY_REAL"
echo "若服务日志出现 Operation not permitted：将该二进制拖入"
echo "「系统设置 → 隐私与安全性 → 完全磁盘访问权限」并开启。"
echo "查看状态：launchctl list | grep hr-workbuddy"
echo "日志目录：$REPO/.run/logs/"
echo "注意：worker 依赖交互式桌面会话（Chrome 窗口）——锁屏/注销期间任务转人工，登录后自动恢复。"
