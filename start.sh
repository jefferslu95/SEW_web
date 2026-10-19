#!/usr/bin/env bash
# 本地一键启动：优先 $PYTHON，否则用 PATH 里的 python3 / python
cd "$(dirname "$0")"
if [[ -n "${PYTHON:-}" ]]; then
  exec "$PYTHON" run.py "$@"
fi
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    exec "$c" run.py "$@"
  fi
done
echo "找不到 python3，请用 PYTHON=/path/to/python ./start.sh" >&2
exit 1
