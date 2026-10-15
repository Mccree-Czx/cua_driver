#!/bin/bash
# 安装 launchd 常驻守护（服务：pipeline / screening / backup）。
#
# 用法：
#   bash install.sh                 # 默认：pipeline screening backup
#   bash install.sh pipeline        # 只装某一个
#
# ★ 2026-10-15 修订：删掉了 scheduler 与 worker。
#   它们是**第一代架构**的常驻服务（APScheduler 编排 + arq worker + 桌面 CUA 驱动），
#   随 commit aeacfe5「Pi 全权内核重构」一起删掉了，`services/` 下现在只剩
#   pipeline 与 screening。这个脚本当时没跟着改，于是它会去装两个**不存在的服务**：
#   worker 跑的是 `arq app.worker.WorkerSettings`（模块早已不存在），
#   scheduler 的 PYTHONPATH 指向 `services/scheduler`（目录也没了）。
#   谁照着 README 跑一遍安装，就会得到两个反复重启失败的常驻进程。
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
  # scheduler / worker 两个分支随第一代架构一起删掉了（2026-10-15），见文件头说明。
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

write_backup_plist() {
  # 备份：每日 21:30 日历任务（非 KeepAlive；backup.py 自读 env_common 配置）
  local label="com.hr-workbuddy.backup"
  local plist="$AGENTS_DIR/$label.plist"
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0">'
    echo '<dict>'
    echo "  <key>Label</key><string>$label</string>"
    echo '  <key>ProgramArguments</key>'
    echo '  <array>'
    echo "    <string>$PY</string>"
    echo "    <string>$REPO/scripts/backup.py</string>"
    echo '  </array>'
    echo "  <key>WorkingDirectory</key><string>$REPO</string>"
    echo '  <key>EnvironmentVariables</key>'
    echo '  <dict>'
    env_common
    echo '  </dict>'
    echo '  <key>RunAtLoad</key><false/>'
    echo '  <key>StartCalendarInterval</key>'
    echo '  <dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>30</integer></dict>'
    echo "  <key>StandardOutPath</key><string>$REPO/.run/logs/backup.out.log</string>"
    echo "  <key>StandardErrorPath</key><string>$REPO/.run/logs/backup.err.log</string>"
    echo '</dict>'
    echo '</plist>'
  } > "$plist"
  launchctl bootout "gui/$UID_NUM" "$plist" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_NUM" "$plist"
  echo "已安装（每日 21:30）：${label}"
}

# —— 服务选择（分批安装）——
SERVICES=("$@")
if [ ${#SERVICES[@]} -eq 0 ]; then
  SERVICES=(pipeline screening backup)
fi

for svc in "${SERVICES[@]}"; do
  case "$svc" in
    pipeline)
      write_plist pipeline "$PY" -m uvicorn app.main:app --app-dir "$REPO/services/pipeline" --host 127.0.0.1 --port 8000 --log-level info
      ;;
    screening)
      write_plist screening "$PY" -m uvicorn app.main:app --app-dir "$REPO/services/screening" --host 127.0.0.1 --port 8001 --log-level info
      ;;
    backup)
      write_backup_plist
      ;;
    *)
      echo "未知服务：$svc（可选：pipeline screening backup）" >&2
      exit 1
      ;;
  esac
done

echo
echo "解释器：$PY"
echo "真实二进制（FDA 授权对象）：$PY_REAL"
echo "若服务日志出现 Operation not permitted：将该二进制拖入"
echo "「系统设置 → 隐私与安全性 → 完全磁盘访问权限」并开启。"
echo "查看状态：launchctl list | grep hr-workbuddy"
echo "日志目录：$REPO/.run/logs/"
