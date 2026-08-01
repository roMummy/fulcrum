#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8280}"
BASE_URL="${BASE_URL%/}"
SMOKE_TMP_DIR="$(mktemp -d)"
LOG_FILE="${MBS_SMOKE_LOG_FILE:-}"

# 临时目录由 mktemp 创建且变量立即固定，退出时只删除这一条已解析路径，不使用
# HOME、通配符或外部输入作为递归删除目标。
cleanup() {
  rm -rf -- "${SMOKE_TMP_DIR}"
}
trap cleanup EXIT

request_status() {
  local method="$1"
  local path="$2"
  local output_file="$3"
  shift 3
  curl -sS -o "${output_file}" -w '%{http_code}' -X "${method}" "${BASE_URL}${path}" "$@"
}

expect_status() {
  local actual="$1"
  local expected="$2"
  local label="$3"
  if [ "${actual}" != "${expected}" ]; then
    echo "${label} failed: expected=${expected} actual=${actual}"
    exit 1
  fi
}

echo "[1/6] checking liveness"
health_status="$(request_status GET /healthz "${SMOKE_TMP_DIR}/health.json")"
expect_status "${health_status}" "200" "healthz"
grep -q '"service":"multi-bot-saas"' "${SMOKE_TMP_DIR}/health.json"

echo "[2/6] checking readiness"
ready_status="$(request_status GET /readyz "${SMOKE_TMP_DIR}/ready.json")"
expect_status "${ready_status}" "200" "readyz"
grep -q '"ready":true' "${SMOKE_TMP_DIR}/ready.json"

echo "[3/6] checking platform login entry"
platform_status="$(request_status GET /platform/login "${SMOKE_TMP_DIR}/platform-login.html")"
expect_status "${platform_status}" "200" "platform login"
grep -q 'action="/platform/login"' "${SMOKE_TMP_DIR}/platform-login.html"

echo "[4/6] checking tenant login entry"
tenant_status="$(request_status GET /login "${SMOKE_TMP_DIR}/tenant-login.html")"
expect_status "${tenant_status}" "200" "tenant login"
grep -q 'action="/login"' "${SMOKE_TMP_DIR}/tenant-login.html"

echo "[5/6] checking provider authentication boundary"
activate_status="$(request_status POST /api/provider/activate "${SMOKE_TMP_DIR}/activate.json" -H 'Content-Type: application/json' --data '{"activation_code":"invalid-smoke-code","fingerprint":"smoke-device"}')"
expect_status "${activate_status}" "401" "provider invalid activation"
heartbeat_status="$(request_status POST /api/provider/heartbeat "${SMOKE_TMP_DIR}/heartbeat.json" -H 'Content-Type: application/json' --data '{"accounts":[]}')"
expect_status "${heartbeat_status}" "401" "provider missing credential"
if grep -Eq '"device_token"|pdev_[A-Za-z0-9_-]+|pact_[A-Za-z0-9_-]+' "${SMOKE_TMP_DIR}/activate.json" "${SMOKE_TMP_DIR}/heartbeat.json"; then
  echo "provider rejection response exposed credential material"
  exit 1
fi

echo "[6/6] checking optional log credential scan"
if [ -n "${LOG_FILE}" ] && [ -f "${LOG_FILE}" ]; then
  if grep -Eq 'pdev_[A-Za-z0-9_-]{16,}|pact_[A-Za-z0-9_-]{16,}' "${LOG_FILE}"; then
    echo "credential-like provider token found in log"
    exit 1
  fi
fi

echo "saas smoke test passed"
