# -*- coding: utf-8 -*-
"""服务器工作区（templates / results / data）的公共实现 —— **纯标准库**。

两个宿主共用这份逻辑，避免两套实现漂移：

  * ``backend/app.py``（Flask，带 numpy/ppxf 依赖）→ ``compute = "server"``
    计算也在服务器上，前端拿到的是服务器绝对路径，不下载 FITS。
  * ``server/workspace_server.py``（纯标准库静态服务）→ ``compute = "browser"``
    计算在浏览器（Pyodide），服务器只存文件。

目录结构（自动创建）::

    <root>/templates  模板 npz
    <root>/results    拟合结果 <时间>_<模板>.json / .png
    <root>/data       导入的 FITS
    <root>/workspace.json

安全：所有可切换/可浏览的目录都必须在 ``allow_roots`` 白名单内。
"""
from __future__ import annotations

import json
import os
import re

SUBDIRS = ("templates", "results", "data")
NAME_RE = re.compile(r"^[^/\\\x00]+$")
FITS_EXT = (".fits", ".fit", ".fts", ".fits.gz", ".fits.fz")
MAX_BODY = 4 * 1024 * 1024 * 1024        # 4 GB


class Cfg:
    """全局配置（进程内单例）。"""

    root = os.path.expanduser("~/sew-workspace")
    allow_roots: list = []
    token = ""
    compute = "browser"      # 'server' | 'browser'
    enabled = True


def init(root=None, allow_roots=None, token=None, compute=None):
    """初始化：root 先规范化，allow_roots 默认 [root, $HOME]。"""
    if root:
        Cfg.root = os.path.abspath(os.path.expanduser(str(root)))
    Cfg.root = os.path.abspath(os.path.expanduser(Cfg.root))

    roots = [os.path.abspath(os.path.expanduser(p))
             for p in (allow_roots or []) if str(p).strip()]
    if not roots:
        roots = [Cfg.root, os.path.expanduser("~")]
    if Cfg.root not in roots:
        roots.insert(0, Cfg.root)
    Cfg.allow_roots = roots

    if token is not None:
        Cfg.token = str(token or "")
    if compute:
        Cfg.compute = str(compute)


def in_allow_roots(path: str) -> bool:
    rp = os.path.realpath(path)
    for r in Cfg.allow_roots:
        rr = os.path.realpath(r)
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def safe_name(name: str) -> str:
    name = os.path.basename(str(name or ""))
    if not name or not NAME_RE.match(name) or name in (".", ".."):
        raise ValueError("非法文件名")
    return name


def safe_dir(d: str) -> str:
    d = str(d or "").strip("/")
    if d not in SUBDIRS:
        raise ValueError(f"未知目录: {d}")
    return os.path.join(Cfg.root, d)


def workspace_path(sub: str, name: str) -> str:
    """工作区内的绝对路径（server 计算模式直接用它打开 FITS）。"""
    return os.path.join(Cfg.root, sub, safe_name(name))


def ensure_dirs():
    for sub in SUBDIRS:
        os.makedirs(os.path.join(Cfg.root, sub), exist_ok=True)
    info_file = os.path.join(Cfg.root, "workspace.json")
    if not os.path.isfile(info_file):
        try:
            with open(info_file, "w", encoding="utf-8") as f:
                json.dump({"app": "SEW web", "structure": list(SUBDIRS)},
                          f, indent=1)
        except OSError:
            pass


def used_bytes() -> int:
    total = 0
    for sub in SUBDIRS:
        for root, _dirs, files in os.walk(os.path.join(Cfg.root, sub)):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    return total


def info() -> dict:
    return dict(ok=True, root=Cfg.root, dirs=list(SUBDIRS),
                writable=os.access(os.path.dirname(Cfg.root) or "/", os.W_OK),
                used_bytes=used_bytes(), auth=bool(Cfg.token),
                compute=Cfg.compute,
                roots=[os.path.realpath(r) for r in Cfg.allow_roots])


def set_root(new_root: str) -> dict:
    new_root = os.path.abspath(os.path.expanduser(str(new_root or "").strip()))
    if not new_root:
        raise ValueError("缺少 root")
    if not in_allow_roots(new_root):
        raise PermissionError(f"不允许的目录: {new_root}")
    os.makedirs(new_root, exist_ok=True)
    Cfg.root = new_root
    ensure_dirs()
    return info()


def browse(path: str = "") -> dict:
    target = os.path.abspath(os.path.expanduser(path)) if str(path or "").strip() \
        else Cfg.root
    if not in_allow_roots(target):
        raise PermissionError(f"路径不在允许范围内: {target}")
    if not os.path.isdir(target):
        raise FileNotFoundError(f"目录不存在: {target}")
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
        raise PermissionError(f"无权限读取: {target}")
    dirs.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())
    parent = os.path.dirname(target)
    if parent == target or not in_allow_roots(parent):
        parent = target
    return dict(ok=True, path=target, parent=parent, dirs=dirs, files=files,
                roots=[os.path.realpath(r) for r in Cfg.allow_roots])


def list_dir(d: str) -> dict:
    folder = safe_dir(d)
    items = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        names = []
    for name in names:
        p = os.path.join(folder, name)
        if not os.path.isfile(p):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        items.append(dict(name=name, size=int(st.st_size),
                          mtime=float(st.st_mtime)))
    return dict(ok=True, dir=str(d or "").strip("/"), files=items,
                root=Cfg.root)


def resolve_file(d: str, name: str) -> str:
    full = os.path.join(safe_dir(d), safe_name(name))
    if not os.path.isfile(full):
        raise FileNotFoundError("文件不存在")
    return full


def delete_file(d: str, name: str) -> dict:
    full = os.path.join(safe_dir(d), safe_name(name))
    try:
        os.remove(full)
    except FileNotFoundError:
        pass
    return dict(ok=True)


def write_stream(d: str, name: str, read_chunk, length: int) -> dict:
    """把流式上传写到 ``<dir>/<name>``：先写 .part 再 os.replace。"""
    full = os.path.join(safe_dir(d), safe_name(name))
    if length <= 0:
        raise ValueError("空文件")
    if length > MAX_BODY:
        raise ValueError("文件过大")
    tmp = full + ".part"
    remaining = length
    with open(tmp, "wb") as f:
        while remaining > 0:
            chunk = read_chunk(min(1 << 20, remaining))
            if not chunk:
                break
            f.write(chunk)
            remaining -= len(chunk)
    os.replace(tmp, full)
    return dict(ok=True, dir=str(d or "").strip("/"),
                name=os.path.basename(full), size=length, path=full)
