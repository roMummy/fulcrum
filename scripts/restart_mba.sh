#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p .run

PID_FILE=".run/ui-8180.pid"
LOG_FILE=".run/mba-uvicorn.log"
PORT="8180"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

# 默认使用稳定模式启动，不开启 --reload。
# 若本地手工调试确实需要热重载，可显式：MBA_UVICORN_RELOAD=1 ./scripts/restart_mba.sh
RELOAD_FLAG=()
if [ "${MBA_UVICORN_RELOAD:-0}" = "1" ]; then
  RELOAD_FLAG=(--reload)
fi

if [ -f "$PID_FILE" ]; then
  old_pid="$(cat "$PID_FILE" || true)"
  if [ -n "${old_pid:-}" ] && kill -0 "$old_pid" 2>/dev/null; then
    kill "$old_pid" 2>/dev/null || true
    sleep 2
    if kill -0 "$old_pid" 2>/dev/null; then
      kill -9 "$old_pid" 2>/dev/null || true
    fi
  fi
  rm -f "$PID_FILE"
fi

# 清理由旧实例遗留的 8180 监听，避免重启后端口被占用
port_pids="$(ss -ltnp 2>/dev/null | awk '/:8180\b/ {print $NF}' | grep -o 'pid=[0-9]\+' | cut -d= -f2 | sort -u || true)"
if [ -n "${port_pids:-}" ]; then
  for pid in $port_pids; do
    kill "$pid" 2>/dev/null || true
  done
  sleep 2
fi

nohup setsid .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" "${RELOAD_FLAG[@]}" > "$LOG_FILE" 2>&1 < /dev/null &
new_pid=$!
echo "$new_pid" > "$PID_FILE"

sleep 3

if ! kill -0 "$new_pid" 2>/dev/null; then
  echo "multi-bot-admin failed to stay alive"
  tail -n 80 "$LOG_FILE" 2>/dev/null || true
  exit 1
fi

echo "multi-bot-admin restarted"
echo "pid=$new_pid"
ss -ltnp | grep -E ":${PORT}\b" || true
curl -fsS "http://127.0.0.1:${PORT}/healthz" || true
