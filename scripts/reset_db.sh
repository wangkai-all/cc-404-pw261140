#!/usr/bin/env bash
# 一键重建数据库（清空后重新生成虚构订单与基线产物）
set -euo pipefail
cd "$(dirname "$0")/.."
rm -f data/metrics.db data/metrics.db-wal data/metrics.db-shm
python3 -m app.bootstrap
