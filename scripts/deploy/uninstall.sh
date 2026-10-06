#!/bin/bash
# 卸载 launchd 常驻守护（四服务）。
set -euo pipefail

AGENTS_DIR="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"

for svc in pipeline screening scheduler worker; do
  label="com.hr-workbuddy.$svc"
  plist="$AGENTS_DIR/$label.plist"
  if [ -f "$plist" ]; then
    launchctl bootout "gui/$UID_NUM" "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "已停止并移除：$label"
  else
    echo "未安装：$label"
  fi
done
