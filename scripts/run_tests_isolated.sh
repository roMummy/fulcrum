#!/usr/bin/env bash
# 逐个测试文件运行:每个文件独立进程、独立测试数据库、带超时,并行执行。
#
# 为什么不直接 pytest 一次跑完:部分测试会修改全局配置/模块状态,放在同一进程里会互相污染,
# 并且个别大文件很慢,整套顺序执行很容易卡住。逐文件隔离运行结果稳定,也能看出哪个文件有问题。
#
# 用法:
#   scripts/run_tests_isolated.sh                       # 运行 tests/test_*.py 全部文件
#   scripts/run_tests_isolated.sh .github/ci-test-files.txt   # 只运行清单里的文件(每行一个路径)
# 环境变量:JOBS 并行数(默认 4)、TIMEOUT 单文件超时秒数(默认 300)、PYTHON 解释器(默认 python)。
set -uo pipefail

cd "$(dirname "$0")/.."
JOBS="${JOBS:-4}"
TIMEOUT="${TIMEOUT:-300}"
PYTHON="${PYTHON:-python}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [ "${1:-}" ]; then
  grep -v '^\s*\(#\|$\)' "$1" > "$WORK/files.txt"
else
  ls tests/test_*.py > "$WORK/files.txt"
fi

run_one() {
  f="$1"; name="$(basename "$f" .py)"; dir="$WORK/$name"
  mkdir -p "$dir/logs"
  # 应用启动时会在固定端口(默认 9165)起本地代理中继,并行的测试进程会撞端口:按文件名为每个文件分配不同端口。
  port=$((20000 + $(printf '%s' "$name" | cksum | cut -d' ' -f1) % 20000))
  if LOCAL_PROXY_RELAY_PORT="$port" APP_ENV=test DATA_DIR="$dir" DATABASE_URL="sqlite:///$dir/test.db" LOG_DIR="$dir/logs" \
      timeout "$TIMEOUT" "$PYTHON" -m pytest "$f" -q -p no:cacheprovider --tb=short > "$dir/out.txt" 2>&1; then
    echo "PASS  $f"
  else
    echo "FAIL  $f"; tail -n 60 "$dir/out.txt" | sed 's/^/    | /'
    return 1
  fi
}
export -f run_one
export WORK TIMEOUT PYTHON

xargs -a "$WORK/files.txt" -P "$JOBS" -I{} bash -c 'run_one "$@"' _ {} | tee "$WORK/summary.txt"
failed=$(grep -c '^FAIL' "$WORK/summary.txt" || true)
total=$(wc -l < "$WORK/files.txt")
echo "----"; echo "files: $total, failed: $failed"
[ "$failed" -eq 0 ]
