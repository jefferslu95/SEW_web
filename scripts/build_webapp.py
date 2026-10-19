#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把后端算法模块同步到 webapp/py/backend（浏览器端 Pyodide 用）。

    python scripts/build_webapp.py

说明：
  * 只复制纯 Python 算法模块；`api.py` 是浏览器端入口，原位保留、不覆盖。
  * 不会复制模板 npz（体积大），需要时手动放到 webapp/templates/。
"""
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "backend")
DST = os.path.join(BASE, "webapp", "py", "backend")

MODULES = ["__init__.py", "fits_io.py", "fitting.py", "templates.py",
           "binning.py"]

# 浏览器里没有 Flask / werkzeug，也没有服务端路由
EXCLUDE_IMPORT_HINTS = ("from flask", "from werkzeug")


def main() -> int:
    os.makedirs(DST, exist_ok=True)
    for fn in MODULES:
        src = os.path.join(SRC, fn)
        if not os.path.isfile(src):
            print(f"跳过（不存在）：{fn}")
            continue
        text = open(src, encoding="utf-8").read()
        bad = [h for h in EXCLUDE_IMPORT_HINTS if h in text]
        if bad:
            print(f"⚠ {fn} 含有服务端依赖 {bad}，浏览器端可能不可用")
        shutil.copy2(src, os.path.join(DST, fn))
        print(f"复制 backend/{fn} -> webapp/py/backend/{fn}")
    print(f"完成：{len(MODULES)} 个模块 -> {DST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
