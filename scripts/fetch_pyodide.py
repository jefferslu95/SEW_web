#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""下载浏览器端（Pyodide）运行所需的全部第三方资源到 webapp/vendor/。

    python scripts/fetch_pyodide.py

只下载一次；产物默认被 .gitignore 排除（体积较大，不适合进 git）。
部署时把这些文件一起上传到静态站点即可，运行时不再访问外网 CDN。
"""
import json
import os
import sys
import urllib.request

VERSION = "0.29.0"
BASE = f"https://cdn.jsdelivr.net/pyodide/v{VERSION}/full/"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE_DIR, "webapp", "vendor", "pyodide")
WHEELS = os.path.join(BASE_DIR, "webapp", "vendor", "wheels")

PACKAGES = ["numpy", "scipy", "matplotlib", "astropy", "micropip", "packaging"]
CORE = ["pyodide.js", "pyodide.mjs", "pyodide.asm.js", "pyodide.asm.wasm",
        "python_stdlib.zip", "pyodide-lock.json"]

# 纯 Python 轮子（pip 安装，浏览器里用 micropip 从本地文件安装）
PYPI_WHEELS = [
    "ppxf==9.5.0",
    "ppxf-sew==0.8.2",
    "plotbin",
    "capfit",
    "powerbin",
    "astropy-iers-data",     # astropy 的运行时依赖，Pyodide 分发里不含
]


def _download(url: str, dst: str) -> int:
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        return 0
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with urllib.request.urlopen(url) as r, open(dst, "wb") as f:
        f.write(r.read())
    return os.path.getsize(dst)


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(WHEELS, exist_ok=True)
    lock = json.load(urllib.request.urlopen(BASE + "pyodide-lock.json"))
    pkgs = lock["packages"]

    need, stack = set(), list(PACKAGES)
    while stack:
        n = stack.pop()
        if n in need or n not in pkgs:
            continue
        need.add(n)
        stack.extend(pkgs[n].get("depends", []))

    files = list(CORE)
    files += [pkgs[n]["file_name"] for n in sorted(need)
              if pkgs[n].get("file_name")]

    total = 0
    for f in files:
        n = _download(BASE + f, os.path.join(OUT, f))
        if n:
            print(f"  {f}  {n/1e6:.1f} MB")
        total += n
    print(f"Pyodide 运行时：{len(files)} 个文件，新下载 {total/1e6:.1f} MB")

    import subprocess
    cmd = [sys.executable, "-m", "pip", "download", "-q", "--no-deps",
           "--dest", WHEELS] + PYPI_WHEELS
    print("下载纯 Python 轮子：", " ".join(PYPI_WHEELS))
    subprocess.check_call(cmd)

    # 供前端预热缓存 / 镜像到工作区用
    manifest = []
    for root, _dirs, fns in os.walk(os.path.join(BASE_DIR, "webapp", "vendor")):
        for fn in fns:
            if fn == "manifest.json":
                continue
            rel = os.path.relpath(os.path.join(root, fn),
                                  os.path.join(BASE_DIR, "webapp", "vendor"))
            manifest.append(rel.replace(os.sep, "/"))
    mpath = os.path.join(BASE_DIR, "webapp", "vendor", "manifest.json")
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump({"version": VERSION, "files": sorted(manifest)}, f, indent=1)
    print(f"vendor/manifest.json：{len(manifest)} 个文件")
    print(f"完成。产物在 {os.path.join(BASE_DIR, 'webapp', 'vendor')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
