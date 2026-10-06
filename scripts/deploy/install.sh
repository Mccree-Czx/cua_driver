#!/bin/bash
# 安装 launchd 常驻守护（四服务：pipeline / screening / scheduler / worker）。
# - KeepAlive：崩溃自动拉起；RunAtLoad：登录/加载即启动
# - 日志：$REPO/.run/logs/<service>.out.log / .err.log
# 前置：Docker（redis/minio）与 MySQL 已运行；.env 已配好（根目录）。
# 卸载：uninstall.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"
mkdir -p "$AGENTS_DIR" "$REPO/.run/logs"

for svc in pipeline screening scheduler worker; do
  label="com.hr-workbuddy.$svc"
  plist="$AGENTS_DIR/$label.plist"
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$REPO/scripts/deploy/run_service.sh</string>
    <string>$svc</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$REPO/.run/logs/$svc.out.log</string>
  <key>StandardErrorPath</key><string>$REPO/.run/logs/$svc.err.log</string>
</dict>
</plist>
PLIST
  launchctl bootout "gui/$UID_NUM" "$plist" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_NUM" "$plist"
  echo "已安装并启动：$label"
done

echo
echo "查看状态：launchctl print gui/$UID_NUM/com.hr-workbuddy.pipeline | head -20"
echo "日志目录：$REPO/.run/logs/"
echo "注意：worker 依赖交互式桌面会话（Chrome 窗口）——锁屏/注销期间任务会转人工，登录后自动恢复。"
