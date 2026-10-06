#!/bin/bash
# 常驻服务启动包装（launchd 不继承交互 shell 环境——此处固化环境，僵尸 worker 教训的对策：
# cwd=仓库根（.env 相对 cwd 加载）、CUA_DRIVER_MODE=real、PYTHONPATH、NO_PROXY 全部显式）。
# 用法：run_service.sh pipeline|screening|scheduler|worker
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE="${1:?用法：run_service.sh pipeline|screening|scheduler|worker}"
cd "$REPO"

export PYTHONUTF8=1
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="$NO_PROXY"
export DATABASE_URL="${DATABASE_URL:-mysql+pymysql://hr_user:hr_dev_pw@127.0.0.1:3306/hr_workbuddy}"
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
export PIPELINE_URL="${PIPELINE_URL:-http://127.0.0.1:8000}"
export SCREENING_URL="${SCREENING_URL:-http://127.0.0.1:8001}"
export MINIO_ENDPOINT="${MINIO_ENDPOINT:-127.0.0.1:9000}"
export MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-minioadmin}"
export MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-minioadmin}"
export MINIO_BUCKET="${MINIO_BUCKET:-hr-workbuddy}"

case "$SERVICE" in
  pipeline)
    exec "$REPO/.venv/bin/uvicorn" app.main:app \
      --app-dir "$REPO/services/pipeline" --host 127.0.0.1 --port 8000 --log-level info
    ;;
  screening)
    exec "$REPO/.venv/bin/uvicorn" app.main:app \
      --app-dir "$REPO/services/screening" --host 127.0.0.1 --port 8001 --log-level info
    ;;
  scheduler)
    # PYTHONPATH 显式给；cwd=仓库根 → settings env_file=".env" 读取根 .env
    export PYTHONPATH="$REPO/services/scheduler"
    exec "$REPO/.venv/bin/python" -m app.main
    ;;
  worker)
    export CUA_DRIVER_MODE=real
    export CUA_TASK_GAP_SECONDS="${CUA_TASK_GAP_SECONDS:-30}"
    export PYTHONPATH="$REPO/services/cua-agent"
    exec "$REPO/.venv/bin/arq" app.worker.WorkerSettings
    ;;
  *)
    echo "未知服务：$SERVICE" >&2
    exit 64
    ;;
esac
