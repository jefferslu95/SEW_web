#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SEW web — 静态站点 + 服务器工作区服务（纯标准库，无需 Flask/依赖）。

用途：浏览器端版本（Pyodide）在远程机器上托管时，让「工作区」落在
**服务器目录**而不是访问者本地。计算仍然在浏览器里，服务器只做两件事：

    1. 提供 webapp/ 静态文件
    2. 提供 /api/ws/* 工作区文件读写（templates / results / data）

用法：

    python server/workspace_server.py --root ~/sew-workspace --port 8790
    python server/workspace_server.py --root /data/sew --port 8790 --token 口令

    # 通过 SSH 转发给本地浏览器：
    ssh -N -L 8790:127.0.0.1:8790 user@server
    # 打开 http://127.0.0.1:8790/  → 首次引导里选「服务器工作区」

目录结构（自动创建）：

    <root>/templates  模板 npz
    <root>/results    拟合结果 <时间>_<模板>.json / .png
    <root>/data       导入的 FITS
    <root>/workspace.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "webapp")
SUBDIRS = ("templates", "results", "data")
NAME_RE = re.compile(r"^[^/\\\x00]+$")
MAX_BODY = 4 * 1024 * 1024 * 1024        # 4 GB


class Config:
    root = os.path.expanduser("~/sew-workspace")
    allow_roots = []        # 允许浏览/作为工作区的目录（默认 root + $HOME）
    token = os.environ.get("SEW_WORKSPACE_TOKEN", "")


FITS_EXT = (".fits", ".fit", ".fts", ".fits.gz")


def _in_allow_roots(path: str) -> bool:
    rp = os.path.realpath(path)
    for r in Config.allow_roots:
        r = os.path.realpath(r)
        if rp == r or rp.startswith(r + os.sep):
            return True
    return False


def _safe_name(name: str) -> str:
    name = os.path.basename(str(name or ""))
    if not name or not NAME_RE.match(name) or name in (".", ".."):
        raise ValueError("非法文件名")
    return name


def _safe_dir(d: str) -> str:
    d = str(d or "").strip("/")
    if d not in SUBDIRS:
        raise ValueError(f"未知目录: {d}")
    return os.path.join(Config.root, d)


def _ensure_dirs():
    for sub in SUBDIRS:
        os.makedirs(os.path.join(Config.root, sub), exist_ok=True)
    info = os.path.join(Config.root, "workspace.json")
    if not os.path.isfile(info):
        with open(info, "w", encoding="utf-8") as f:
            json.dump({"app": "SEW web", "structure": list(SUBDIRS)}, f, indent=1)


class Handler(SimpleHTTPRequestHandler):
    server_version = "SEWws/1.0"

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

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="SEW workspace"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorized(self, q) -> bool:
        if not Config.token:
            return True
        tok = self.headers.get("X-SEW-Token") or (q.get("token", [""])[0])
        return tok == Config.token

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
                usage = 0
                for sub in SUBDIRS:
                    for _root, _d, files in os.walk(os.path.join(Config.root, sub)):
                        for f in files:
                            try:
                                usage += os.path.getsize(os.path.join(_root, f))
                            except OSError:
                                pass
                return self._json(dict(ok=True, root=Config.root,
                                       dirs=list(SUBDIRS),
                                       writable=os.access(Config.root, os.W_OK),
                                       used_bytes=usage,
                                       auth=bool(Config.token)))
            if path == "/api/ws/browse":
                raw = (q.get("path", [""])[0] or "").strip()
                target = os.path.abspath(os.path.expanduser(raw)) if raw \
                    else Config.root
                if not _in_allow_roots(target):
                    return self._json(dict(ok=False, error=f"路径不在允许范围内: {target}"), 403)
                if not os.path.isdir(target):
                    return self._json(dict(ok=False, error=f"目录不存在: {target}"), 404)
                dirs, files = [], []
                try:
                    with os.scandir(target) as it:
                        for e in it:
                            try:
                                if e.is_dir():
                                    dirs.append(dict(name=e.name, path=e.path))
                                elif e.is_file() and e.name.lower().endswith(FITS_EXT):
                                    st = e.stat()
                                    files.append(dict(name=e.name, path=e.path,
                                                      size=int(st.st_size),
                                                      mtime=float(st.st_mtime)))
                            except OSError:
                                continue
                except PermissionError:
                    return self._json(dict(ok=False, error=f"无权限读取: {target}"), 403)
                dirs.sort(key=lambda x: x["name"].lower())
                files.sort(key=lambda x: x["name"].lower())
                parent = os.path.dirname(target)
                if parent == target or not _in_allow_roots(parent):
                    parent = target
                return self._json(dict(ok=True, path=target, parent=parent,
                                       dirs=dirs, files=files,
                                       roots=[os.path.realpath(r) for r in Config.allow_roots]))
            if path == "/api/ws/file":
                raw = (q.get("path", [""])[0] or "").strip()
                target = os.path.abspath(os.path.expanduser(raw))
                if not _in_allow_roots(target):
                    return self._json(dict(ok=False, error="路径不在允许范围内"), 403)
                if not os.path.isfile(target):
                    return self._json(dict(ok=False, error="文件不存在"), 404)
                size = os.path.getsize(target)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with open(target, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 1 << 20)
                return
            if path == "/api/ws/list":
                d = (q.get("dir", [""])[0])
                folder = _safe_dir(d)
                items = []
                for name in sorted(os.listdir(folder)):
                    p = os.path.join(folder, name)
                    if not os.path.isfile(p):
                        continue
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    items.append(dict(name=name, size=int(st.st_size),
                                      mtime=float(st.st_mtime)))
                return self._json(dict(ok=True, dir=d, files=items))
            if path == "/api/ws/get":
                d = _safe_dir(q.get("dir", [""])[0])
                name = _safe_name(q.get("name", [""])[0])
                full = os.path.join(d, name)
                if not os.path.isfile(full):
                    return self._json(dict(ok=False, error="文件不存在"), 404)
                size = os.path.getsize(full)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                # 模板基本不变，允许浏览器缓存（配合 Service Worker）
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                with open(full, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 1 << 20)
                return
            return self._json(dict(ok=False, error="未知接口"), 404)
        except Exception as e:
            return self._json(dict(ok=False, error=f"{type(e).__name__}: {e}"), 400)

    def api_post(self, path, q):
        try:
            if path == "/api/ws/root":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    body = {}
                new_root = str(body.get("root") or q.get("root", [""])[0]).strip()
                if not new_root:
                    return self._json(dict(ok=False, error="缺少 root"), 400)
                new_root = os.path.abspath(os.path.expanduser(new_root))
                if not _in_allow_roots(new_root):
                    return self._json(dict(ok=False, error=f"不允许的目录: {new_root}"), 403)
                os.makedirs(new_root, exist_ok=True)
                Config.root = new_root
                _ensure_dirs()
                return self._json(dict(ok=True, root=Config.root,
                                       dirs=list(SUBDIRS)))
            if path == "/api/ws/put":
                d = _safe_dir(q.get("dir", [""])[0])
                name = _safe_name(q.get("name", [""])[0])
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return self._json(dict(ok=False, error="空文件"), 400)
                if length > MAX_BODY:
                    return self._json(dict(ok=False, error="文件过大"), 413)
                full = os.path.join(d, name)
                tmp = full + ".part"
                remaining = length
                with open(tmp, "wb") as f:
                    while remaining > 0:
                        chunk = self.rfile.read(min(1 << 20, remaining))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                os.replace(tmp, full)
                return self._json(dict(ok=True, dir=q.get("dir", [""])[0],
                                       name=name, size=length))
            return self._json(dict(ok=False, error="未知接口"), 404)
        except Exception as e:
            return self._json(dict(ok=False, error=f"{type(e).__name__}: {e}"), 400)

    def api_delete(self, path, q):
        try:
            if path != "/api/ws/delete":
                return self._json(dict(ok=False, error="未知接口"), 404)
            d = _safe_dir(q.get("dir", [""])[0])
            name = _safe_name(q.get("name", [""])[0])
            full = os.path.join(d, name)
            if os.path.isfile(full):
                os.remove(full)
            return self._json(dict(ok=True))
        except Exception as e:
            return self._json(dict(ok=False, error=f"{type(e).__name__}: {e}"), 400)

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
        # 静态资源加一点缓存，便于二次打开
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    ap = argparse.ArgumentParser(description="SEW web 静态站点 + 服务器工作区")
    ap.add_argument("--root", default=Config.root, help="工作区根目录")
    ap.add_argument("--allow-root", action="append", default=[],
                    help="允许浏览/作为工作区的目录（可多次），默认 root 与 $HOME")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--token", default=Config.token,
                    help="可选访问口令（也可用环境变量 SEW_WORKSPACE_TOKEN）")
    args = ap.parse_args()

    Config.root = os.path.abspath(os.path.expanduser(args.root))
    Config.token = args.token or ""
    Config.allow_roots = [os.path.abspath(os.path.expanduser(p))
                          for p in args.allow_root] or [Config.root,
                                                        os.path.expanduser("~")]
    if Config.root not in Config.allow_roots:
        Config.allow_roots.insert(0, Config.root)
    os.makedirs(Config.root, exist_ok=True)
    _ensure_dirs()

    if not os.path.isdir(WEB_DIR):
        print(f"找不到静态站点目录：{WEB_DIR}\n先运行 python scripts/build_webapp.py")
        return 2

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 66)
    print(f"  SEW web  http://{args.host}:{args.port}/")
    print(f"  静态站点: {WEB_DIR}")
    print(f"  工作区  : {Config.root}")
    print(f"  访问口令: {'已启用' if Config.token else '无'}")
    print("=" * 66)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
