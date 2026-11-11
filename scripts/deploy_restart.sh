#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p .run

APP_PORT="${MBS_PORT:-8280}"
APP_HOST="${MBS_HOST:-0.0.0.0}"
APP_MODULE="${MBS_APP_MODULE:-app.main:app}"
APP_BRANCH="${MBS_GIT_BRANCH:-main}"
ALLOW_DIRTY="${MBS_ALLOW_DIRTY:-0}"
PID_FILE=".run/ui-${APP_PORT}.pid"
LOG_FILE=".run/mbs-uvicorn.log"

if ! command -v git >/dev/null 2>&1; then
  echo "git is required"
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required"
  exit 1
fi

# `.run/` 保存部署运行文件，`.rcodex/` 保存本机研发工具状态；二者都不属于
# 应用发布差异，不能让它们阻断已经提交完成的正式部署。
dirty_status="$(git status --porcelain -- . ':(exclude)scripts/deploy_restart.sh' ':(exclude).run' ':(exclude).rcodex' || true)"
if [ -n "${dirty_status}" ] && [ "${ALLOW_DIRTY}" != "1" ]; then
  echo "working tree is not clean; please commit or stash local changes before deploy restart"
  echo "ignored paths: scripts/deploy_restart.sh, .run/, .rcodex/"
  printf '%s\n' "${dirty_status}"
  echo "set MBS_ALLOW_DIRTY=1 to bypass this check temporarily"
  exit 1
fi

script_status="$(git status --porcelain -- scripts/deploy_restart.sh || true)"
if [ -n "${script_status}" ]; then
  echo "[preflight] restoring local changes in scripts/deploy_restart.sh before pull"
  git restore --source=HEAD --staged --worktree scripts/deploy_restart.sh
fi

echo "[1/10] fetching latest code from origin/${APP_BRANCH}"
git fetch origin "${APP_BRANCH}"
git checkout "${APP_BRANCH}"
git pull --ff-only origin "${APP_BRANCH}"

if [ ! -d .venv ]; then
  echo "[2/10] creating virtual environment"
  python3 -m venv .venv
fi

echo "[3/10] installing dependencies"
.venv/bin/python -m pip install -r requirements.txt

# 部署前先证明当前分支包含完整插件集合，并通过一次纯内存加密验证主密钥。
# 同时导入敏感字段清理模块，提前发现日志目录权限或运行依赖错误。两个探针都不会
# 读取数据库、保存密文或把主密钥打印到部署日志。
echo "[4/10] verifying plugin migration and secret key"
.venv/bin/python scripts/verify_plugin_migration.py
.venv/bin/python -c 'from app.services.plugin_secret_cleanup import scrub_tenant_plugin_plaintext_secrets; from app.services.plugin_secrets import encrypt_plugin_secret_config; encrypt_plugin_secret_config(tenant_id=1, binding_id=1, config={"deployment_probe": "ok"})'

# Alembic 和敏感字段清理必须在停止旧进程前成功。这样依赖安装、完整性校验、
# 密钥校验或数据库处理失败时，旧服务仍会保持运行，不扩大部署前置错误。
echo "[5/10] migrating database"
.venv/bin/alembic upgrade head

echo "[6/10] scrubbing plaintext plugin secrets"
.venv/bin/python scripts/scrub_plugin_plaintext_secrets.py

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

echo "[7/10] stopping old process"
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

echo "[8/10] starting service"
nohup setsid .venv/bin/python -m uvicorn "${APP_MODULE}" --host "${APP_HOST}" --port "${APP_PORT}" > "$LOG_FILE" 2>&1 < /dev/null &
new_pid=$!
echo "$new_pid" > "$PID_FILE"

sleep 4

if ! kill -0 "$new_pid" 2>/dev/null; then
  echo "fulcrum failed to stay alive"
  tail -n 80 "$LOG_FILE" 2>/dev/null || true
  exit 1
fi

echo "[9/10] checking liveness"
if ! curl -fsS "http://127.0.0.1:${APP_PORT}/healthz" >/dev/null; then
  echo "liveness check failed"
  tail -n 80 "$LOG_FILE" 2>/dev/null || true
  exit 1
fi

echo "[10/10] checking readiness"
if ! curl -fsS "http://127.0.0.1:${APP_PORT}/readyz" >/dev/null; then
  echo "readiness check failed"
  tail -n 80 "$LOG_FILE" 2>/dev/null || true
  exit 1
fi

echo "deploy restart finished"
echo "branch=${APP_BRANCH}"
echo "pid=${new_pid}"
echo "log=${LOG_FILE}"
