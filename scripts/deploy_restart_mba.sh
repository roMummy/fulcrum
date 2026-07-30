#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p .run

APP_PORT="${MBA_PORT:-8180}"
APP_HOST="${MBA_HOST:-0.0.0.0}"
APP_MODULE="${MBA_APP_MODULE:-app.main:app}"
APP_BRANCH="${MBA_GIT_BRANCH:-main}"
ALLOW_DIRTY="${MBA_ALLOW_DIRTY:-0}"
PID_FILE=".run/ui-${APP_PORT}.pid"
LOG_FILE=".run/mba-uvicorn.log"

if ! command -v git >/dev/null 2>&1; then
  echo "git is required"
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required"
  exit 1
fi

dirty_status="$(git status --porcelain -- . ':(exclude)scripts/deploy_restart_mba.sh' ':(exclude).run' || true)"
if [ -n "${dirty_status}" ] && [ "${ALLOW_DIRTY}" != "1" ]; then
  echo "working tree is not clean; please commit or stash local changes before deploy restart"
  echo "ignored paths: scripts/deploy_restart_mba.sh, .run/"
  printf '%s\n' "${dirty_status}"
  echo "set MBA_ALLOW_DIRTY=1 to bypass this check temporarily"
  exit 1
fi

script_status="$(git status --porcelain -- scripts/deploy_restart_mba.sh || true)"
if [ -n "${script_status}" ]; then
  echo "[preflight] restoring local changes in scripts/deploy_restart_mba.sh before pull"
  git restore --source=HEAD --staged --worktree scripts/deploy_restart_mba.sh
fi

echo "[1/6] fetching latest code from origin/${APP_BRANCH}"
git fetch origin "${APP_BRANCH}"
git checkout "${APP_BRANCH}"
git pull --ff-only origin "${APP_BRANCH}"

if [ ! -d .venv ]; then
  echo "[2/6] creating virtual environment"
  python3 -m venv .venv
fi

echo "[3/6] installing dependencies"
.venv/bin/python -m pip install -r requirements.txt

stop_pid() {
  local pid="$1"
  if [ -z "${pid:-}" ]; then
    return 0
  fi
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 2
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
}

echo "[4/6] stopping old process"
if [ -f "$PID_FILE" ]; then
  stop_pid "$(cat "$PID_FILE" || true)"
  rm -f "$PID_FILE"
fi

port_pids=""
if command -v ss >/dev/null 2>&1; then
  port_pids="$(ss -ltnp 2>/dev/null | awk -v port=":${APP_PORT}" '$0 ~ port {print $NF}' | grep -o 'pid=[0-9]\+' | cut -d= -f2 | sort -u || true)"
elif command -v lsof >/dev/null 2>&1; then
  port_pids="$(lsof -ti tcp:"${APP_PORT}" || true)"
fi

if [ -n "${port_pids:-}" ]; then
  for pid in $port_pids; do
    stop_pid "$pid"
  done
fi

echo "[5/6] starting service"
nohup setsid .venv/bin/python -m uvicorn "${APP_MODULE}" --host "${APP_HOST}" --port "${APP_PORT}" > "$LOG_FILE" 2>&1 < /dev/null &
new_pid=$!
echo "$new_pid" > "$PID_FILE"

sleep 4

if ! kill -0 "$new_pid" 2>/dev/null; then
  echo "multi-bot-admin failed to stay alive"
  tail -n 80 "$LOG_FILE" 2>/dev/null || true
  exit 1
fi

echo "[6/6] checking health"
if ! curl -fsS "http://127.0.0.1:${APP_PORT}/healthz" >/dev/null; then
  echo "health check failed"
  tail -n 80 "$LOG_FILE" 2>/dev/null || true
  exit 1
fi

echo "deploy restart finished"
echo "branch=${APP_BRANCH}"
echo "pid=${new_pid}"
echo "log=${LOG_FILE}"
