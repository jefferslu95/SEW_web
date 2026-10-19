#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把「服务器算力版」打包成一个可独立部署的目录 / tar.gz。

    python scripts/make_bundle.py                 # → dist/sew-web-server/
    python scripts/make_bundle.py --tar           # 另出 tar.gz
    python scripts/make_bundle.py --with-vendor    # 连 Pyodide 运行时一起（浏览器算力）
    python scripts/make_bundle.py --no-templates   # 不带可下载模板 npz

目标：把整个包 scp 到**任意一台机器**（不需要 SEW_web 源码仓库），
``python run.py --root <工作区>`` 就能用；工作区、上传、结果都在 --root 下，
与程序目录完全隔离。

包内默认带上 ``webapp/templates/*.npz``（清单里的可下载模板），
网页端「模板库 → 可下载模板」直接从包里拷到工作区（无需外网）；
不含 ``webapp/vendor``（Pyodide，≈60 MB）—— 服务器算力模式用不到。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tarfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "dist", "sew-web-server")

WEB_FILES = ["index.html", "app.js", "style.css", "i18n.js", "rpc.js",
             "workspace.js", "worker.js", "sw.js"]
WEB_DIRS = ["i18n", "py", os.path.join("data", "samples")]
SKIP_DIRS = {"__pycache__", "vendor"}

DEPLOY_TXT = """SEW web — 服务器算力版部署说明
=================================

1) 前置依赖（任选其一）
   pip install ppxf ppxf-sew powerbin capfit flask astropy numpy scipy matplotlib plotly

2) 启动（工作区与程序目录分离；不指定 --root 时在页面上选目录）
   python run.py --port 8765                # 工作区在页面上选
   python run.py --root /data/sew-ws        # 或预设

   参数：
     --root        预设工作区根目录（templates/ results/ data/ 自动创建）
     --token       可选口令（/api/* 需要 X-SEW-Token）

3) 从自己的电脑通过 SSH 转发访问
   ssh -N -L 8765:127.0.0.1:8765 user@server
   浏览器打开 http://127.0.0.1:8765/

说明：
  * 本版本计算完全在服务器（ppxf），浏览器不下载任何 Python 运行时。
  * 打开 FITS 只传路径，不需要把数据传到浏览器。
  * 「工作区设置」（点左上角 SEWweb 图标）可随时改工作区目录（带目录浏览面板）。
"""


def ignore(_dir, names):
    return [n for n in names if n in SKIP_DIRS or n.endswith(".pyc")]


def _template_files(manifest_path):
    """清单里的模板文件名（列表）。"""
    import json
    if not os.path.isfile(manifest_path):
        return []
    try:
        man = json.load(open(manifest_path, encoding="utf-8"))
        return [e["file"] for e in (man.get("templates") or []) if e.get("file")]
    except Exception:
        return []


def _from_ppxf_sew(fn):
    """从 ppxf_sew 自带模板目录找同名 npz。"""
    cands = []
    try:
        from importlib import resources
        cands.append(str(resources.files("ppxf_sew") / "example" / "sps_models"))
    except Exception:
        pass
    cands.append(os.path.expanduser("~/.local/lib/python3.11/site-packages/"
                                    "ppxf_sew/example/sps_models"))
    for d in cands:
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            return p
    return ""


def copy_tree(src, dst):
    shutil.copytree(src, dst, ignore=ignore)


def main() -> int:
    ap = argparse.ArgumentParser(description="打包服务器算力版")
    ap.add_argument("--out", default=OUT, help="输出目录")
    ap.add_argument("--tar", action="store_true", help="另外生成 tar.gz")
    ap.add_argument("--with-vendor", action="store_true",
                    help="带上 Pyodide 运行时（浏览器算力模式需要）")
    ap.add_argument("--no-templates", action="store_true",
                    help="不带可下载的模板 npz（默认带上）")
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)

    # 1) 启动器 + 后端
    shutil.copy2(os.path.join(BASE, "run.py"), os.path.join(out, "run.py"))
    copy_tree(os.path.join(BASE, "backend"), os.path.join(out, "backend"))

    # 2) 前端（webapp 构建产物）
    web_src = os.path.join(BASE, "webapp")
    if not os.path.isfile(os.path.join(web_src, "index.html")):
        print("缺少 webapp/index.html，先运行 python scripts/build_webapp.py")
        return 2
    web_dst = os.path.join(out, "webapp")
    os.makedirs(web_dst)
    for fn in WEB_FILES:
        p = os.path.join(web_src, fn)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(web_dst, fn))
    for d in WEB_DIRS:
        src = os.path.join(web_src, d)
        if os.path.isdir(src):
            copy_tree(src, os.path.join(web_dst, d))

    # 3) 模板清单（npz 可选）
    tpl_dst = os.path.join(web_dst, "templates")
    os.makedirs(tpl_dst, exist_ok=True)
    man = os.path.join(web_src, "templates", "manifest.json")
    if os.path.isfile(man):
        shutil.copy2(man, os.path.join(tpl_dst, "manifest.json"))
    if not args.no_templates:
        for fn in _template_files(man):
            src = os.path.join(web_src, "templates", fn)
            if not os.path.isfile(src):
                alt = _from_ppxf_sew(fn)
                if alt:
                    shutil.copy2(alt, src)
                    print(f"  补齐模板 {fn} ← {alt}")
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(tpl_dst, fn))
            else:
                print(f"⚠ 模板缺失: {fn}（运行 scripts/sync_templates.py --copy）")

    # 4) 可选：Pyodide 运行时（浏览器算力）
    if args.with_vendor:
        vend = os.path.join(web_src, "vendor")
        if os.path.isdir(vend):
            copy_tree(vend, os.path.join(web_dst, "vendor"))
        else:
            print("⚠ webapp/vendor 不存在，跳过（先运行 scripts/fetch_pyodide.py）")

    # 5) 运行时数据目录
    os.makedirs(os.path.join(out, "data", "uploads"), exist_ok=True)
    lt = os.path.join(web_dst, "data", "line_use.dat")
    if os.path.isfile(lt):
        shutil.copy2(lt, os.path.join(out, "data", "line_use.dat"))

    with open(os.path.join(out, "DEPLOY.txt"), "w", encoding="utf-8") as f:
        f.write(DEPLOY_TXT)

    size = sum(os.path.getsize(os.path.join(r, fn))
               for r, _d, fs in os.walk(out) for fn in fs)
    print(f"打包完成 → {out}  ({size/1048576:.1f} MB)")

    if args.tar:
        tar_path = out.rstrip("/") + ".tar.gz"
        with tarfile.open(tar_path, "w:gz") as tf:
            tf.add(out, arcname=os.path.basename(out))
        print(f"压缩包  → {tar_path} "
              f"({os.path.getsize(tar_path)/1048576:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
