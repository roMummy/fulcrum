#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p .run

PORT="${MBS_PORT:-8280}"
PID_FILE=".run/ui-${PORT}.pid"
LOG_FILE=".run/mbs-uvicorn.log"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

# 默认使用稳定模式启动，不开启 --reload。
# 若本地手工调试确实需要热重载，可显式设置 MBS_UVICORN_RELOAD=1。
RELOAD_FLAG=()
if [ "${MBS_UVICORN_RELOAD:-0}" = "1" ]; then
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

# 只清理当前 SaaS 端口，禁止误停仍在 8180 运行的原项目。
port_pids="$(ss -ltnp 2>/dev/null | awk -v port=":${PORT}" '$0 ~ port {print $NF}' | grep -o 'pid=[0-9]\+' | cut -d= -f2 | sort -u || true)"
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
  echo "fulcrum failed to stay alive"
  tail -n 80 "$LOG_FILE" 2>/dev/null || true
  exit 1
fi

echo "fulcrum restarted"
echo "pid=$new_pid"
ss -ltnp | grep -E ":${PORT}\b" || true
curl -fsS "http://127.0.0.1:${PORT}/healthz" || true
