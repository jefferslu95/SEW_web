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
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "backend")
STATIC = os.path.join(BASE, "static")
OUT = os.path.join(BASE, "webapp")


def asset_version():
    """前端资源版本号（用于 cache-busting）。

    GitHub Pages 给 .js/.css 发 ``cache-control: max-age=600``；一旦 index.html
    先更新、而浏览器还在用缓存的旧 app.js，就会出现“按钮存在但点了没反应”。
    把所有资源引用改成 ``app.js?v=<commit>`` 即可避免。
    """
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=BASE, stderr=subprocess.DEVNULL).decode().strip()
        if out:
            return out
    except Exception:
        pass
    try:
        return str(int(max(
            os.path.getmtime(os.path.join(STATIC, f))
            for f in ("app.js", "style.css", "i18n.js", "index.html"))))
    except Exception:
        return "0"

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
    ver = asset_version()
    for name in ("style.css", "app.js", "i18n.js", "workspace.js", "rpc.js"):
        html = html.replace('"%s"' % name, '"%s?v=%s"' % (name, ver))
    open(os.path.join(OUT, "index.html"), "w", encoding="utf-8").write(html)
    print("  index.html（改写资源路径 + 注入 rpc.js + 版本 %s）" % ver)


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
        base = "https://raw.githubusercontent.com/micappe/ppxf_data/main/"
        data = {"source": "https://github.com/micappe/ppxf_data",
                "templates": [
            {"name": "emiles", "label": "EMILES (25×6)",
             "file": "spectra_emiles_9.0.npz", "size_mb": 13,
             "url": base + "spectra_emiles_9.0.npz"},
            {"name": "fsps", "label": "FSPS (43×9)",
             "file": "spectra_fsps_9.0.npz", "size_mb": 15,
             "url": base + "spectra_fsps_9.0.npz"},
            {"name": "galaxev", "label": "Galaxev (43×5)",
             "file": "spectra_galaxev_9.0.npz", "size_mb": 20,
             "url": base + "spectra_galaxev_9.0.npz"},
            {"name": "xsl", "label": "XSL (26×8)",
             "file": "spectra_xsl_9.0.npz", "size_mb": 94,
             "url": base + "spectra_xsl_9.0.npz"},
        ]}
        open(man, "w", encoding="utf-8").write(
            json.dumps(data, ensure_ascii=False, indent=2))
        print("  templates/manifest.json（新建）")

    # 清单里的 npz 是否真的在：缺了页面点下载就是 404
    try:
        import json as _json
        ent = _json.load(open(man, encoding="utf-8")).get("templates") or []
        miss = [e["file"] for e in ent
                if not os.path.isfile(os.path.join(OUT, "templates", e["file"]))]
        if miss:
            print("⚠ 模板清单里这些文件缺失（页面下载会 404）："
                  + ", ".join(miss))
            print("  执行 python scripts/sync_templates.py --copy 补齐")
    except Exception as e:
        print(f"⚠ 无法校验模板清单：{e}")


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
