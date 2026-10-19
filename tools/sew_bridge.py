#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sew_bridge.py — 本地 SSH 桥：让 GitHub Pages 上的 SEWweb 使用任意算力服务器。

为什么需要它
------------
GitHub Pages 是唯一界面（静态页，跑在浏览器里，只能看本机目录）。
浏览器没有 TCP socket，无法直接 SSH；而大多数算力服务器在内网、没有公网地址，
也不应该对外暴露一个 Web 服务。

做法：在**你自己的电脑上**跑这个脚本，它
    1) 用系统 ssh 建立本地端口转发  (ssh -N -L 127.0.0.1:A:127.0.0.1:B user@host)
    2) 必要时通过 ssh 在服务器上拉起 run.py（只监听 127.0.0.1，不对外）
    3) 在 127.0.0.1:<port> 起一个反向代理，给响应补上 CORS /
       Private-Network-Access 头，使 https://jefferslu95.github.io 能跨源调用

数据流
------
    浏览器 (https://jefferslu95.github.io/SEW_web/)
      └─ HTTP  →  127.0.0.1:8977   本脚本（加 CORS/PNA 头）
           └─ HTTP → 127.0.0.1:8899  ssh 本地转发
                └─ SSH → 服务器 127.0.0.1:8899  run.py（回环监听）

服务器端零配置：不需要公网 IP、不需要开放端口、不需要改防火墙。

用法
----
    # 最简：连服务器上的 SEW_web 目录，自动选端口并拉起源码
    python sew_bridge.py --ssh user@host

    # 指定远端代码目录与工作区
    python sew_bridge.py --ssh user@host \\
        --remote-dir ~/SEW_web --remote-root ~/sew-ws

    # 额外放行别的来源（可重复）
    python sew_bridge.py --ssh user@host --origin http://localhost:8080

    # 远端已经有实例在跑（自己起的），只做转发：
    python sew_bridge.py --ssh user@host --remote-port 8800 --no-autostart

打印出的地址填到页面「连接算力服务器」里即可（或加 ?bridge=http://127.0.0.1:8977）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_ORIGINS = [
    "https://jefferslu95.github.io",
    "https://jefferslu95.github.io/SEW_web",
    "http://127.0.0.1:8000",
    "http://localhost:8000",
]

HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length", "origin", "referer",
}


def log(*a):
    print("[bridge]", *a, flush=True)


def free_port(start: int = 8977, tries: int = 40) -> int:
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise SystemExit("找不到可用端口")


def ssh_run(ssh: str, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", "-o", "BatchMode=yes", ssh, cmd],
                          capture_output=True, text=True, timeout=timeout)


def remote_api_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/ws/info", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "sew-bridge"

    # 由 main() 注入
    upstream = "127.0.0.1:8899"
    origins: set[str] = set()

    def log_message(self, fmt, *args):  # 静音：只在出错时打印
        pass

    # ---------- CORS ----------
    def _cors(self):
        origin = self.headers.get("Origin", "")
        allow = "*" if "*" in self.origins else origin
        if origin and origin not in self.origins and "*" not in self.origins:
            return False
        self.send_header("Access-Control-Allow-Origin", allow or "*")
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods",
                         "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         self.headers.get("Access-Control-Request-Headers")
                         or "Content-Type,X-SEW-Token")
        self.send_header("Access-Control-Expose-Headers", "Content-Length")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        return True

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- 转发 ----------
    def _proxy(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        import http.client
        host, _, port = self.upstream.rpartition(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=600)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_HEADERS}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            status = resp.status
            rheaders = [(k, v) for k, v in resp.getheaders()
                        if k.lower() not in HOP_HEADERS]
        except Exception as exc:                     # noqa: BLE001
            msg = json.dumps({"ok": False,
                              "error": f"上游不可达：{exc}"}).encode()
            self.send_response(502)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        finally:
            try:
                conn.close()
            except Exception:
                pass

        self.send_response(status)
        for k, v in rheaders:
            if k.lower() in ("server", "date"):
                continue
            self.send_header(k, v)
        self._cors()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _proxy


def main() -> int:
    ap = argparse.ArgumentParser(description="SEWweb 本地 SSH 桥")
    ap.add_argument("--ssh", required=True,
                    help="SSH 目标，如 user@host")
    ap.add_argument("--port", type=int, default=8977,
                    help="本地代理端口（浏览器访问它），默认 8977")
    ap.add_argument("--remote-port", type=int, default=8899,
                    help="远端 run.py 端口（回环），默认 8899")
    ap.add_argument("--remote-dir", default="~/SEW_web",
                    help="远端 SEW_web 代码目录，默认 ~/SEW_web")
    ap.add_argument("--remote-root", default="",
                    help="远端工作区根目录（可空，页面上再选）")
    ap.add_argument("--python", default="python3",
                    help="远端 python 解释器，默认 python3")
    ap.add_argument("--origin", action="append", default=[],
                    help="额外允许的页面来源，可重复")
    ap.add_argument("--no-autostart", action="store_true",
                    help="不自动在远端启动 run.py（假设已有实例）")
    ap.add_argument("--direct", default="",
                    help="调试用：跳过 ssh，直接把上游设成该地址，如 127.0.0.1:8800")
    args = ap.parse_args()

    origins = set(DEFAULT_ORIGINS) | set(args.origin)
    if "*" in origins:
        origins = {"*"}

    port = args.port or free_port()

    if args.direct:
        upstream = args.direct
        log(f"调试模式：直连上游 {upstream}")
    else:
        if not shutil.which("ssh"):
            raise SystemExit("找不到 ssh 命令（Windows 请启用 OpenSSH 客户端）")
        rport = args.remote_port
        rdir = args.remote_dir
        py = args.python

        # 1) 远端是否已有实例
        check = ssh_run(args.ssh, f"curl -sf --max-time 3 "
                                  f"http://127.0.0.1:{rport}/api/ws/info "
                                  f">/dev/null && echo up || echo down")
        up = (check.stdout or "").strip().endswith("up")
        if not up:
            if args.no_autostart:
                raise SystemExit(
                    f"远端 127.0.0.1:{rport} 上没有运行中的 SEWweb；"
                    f"去掉 --no-autostart 让我自动启动，或先手动启动")
            root_arg = f" --root {args.remote_root}" if args.remote_root else ""
            cmd = (f"cd {rdir} && nohup {py} run.py --host 127.0.0.1 "
                   f"--port {rport}{root_arg} > /tmp/sew-run-{rport}.log 2>&1 &")
            log(f"在远端启动 run.py（端口 {rport}）…")
            ssh_run(args.ssh, cmd, timeout=60)
            for _ in range(20):
                time.sleep(0.5)
                if ssh_run(args.ssh, f"curl -sf --max-time 2 "
                                     f"http://127.0.0.1:{rport}/api/ws/info "
                                     f">/dev/null && echo up || echo down"
                           ).stdout.strip().endswith("up"):
                    break
        else:
            log(f"远端已有实例（端口 {rport}），直接复用")

        # 2) 建立本地转发
        tunnel = subprocess.Popen(
            ["ssh", "-N", "-o", "ExitOnForwardFailure=yes",
             "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
             "-L", f"127.0.0.1:{rport}:127.0.0.1:{rport}", args.ssh],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(40):
            time.sleep(0.25)
            if remote_api_ok(rport):
                break
            if tunnel.poll() is not None:
                err = (tunnel.stderr.read() or b"").decode(errors="replace")
                raise SystemExit(f"ssh 隧道建立失败：{err.strip()}")
        else:
            tunnel.kill()
            raise SystemExit("ssh 隧道建立后仍无法访问远端接口")
        log(f"隧道就绪：127.0.0.1:{rport} → {args.ssh}:{rport}")
        upstream = f"127.0.0.1:{rport}"

    Proxy.upstream = upstream
    Proxy.origins = origins

    srv = ThreadingHTTPServer(("127.0.0.1", port), Proxy)
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{port}"
    log("=" * 62)
    log(f"桥已启动：{url}")
    log(f"上游接口：{upstream}")
    log(f"允许来源：{', '.join(sorted(origins))}")
    log("把下面的地址填到页面里（或直接打开）：")
    log(f"  https://jefferslu95.github.io/SEW_web/?bridge={url}")
    log("按 Ctrl+C 结束")
    log("=" * 62)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("正在关闭…")
    finally:
        srv.server_close()
        if not args.direct:
            tunnel.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
