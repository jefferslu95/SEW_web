#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把「服务器版」的代码组装成可在浏览器里独立运行的静态站点（webapp/）。

    python scripts/build_webapp.py

产物：
    webapp/index.html, app.js, style.css, i18n.js, i18n/*.json
    webapp/py/backend/*.py            （与服务器端同一套算法）
    webapp/vendor/plotly.min.js
    webapp/data/line_use.dat          （谱线表）
    webapp/data/samples/spectrum.fit  （示例谱，可选）

首次构建后执行一次 ``python scripts/fetch_pyodide.py`` 下载运行时。
"""
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "backend")
STATIC = os.path.join(BASE, "static")
OUT = os.path.join(BASE, "webapp")

MODULES = ["__init__.py", "fits_io.py", "fitting.py", "templates.py",
           "binning.py"]

# 浏览器里没有 Flask / werkzeug
EXCLUDE_IMPORT_HINTS = ("from flask", "from werkzeug")


def copy_backend():
    dst = os.path.join(OUT, "py", "backend")
    os.makedirs(dst, exist_ok=True)
    for fn in MODULES:
        src = os.path.join(SRC, fn)
        if not os.path.isfile(src):
            print(f"跳过（不存在）：{fn}")
            continue
        text = open(src, encoding="utf-8").read()
        bad = [h for h in EXCLUDE_IMPORT_HINTS if h in text]
        if bad:
            print(f"⚠ {fn} 含有服务端依赖 {bad}")
        shutil.copy2(src, os.path.join(dst, fn))
        print(f"  backend/{fn}")


def copy_static():
    for fn in ["style.css", "app.js", "i18n.js"]:
        shutil.copy2(os.path.join(STATIC, fn), os.path.join(OUT, fn))
        print(f"  static/{fn}")
    i18n_src = os.path.join(STATIC, "i18n")
    i18n_dst = os.path.join(OUT, "i18n")
    os.makedirs(i18n_dst, exist_ok=True)
    for fn in os.listdir(i18n_src):
        shutil.copy2(os.path.join(i18n_src, fn), os.path.join(i18n_dst, fn))
        print(f"  static/i18n/{fn}")

    html = open(os.path.join(STATIC, "index.html"), encoding="utf-8").read()
    html = html.replace('href="/static/style.css"', 'href="style.css"')
    html = html.replace('src="/vendor/plotly.min.js"',
                        'src="vendor/plotly.min.js"')
    html = html.replace(
        '<script src="/static/i18n.js"></script>',
        '<script src="i18n.js"></script>\n'
        '<script src="workspace.js"></script>\n'
        '<script src="rpc.js"></script>')
    html = html.replace(
        '<script src="/static/app.js"></script>',
        '<script src="app.js"></script>')
    html = html.replace("/static/", "")
    open(os.path.join(OUT, "index.html"), "w", encoding="utf-8").write(html)
    print("  index.html（改写资源路径 + 注入 rpc.js）")


def copy_assets():
    # plotly.min.js：来自已安装的 plotly 包
    try:
        import plotly
        src = os.path.join(os.path.dirname(plotly.__file__),
                           "package_data", "plotly.min.js")
        dst = os.path.join(OUT, "vendor", "plotly.min.js")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        print(f"  vendor/plotly.min.js ({os.path.getsize(dst)/1e6:.1f} MB)")
    except Exception as e:
        print(f"⚠ 未找到 plotly.min.js：{e}")

    # 谱线表
    line = os.environ.get("SEW_LINE_TABLE") or "/home/jflu/DGIS/line_use.dat"
    dst = os.path.join(OUT, "data", "line_use.dat")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isfile(line):
        shutil.copy2(line, dst)
        print("  data/line_use.dat")
    else:
        print(f"⚠ 谱线表不存在：{line}")

    # 示例谱
    try:
        from importlib import resources
        p = (resources.files("ppxf_sew") / "example" / "test_data"
             / "spectrum.fit")
        dst = os.path.join(OUT, "data", "samples", "spectrum.fit")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(str(p), dst)
        print("  data/samples/spectrum.fit")
    except Exception as e:
        print(f"⚠ 示例谱不可用：{e}")

    # 模板清单（不存在才写，避免覆盖用户自定义）
    man = os.path.join(OUT, "templates", "manifest.json")
    os.makedirs(os.path.dirname(man), exist_ok=True)
    if not os.path.isfile(man):
        import json
        data = {"templates": [
            {"name": "emiles", "label": "EMILES (25×6)",
             "file": "spectra_emiles_9.0.npz", "size_mb": 13},
            {"name": "fsps", "label": "FSPS (43×9)",
             "file": "spectra_fsps_9.0.npz", "size_mb": 15},
            {"name": "galaxev", "label": "Galaxev (43×5)",
             "file": "spectra_galaxev_9.0.npz", "size_mb": 20},
        ]}
        open(man, "w", encoding="utf-8").write(
            json.dumps(data, ensure_ascii=False, indent=2))
        print("  templates/manifest.json（新建）")


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    print("同步后端算法模块：")
    copy_backend()
    print("同步前端静态文件：")
    copy_static()
    print("复制运行资源：")
    copy_assets()
    print(f"完成 → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
