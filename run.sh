#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -r requirements.txt
# 主服务默认使用 8180，避免落入 Windows TCP 排除端口段导致绑定失败。
uvicorn app.main:app --reload --host 0.0.0.0 --port 8180
