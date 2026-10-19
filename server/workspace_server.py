#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SEW web — 静态站点 + 服务器工作区（**纯标准库，不含计算**）。

用途：浏览器端版本（Pyodide/WASM）在远程机器上托管时，让「工作区」落在
**服务器目录**而不是访问者本地。计算仍在浏览器里（``compute = "browser"``），
服务器只做两件事：

    1. 提供 webapp/ 静态文件
    2. 提供 /api/ws/* 工作区文件读写（templates / results / data）

> 如果希望**计算也在服务器**（SSH 场景推荐），请用 ``python run.py --root ...``：
> 那条路径由 Flask 后端提供全部 /api/*，前端不会启动 Pyodide，打开服务器上的
> FITS 是零下载的。

用法：

    python server/workspace_server.py --root ~/sew-workspace --port 8790
    python server/workspace_server.py --root /data/sew --port 8790 --token 口令

    # 通过 SSH 转发给本地浏览器：
    ssh -N -L 8790:127.0.0.1:8790 user@server
    # 打开 http://127.0.0.1:8790/
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from backend import workspace as wsmod            # noqa: E402  纯标准库

WEB_DIR = os.path.join(BASE_DIR, "webapp")


class Handler(SimpleHTTPRequestHandler):
    server_version = "SEWws/1.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB_DIR, **kw)

    # ---------------- 工具 ----------------
    def _json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, cache_seconds=0):
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control",
                         f"max-age={cache_seconds}" if cache_seconds else "no-store")
        self.end_headers()
        with open(path, "rb") as f:
            remaining = size
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="SEW workspace"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorized(self, q) -> bool:
        if not wsmod.Cfg.token:
            return True
        tok = self.headers.get("X-SEW-Token") or (q.get("token", [""])[0])
        return tok == wsmod.Cfg.token

    def _err(self, e):
        code = 404 if isinstance(e, FileNotFoundError) else 403 \
            if isinstance(e, PermissionError) else 400
        return self._json(dict(ok=False, error=str(e)), code)

    # ---------------- 路由 ----------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/api/ws/"):
            if not self._authorized(q):
                return self._deny()
            return self.api_get(u.path, q)
        return super().do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/api/ws/"):
            if not self._authorized(q):
                return self._deny()
            return self.api_post(u.path, q)
        self.send_error(404)

    def do_DELETE(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/api/ws/"):
            if not self._authorized(q):
                return self._deny()
            return self.api_delete(u.path, q)
        self.send_error(404)

    # ---------------- API ----------------
    def api_get(self, path, q):
        try:
            if path == "/api/ws/info":
                return self._json(wsmod.info())
            if path == "/api/ws/browse":
                return self._json(wsmod.browse(q.get("path", [""])[0]))
            if path == "/api/ws/file":
                target = os.path.abspath(os.path.expanduser(
                    q.get("path", [""])[0]))
                if not wsmod.in_allow_roots(target):
                    raise PermissionError("路径不在允许范围内")
                if not os.path.isfile(target):
                    raise FileNotFoundError("文件不存在")
                return self._file(target)
            if path == "/api/ws/list":
                return self._json(wsmod.list_dir(q.get("dir", [""])[0]))
            if path == "/api/ws/get":
                full = wsmod.resolve_file(q.get("dir", [""])[0],
                                          q.get("name", [""])[0])
                # 模板基本不变，允许浏览器缓存（配合 Service Worker）
                return self._file(full, cache_seconds=86400)
            return self._json(dict(ok=False, error="未知接口"), 404)
        except Exception as e:
            return self._err(e)

    def api_post(self, path, q):
        try:
            if path == "/api/ws/root":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    body = {}
                new_root = body.get("root") or q.get("root", [""])[0]
                return self._json(wsmod.set_root(new_root))
            if path == "/api/ws/put":
                length = int(self.headers.get("Content-Length") or 0)
                out = wsmod.write_stream(q.get("dir", [""])[0],
                                         q.get("name", [""])[0],
                                         lambda n: self.rfile.read(n), length)
                return self._json(out)
            return self._json(dict(ok=False, error="未知接口"), 404)
        except Exception as e:
            return self._err(e)

    def api_delete(self, path, q):
        try:
            if path != "/api/ws/delete":
                return self._json(dict(ok=False, error="未知接口"), 404)
            return self._json(wsmod.delete_file(q.get("dir", [""])[0],
                                                q.get("name", [""])[0]))
        except Exception as e:
            return self._err(e)

    # 静态文件的 HEAD 也要走父类
    def do_HEAD(self):
        u = urlparse(self.path)
        if u.path.startswith("/api/ws/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return super().do_HEAD()

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    ap = argparse.ArgumentParser(description="SEW web 静态站点 + 服务器工作区（不计算）")
    ap.add_argument("--root", default=None, help="工作区根目录")
    ap.add_argument("--allow-root", action="append", default=[],
                    help="允许浏览/作为工作区的目录（可多次），默认 root 与 $HOME")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--token", default=None,
                    help="可选访问口令（也可用环境变量 SEW_WORKSPACE_TOKEN）")
    args = ap.parse_args()

    wsmod.init(root=args.root or os.path.expanduser("~/sew-workspace"),
               allow_roots=args.allow_root or None,
               token=args.token if args.token is not None
               else os.environ.get("SEW_WORKSPACE_TOKEN") or "",
               compute="browser")
    wsmod.ensure_dirs()

    if not os.path.isdir(WEB_DIR):
        print(f"找不到静态站点目录：{WEB_DIR}\n先运行 python scripts/build_webapp.py")
        return 2

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 66)
    print(f"  SEW web  http://{args.host}:{args.port}/")
    print(f"  静态站点: {WEB_DIR}")
    print(f"  工作区  : {wsmod.Cfg.root}")
    print("  算力    : 浏览器（Pyodide）；如需服务器算力请用 python run.py")
    print(f"  访问口令: {'已启用' if wsmod.Cfg.token else '无'}")
    print("=" * 66)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
