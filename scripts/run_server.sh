#!/usr/bin/env bash
# 启动 Web 服务（首次启动自动初始化数据库）
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${1:-8080}"
python3 -m app.server --host 0.0.0.0 --port "$PORT"
