#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -r requirements.txt
# 新项目固定使用独立 8280 端口，允许开发机同时保留原项目的 8180 服务。
uvicorn app.main:app --reload --host 0.0.0.0 --port 8280
