#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SEW_web 启动器（唯一入口）。

    python run.py                     # 默认 127.0.0.1:8765
    python run.py --port 8080
    python run.py --host 0.0.0.0 --port 8080   # 允许远程访问
    python run.py --open              # 启动后打开系统浏览器

服务端封装了全部 Python 依赖（numpy / astropy / matplotlib / ppxf /
ppxf-sew），浏览器只需要访问 HTTP 页面。
"""
import argparse
import os
import sys
import threading
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def main():
    ap = argparse.ArgumentParser(description="SEW web spectrum/cube viewer & fitter")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="启动后打开浏览器")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--root", default=None,
                    help="预设服务器工作区根目录（默认不预设，在页面上选）")
    ap.add_argument("--token", default=None,
                    help="服务器工作区口令（也可用 SEW_WORKSPACE_TOKEN）")
    ap.add_argument("--cors", default=None,
                    help="可选：允许的跨源来源（逗号分隔，默认不开放）")
    args = ap.parse_args()

    # 服务器工作区配置必须在导入 app 之前入环境
    if args.root:
        os.environ["SEW_WS_ROOT"] = args.root
    if args.token:
        os.environ["SEW_WORKSPACE_TOKEN"] = args.token
    if args.cors:
        os.environ["SEW_CORS"] = args.cors

    from backend.app import app
    from backend import workspace as wsmod

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"
    print("=" * 70)
    print("  SEW web — FITS 光谱 / 数据立方 查看与拟合")
    print(f"  URL : {url}")
    print(f"  Root: {BASE_DIR}")
    print(f"  工作区: {wsmod.Cfg.root}  (算力: 服务器)")
    print(f"  Python: {sys.executable}")
    print("=" * 70)
    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
