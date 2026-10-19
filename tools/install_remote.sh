#!/usr/bin/env bash
# SEWweb — 在算力服务器上一键部署并启动 run.py
#
#   # 最简（监听 0.0.0.0:8899，允许 GitHub Pages 跨源访问）
#   curl -fsSL https://jefferslu95.github.io/SEW_web/tools/install_remote.sh | bash
#
#   # 指定端口 / 工作区 / 只监听回环（配合 ssh -L 使用）
#   curl -fsSL .../install_remote.sh | bash -s -- --port 8899 --root ~/sew-ws
#   curl -fsSL .../install_remote.sh | bash -s -- --host 127.0.0.1
#
# 参数：
#   --docker           强制使用 Docker（服务器无需 Python 环境）
#   --no-docker        不用 Docker，直接跑 Python
#   --image NAME       镜像名（默认 sew-web:latest）
#   --rebuild          强制重建镜像
#   --pip-index URL    构建镜像时的 pip 源（国内可换清华/阿里源）
#
# 默认行为：有可用的 docker 就用 Docker（依赖全在镜像里），否则回退到 Python。
#   --port PORT        监听端口（默认 8899）
#   --host HOST        绑定地址（默认 0.0.0.0；内网/隧道场景可用 127.0.0.1）
#   --root DIR         预设工作区根目录（可留空，页面上再选）
#   --dir DIR          代码目录（默认 ~/SEW_web）
#   --python EXE       Python 解释器（默认自动探测：python3 / python / anaconda3/bin/python）
#   --origin URL       允许的页面来源（默认 https://jefferslu95.github.io，可重复）
#   --token XXX        工作区口令（可选）
#   --repo URL         代码仓库（默认 GitHub）
#   --no-build         跳过静态站点构建
set -euo pipefail

PORT=8899
HOST=0.0.0.0
ROOT=""
DIR="$HOME/SEW_web"
PY=""
ORIGINS=()
TOKEN=""
REPO="https://github.com/jefferslu95/SEW_web.git"
DO_BUILD=1
USE_DOCKER=auto
IMAGE="sew-web:latest"
REBUILD=0
PIP_INDEX=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)    PORT="$2"; shift 2 ;;
    --host)    HOST="$2"; shift 2 ;;
    --root)    ROOT="$2"; shift 2 ;;
    --dir)     DIR="$2"; shift 2 ;;
    --python)  PY="$2"; shift 2 ;;
    --origin)  ORIGINS+=("$2"); shift 2 ;;
    --token)   TOKEN="$2"; shift 2 ;;
    --repo)    REPO="$2"; shift 2 ;;
    --docker)  USE_DOCKER=1; shift ;;
    --no-docker) USE_DOCKER=0; shift ;;
    --image)   IMAGE="$2"; shift 2 ;;
    --rebuild) REBUILD=1; shift ;;
    --pip-index) PIP_INDEX="$2"; shift 2 ;;
    --no-build) DO_BUILD=0; shift ;;
    -h|--help) echo "用法：install_remote.sh [--docker|--no-docker] [--image NAME] [--rebuild] [--pip-index URL] [--port N] [--host H] [--root DIR] [--dir DIR] [--python EXE] [--origin URL] [--token X] [--no-build]"; exit 0 ;;
    *) echo "未知参数：$1（--help 查看用法）" >&2; exit 1 ;;
  esac
done

if [[ ${#ORIGINS[@]} -eq 0 ]]; then
  ORIGINS=("https://jefferslu95.github.io")
fi
CORS=$(IFS=,; echo "${ORIGINS[*]}")

say() { printf '\033[36m[SEWweb]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[SEWweb]\033[0m %s\n' "$*"; }

# ---------- 0. 部署方式判定（Docker 优先） ----------
have_docker() {
  command -v docker >/dev/null 2>&1 || return 1
  docker info >/dev/null 2>&1
}
if [[ "$USE_DOCKER" == "auto" ]]; then
  if have_docker; then USE_DOCKER=1; else USE_DOCKER=0; fi
elif [[ "$USE_DOCKER" == "1" ]] && ! have_docker; then
  warn "docker 不可用（未安装，或当前用户不在 docker 组），回退到 Python 模式"
  warn "  修复：sudo usermod -aG docker \$USER && newgrp docker"
  USE_DOCKER=0
fi

# ---------- 1. 解释器 ----------
# 关键：不能只挑第一个 python3 —— 系统自带的往往没装 flask/ppxf。
# 这里优先选「能 import flask + ppxf」的解释器（通常是 anaconda/venv）。
if [[ -z "$PY" ]]; then
  CANDS=(python3 python "$HOME/anaconda3/bin/python" "$HOME/miniconda3/bin/python"
         "$HOME/opt/anaconda3/bin/python" python3.12 python3.11 python3.10
         "$HOME/.local/bin/python3")
  FIRST=""
  for c in "${CANDS[@]}"; do
    if [[ "$c" == /* ]]; then
      [[ -x "$c" ]] || continue
      exe="$c"
    else
      command -v "$c" >/dev/null 2>&1 || continue
      exe="$(command -v "$c")"
    fi
    [[ -z "$FIRST" ]] && FIRST="$exe"
    if "$exe" -c "import flask, numpy, scipy, astropy, ppxf, powerbin" >/dev/null 2>&1; then
      PY="$exe"
      break
    fi
  done
  [[ -z "$PY" ]] && PY="$FIRST"
fi
if [[ -z "$PY" ]]; then
  [[ "$USE_DOCKER" == "1" ]] || { echo "找不到 Python 3（可改用 --docker 部署）" >&2; exit 1; }
  warn "未找到 Python（Docker 部署不需要），继续"
else
  say "使用解释器：$PY ($("$PY" -V 2>&1))"
fi

# ---------- 2. 代码 ----------
ssh_repo_url() {
  printf '%s' "$1" | sed -E 's#^https://github\.com/#git@github.com:#'
}

clone_repo() {
  say "克隆代码：$1 → $DIR"
  git clone --depth 1 "$1" "$DIR" -q
}

if [[ -d "$DIR/.git" ]]; then
  say "更新代码：$DIR"
  git -C "$DIR" pull --ff-only -q || warn "git pull 失败，继续用现有代码"
elif [[ -f "$DIR/run.py" ]]; then
  say "使用现有目录：$DIR（非 git 仓库，跳过更新）"
else
  if ! clone_repo "$REPO"; then
    # 有些网络 443 被拦、22 可用（或反之）：换个协议再试一次
    ALT="$(ssh_repo_url "$REPO")"
    warn "HTTPS 克隆失败，改用 SSH：$ALT"
    rm -rf "$DIR"
    clone_repo "$ALT" || {
      warn "克隆失败。可用 --repo 指定可达的仓库地址，或手动把代码放到 $DIR 再运行"
      exit 1
    }
  fi
fi
cd "$DIR"

# ---------- 3. 依赖自检（仅 Python 模式） ----------
if [[ "$USE_DOCKER" == "0" && -n "$PY" ]]; then
"$PY" - <<'EOF' || true
import importlib.util as u
need = [("flask", "flask"), ("numpy", "numpy"), ("scipy", "scipy"),
        ("astropy", "astropy"), ("ppxf", "ppxf"), ("ppxf_sew", "ppxf-sew")]
miss = [pip for mod, pip in need if u.find_spec(mod) is None]
if miss:
    print("[SEWweb] 缺少依赖：%s" % " ".join(miss))
    print("[SEWweb] 安装：<python> -m pip install %s" % " ".join(miss))
else:
    print("[SEWweb] 依赖检查通过")
EOF
fi

# ---------- 4. 静态站点（webapp/，仅 Python 模式需要） ----------
if [[ "$USE_DOCKER" == "0" && "$DO_BUILD" == "1" && -n "$PY" && -f scripts/build_webapp.py ]]; then
  "$PY" scripts/build_webapp.py >/dev/null 2>&1 \
    && say "静态站点已构建（webapp/）" || warn "构建静态站点失败（不影响 API 模式）"
fi

# ---------- 5. 部署 ----------
print_hint() {
  say "$1"
  if [[ "$HOST" == "0.0.0.0" ]]; then
    IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    say "在页面里填：http://${IP:-<服务器IP>}:$PORT"
  else
    say "仅监听回环。若页面在别处，用：ssh -L $PORT:127.0.0.1:$PORT <user>@<host>"
    say "然后页面里填：http://127.0.0.1:$PORT"
  fi
  say "允许来源：$CORS"
}

if [[ "$USE_DOCKER" == "1" ]]; then
  say "Docker 部署：服务器不需要 Python、不需要装任何依赖"
  if [[ "$REBUILD" == "1" ]] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    say "构建镜像 $IMAGE（首次约 5–10 分钟）…"
    BOPTS=()
    [[ -n "$PIP_INDEX" ]] && BOPTS+=(--build-arg "PIP_INDEX_URL=$PIP_INDEX")
    docker build "${BOPTS[@]}" -t "$IMAGE" "$DIR"
  else
    say "复用已有镜像 $IMAGE（--rebuild 可强制重建）"
  fi

  WS="${ROOT:-$HOME/sew-ws}"
  mkdir -p "$WS"
  CNAME="sew-web-$PORT"
  docker rm -f "$CNAME" >/dev/null 2>&1 || true

  RUN_OPTS=(-d --name "$CNAME" --restart unless-stopped
            -p "$HOST:$PORT:8765" -e "SEW_CORS=$CORS" -v "$WS:/workspace")
  [[ -n "$TOKEN" ]] && RUN_OPTS+=(-e "SEW_WORKSPACE_TOKEN=$TOKEN")
  # 镜像内默认用户是 uid 1000；宿主 uid 不同则显式指定，保证挂载目录可写
  if [[ "$(id -u)" != "1000" ]]; then
    RUN_OPTS+=(--user "$(id -u):$(id -g)" -e "HOME=/tmp")
  fi

  docker run "${RUN_OPTS[@]}" "$IMAGE" \
    python run.py --host 0.0.0.0 --port 8765 --root /workspace

  for _ in $(seq 1 60); do
    sleep 0.5
    if curl -sf "http://127.0.0.1:$PORT/api/ws/info" >/dev/null 2>&1; then
      say "容器已启动：$CNAME"
      say "宿主工作区：$WS  →  容器内 /workspace"
      say "日志：docker logs -f $CNAME"
      print_hint "部署完成（镜像 $IMAGE）"
      exit 0
    fi
  done
  warn "容器启动失败，日志："
  docker logs --tail 40 "$CNAME" 2>&1 || true
  exit 1
fi

# ---------- 6. 没有 Docker：直接跑 Python ----------
ARGS=(--host "$HOST" --port "$PORT" --cors "$CORS")
[[ -n "$ROOT" ]] && ARGS+=(--root "$ROOT")
[[ -n "$TOKEN" ]] && ARGS+=(--token "$TOKEN")

pkill -f "run.py --host .* --port $PORT" 2>/dev/null || true
sleep 0.4
LOG="/tmp/sew-run-$PORT.log"
nohup "$PY" run.py "${ARGS[@]}" >"$LOG" 2>&1 &
PID=$!
for _ in $(seq 1 30); do
  sleep 0.4
  if curl -sf "http://127.0.0.1:$PORT/api/ws/info" >/dev/null 2>&1; then
    say "已启动（pid $PID，监听 $HOST:$PORT）"
    say "日志：$LOG"
    print_hint "部署完成（Python 模式）"
    exit 0
  fi
done
warn "启动失败，日志："
tail -n 25 "$LOG" || true
exit 1
