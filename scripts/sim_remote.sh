#!/usr/bin/env bash
# 在完全隔离的目录里模拟「通过 SSH 连上的远端服务器」：
#   * 程序目录  /tmp/sew-remote-sim/opt/sew-web-server   （只有部署包，没有源码仓库）
#   * 独立 HOME /tmp/sew-remote-sim/home                 （不碰 ~/.config 等）
#   * 工作区    /tmp/sew-remote-sim/work                 （与程序目录分离）
#   * 测试数据  /tmp/sew-remote-sim/data
#
# 用法：
#   bash scripts/sim_remote.sh [port]        # 默认 8799
#   SEW_SIM_DIR=/data/sim bash scripts/sim_remote.sh
#
# 停止：pkill -f "port ${PORT:-8799}"
set -euo pipefail

PORT="${1:-8799}"
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIM="${SEW_SIM_DIR:-/tmp/sew-remote-sim}"

# 找一个装了 flask + numpy 的解释器（可用 PYTHON=... 覆盖）
pick_python() {
  local c
  for c in "${PYTHON:-}" python python3 "$HOME/anaconda3/bin/python" \
           "$HOME/miniconda3/bin/python"; do
    [[ -n "$c" ]] || continue
    if command -v "$c" >/dev/null 2>&1 \
       && "$c" -c "import flask, powerbin, ppxf, astropy" >/dev/null 2>&1; then
      echo "$c"; return 0
    fi
  done
  echo python3
}
PY="$(pick_python)"
echo "Python: $PY"

cd "$BASE"
echo "[1/4] 打包（不含 Pyodide / 模板 npz）"
"$PY" scripts/build_webapp.py >/dev/null
"$PY" scripts/make_bundle.py --tar >/dev/null

echo "[2/4] 重建隔离环境 $SIM"
rm -rf "$SIM"
mkdir -p "$SIM/opt" "$SIM/home" "$SIM/work" "$SIM/data"
tar -xzf "$BASE/dist/sew-web-server.tar.gz" -C "$SIM/opt"

echo "[3/4] 放测试数据"
cp "$BASE/webapp/data/samples/spectrum.fit" "$SIM/data/sp.fits" 2>/dev/null || true

echo "[4/4] 启动（工作区 $SIM/work，端口 $PORT）"
cd "$SIM/opt/sew-web-server"
# 依赖装在用户 site-packages 时，改 HOME 会找不到 —— 显式加进 PYTHONPATH
EXTRA="${SEW_SIM_PYTHONPATH:-$HOME/.local/lib/python3.$( "$PY" -c 'import sys;print(sys.version_info[1])' )/site-packages}"
HOME="$SIM/home" PYTHONPATH="$EXTRA" OMP_NUM_THREADS=1 \
  exec "$PY" run.py --host 127.0.0.1 --port "$PORT" \
  --root "$SIM/work"
