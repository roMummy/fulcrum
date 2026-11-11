#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8280}"
BASE_URL="${BASE_URL%/}"
SMOKE_TMP_DIR="$(mktemp -d)"
LOG_FILE="${MBS_SMOKE_LOG_FILE:-}"
BUSINESS_SMOKE_SCRIPT="${MBS_BUSINESS_SMOKE_SCRIPT:-scripts/saas_business_smoke.py}"

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

echo "[1/8] checking liveness"
health_status="$(request_status GET /healthz "${SMOKE_TMP_DIR}/health.json")"
expect_status "${health_status}" "200" "healthz"
grep -q '"service":"fulcrum"' "${SMOKE_TMP_DIR}/health.json"

echo "[2/8] checking readiness"
ready_status="$(request_status GET /readyz "${SMOKE_TMP_DIR}/ready.json")"
expect_status "${ready_status}" "200" "readyz"
grep -q '"ready":true' "${SMOKE_TMP_DIR}/ready.json"

echo "[3/8] checking public commercial entries"
home_status="$(request_status GET / "${SMOKE_TMP_DIR}/home.html")"
expect_status "${home_status}" "200" "marketing home"
grep -q 'Fulcrum' "${SMOKE_TMP_DIR}/home.html"
grep -q '一个后台，统一运营所有智能机器人' "${SMOKE_TMP_DIR}/home.html"

pricing_status="$(request_status GET /pricing "${SMOKE_TMP_DIR}/pricing.html")"
expect_status "${pricing_status}" "200" "public pricing"
grep -q '套餐价格' "${SMOKE_TMP_DIR}/pricing.html"

register_status="$(request_status GET /register "${SMOKE_TMP_DIR}/register.html")"
expect_status "${register_status}" "200" "public registration"
grep -q 'action="/register"' "${SMOKE_TMP_DIR}/register.html"

echo "[4/8] checking browser login entries"
platform_status="$(request_status GET /platform/login "${SMOKE_TMP_DIR}/platform-login.html")"
expect_status "${platform_status}" "200" "platform login"
grep -q 'action="/platform/login"' "${SMOKE_TMP_DIR}/platform-login.html"

enterprise_status="$(request_status GET /login "${SMOKE_TMP_DIR}/enterprise-login.html")"
expect_status "${enterprise_status}" "200" "enterprise login"
grep -q 'action="/login"' "${SMOKE_TMP_DIR}/enterprise-login.html"

# 企业所有者和员工已经统一使用手机号入口。旧员工登录路由必须保持原生 404，
# 冒烟检查同时覆盖 GET 和 POST，避免后续发布重新引入租户编码或用户名兼容链。
removed_employee_get_status="$(request_status GET /employee/login "${SMOKE_TMP_DIR}/removed-employee-login-get.html")"
expect_status "${removed_employee_get_status}" "404" "removed employee login GET"
removed_employee_post_status="$(request_status POST /employee/login "${SMOKE_TMP_DIR}/removed-employee-login-post.html")"
expect_status "${removed_employee_post_status}" "404" "removed employee login POST"

echo "[5/8] checking provider authentication boundary"
activate_status="$(request_status POST /api/provider/activate "${SMOKE_TMP_DIR}/activate.json" -H 'Content-Type: application/json' --data '{"activation_code":"invalid-smoke-code","fingerprint":"smoke-device"}')"
expect_status "${activate_status}" "401" "provider invalid activation"
heartbeat_status="$(request_status POST /api/provider/heartbeat "${SMOKE_TMP_DIR}/heartbeat.json" -H 'Content-Type: application/json' --data '{"accounts":[]}')"
expect_status "${heartbeat_status}" "401" "provider missing credential"
if grep -Eq '"device_token"|pdev_[A-Za-z0-9_-]+|pact_[A-Za-z0-9_-]+' "${SMOKE_TMP_DIR}/activate.json" "${SMOKE_TMP_DIR}/heartbeat.json"; then
  echo "provider rejection response exposed credential material"
  exit 1
fi

echo "[6/8] checking isolated provider business flow"
# 业务冒烟只使用临时 SQLite 数据库，但调用与生产相同的租户会话、Provider
# 路由和租约服务。优先使用项目虚拟环境，容器内则回退到镜像自带 Python。
if [ -x ".venv/bin/python" ]; then
  ".venv/bin/python" "${BUSINESS_SMOKE_SCRIPT}"
else
  python "${BUSINESS_SMOKE_SCRIPT}"
fi

echo "[7/8] checking commercial test availability"
# 部署镜像可能只安装运行依赖，因此这里只确认测试文件随代码发布；完整商业闭环测试
# 在本地发布门禁中执行，避免在线冒烟把测试依赖或写操作带入生产数据库。
if [ ! -f "tests/test_public_commercial_flow.py" ]; then
  echo "commercial flow test is missing"
  exit 1
fi

echo "[8/8] checking optional log credential scan"
if [ -n "${LOG_FILE}" ] && [ -f "${LOG_FILE}" ]; then
  if grep -Eq 'pdev_[A-Za-z0-9_-]{16,}|pact_[A-Za-z0-9_-]{16,}' "${LOG_FILE}"; then
    echo "credential-like provider token found in log"
    exit 1
  fi
fi

echo "saas smoke test passed"
