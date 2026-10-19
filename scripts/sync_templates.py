#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校验 / 补齐 webapp/templates 里的可下载模板。

    python scripts/sync_templates.py           # 只报告缺失
    python scripts/sync_templates.py --copy    # 从 ppxf_sew 自带目录复制缺失项

``webapp/templates/manifest.json`` 是浏览器端「模板库 → 可下载」列表，如果清单里的
npz 实际不存在，页面点击下载会 404（2026-10-19 Galaxev 就是这样缺的）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL_DIR = os.path.join(BASE, "webapp", "templates")
MANIFEST = os.path.join(TPL_DIR, "manifest.json")


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


def main() -> int:
    ap = argparse.ArgumentParser(description="校验/补齐可下载模板")
    ap.add_argument("--copy", action="store_true", help="复制缺失的 npz")
    args = ap.parse_args()

    if not os.path.isfile(MANIFEST):
        print(f"找不到清单：{MANIFEST}")
        return 2
    man = json.load(open(MANIFEST, encoding="utf-8"))
    entries = man.get("templates") or []
    src_dirs = builtin_dirs()

    missing = 0
    for e in entries:
        fn = e.get("file")
        dst = os.path.join(TPL_DIR, fn)
        if os.path.isfile(dst):
            print(f"OK    {fn}  ({os.path.getsize(dst)/1048576:.1f} MB)")
            continue
        missing += 1
        found = ""
        for d in src_dirs:
            p = os.path.join(d, fn)
            if os.path.isfile(p):
                found = p
                break
        if not found:
            print(f"MISS  {fn}  ← 本地找不到源文件")
            continue
        if args.copy:
            shutil.copyfile(found, dst)
            print(f"COPY  {fn}  ← {found}")
        else:
            print(f"MISS  {fn}  ← 可用 {found}（加 --copy 复制）")
    if missing and not args.copy:
        print(f"\n共 {missing} 个缺失；执行 python scripts/sync_templates.py --copy")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
