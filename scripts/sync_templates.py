#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校验 / 补齐 webapp/templates 里的可下载模板。

    python scripts/sync_templates.py                    # 只报告缺失
    python scripts/sync_templates.py --copy             # 从本地 ppxf_sew 复制
    python scripts/sync_templates.py --download         # 从 micappe/ppxf_data 下载

``webapp/templates/manifest.json`` 是「模板库 → 可下载模板」列表，每个条目的 ``url``
默认指向 https://github.com/micappe/ppxf_data （pPXF 官方数据仓库）。
浏览器端下载直接用这个 URL；服务器端由后端下载到工作区。

CI（GitHub Pages）里用 ``--download`` 让站点自带模板，无需把 48 MB 的 npz 提交进 git。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL_DIR = os.path.join(BASE, "webapp", "templates")
MANIFEST = os.path.join(TPL_DIR, "manifest.json")

DEFAULT_SRC = "https://raw.githubusercontent.com/micappe/ppxf_data/main/"


def builtin_dirs():
    dirs = []
    try:
        from importlib import resources
        dirs.append(str(resources.files("ppxf_sew") / "example" / "sps_models"))
    except Exception:
        pass
    dirs.append(os.path.expanduser("~/.local/lib/python3.11/site-packages/"
                                   "ppxf_sew/example/sps_models"))
    dirs.append(os.path.expanduser("~/.ppxf_sps_models"))
    dirs.append(os.path.join(BASE, "data", "templates"))
    return [d for d in dict.fromkeys(dirs) if os.path.isdir(d)]


def download(url: str, dst: str) -> int:
    tmp = dst + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "SEW-web/1.0"})
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    os.replace(tmp, dst)
    return os.path.getsize(dst)


def main() -> int:
    ap = argparse.ArgumentParser(description="校验/补齐可下载模板")
    ap.add_argument("--copy", action="store_true",
                    help="从本机 ppxf_sew 自带的 sps_models 复制缺失项")
    ap.add_argument("--download", action="store_true",
                    help="从清单里的 url（默认 micappe/ppxf_data）下载缺失项")
    args = ap.parse_args()

    if not os.path.isfile(MANIFEST):
        print(f"找不到清单：{MANIFEST}")
        return 2
    man = json.load(open(MANIFEST, encoding="utf-8"))
    entries = man.get("templates") or []
    src_dirs = builtin_dirs()
    os.makedirs(TPL_DIR, exist_ok=True)

    for e in entries:
        fn = e.get("file")
        dst = os.path.join(TPL_DIR, fn)
        if os.path.isfile(dst):
            print(f"OK    {fn}  ({os.path.getsize(dst)/1048576:.1f} MB)")
            continue
        if args.download:
            url = e.get("url") or (DEFAULT_SRC + fn)
            try:
                n = download(url, dst)
                print(f"GET   {fn}  ← {url}  ({n/1048576:.1f} MB)")
                continue
            except Exception as ex:
                print(f"FAIL  {fn}  ← {url}  ({ex})")
        found = ""
        for d in src_dirs:
            p = os.path.join(d, fn)
            if os.path.isfile(p):
                found = p
                break
        if not found:
            print(f"MISS  {fn}  ← 本地无源文件；可用 --download 从 ppxf_data 下载")
            continue
        if args.copy:
            shutil.copyfile(found, dst)
            print(f"COPY  {fn}  ← {found}")
        else:
            print(f"MISS  {fn}  ← 可用 {found}（--copy 复制 / --download 下载）")

    left = [e["file"] for e in entries
            if not os.path.isfile(os.path.join(TPL_DIR, e["file"]))]
    if left:
        print(f"\n仍缺 {len(left)} 个：{', '.join(left)}")
        print("可用：python scripts/sync_templates.py --download")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
