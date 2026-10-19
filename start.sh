#!/usr/bin/env bash
# 本地一键启动（使用 anaconda3 解释器；如无该环境请改 PYTHON）
cd "$(dirname "$0")"
PYTHON="${PYTHON:-/home/jflu/anaconda3/bin/python}"
exec "$PYTHON" run.py "$@"
