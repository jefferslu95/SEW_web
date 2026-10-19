# -*- coding: utf-8 -*-
"""SEW_web Flask 后端。

网页端只通过 HTTP 调用这里的 Python；Python 依赖全部封装在服务端，
浏览器侧不需要任何 Python 环境。
"""
from __future__ import annotations

import io
import json
import math
import os
import shutil
import threading
import time
import traceback
import uuid
from datetime import datetime

import numpy as np
from flask import (Flask, Response, jsonify, request, send_from_directory,
                   send_file)
from werkzeug.utils import secure_filename

from . import fits_io, templates, fitting
from . import workspace as wsmod

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
WEBAPP_DIR = os.path.join(BASE_DIR, "webapp")   # build_webapp.py 产物
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _page_dir() -> str:
    """优先用组装好的 webapp/（带 workspace.js / rpc.js 工作区能力）。"""
    if os.path.isfile(os.path.join(WEBAPP_DIR, "index.html")):
        return WEBAPP_DIR
    return STATIC_DIR

# 服务器工作区：默认**不设**（由用户在工作区设置里选）；可用 SEW_WS_ROOT 预设
wsmod.init(root=os.environ.get("SEW_WS_ROOT") or None,
           token=os.environ.get("SEW_WORKSPACE_TOKEN") or "",
           compute="server")
# 模板库优先读工作区 <root>/templates（随 /api/ws/root 切换动态生效）
templates.set_runtime_dir(os.path.join(wsmod.Cfg.root, "templates")
                          if wsmod.Cfg.root else None)
try:
    wsmod.ensure_dirs()
except OSError:
    pass

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024   # 4 GB

# ---------------------------------------------------------------------------
# 可选访问控制（本地单用户默认关闭；对外暴露时必须开启）
#   SEW_AUTH="user:pass"                  -> HTTP Basic 认证
#   SEW_DATA_ROOTS=/data:/home/me/fits    -> 只允许这些目录下的 FITS
# ---------------------------------------------------------------------------
_AUTH = os.environ.get("SEW_AUTH") or ""
_AUTH_PAIR = None
if _AUTH.strip():
    _u, _, _p = _AUTH.partition(":")
    _AUTH_PAIR = (_u, _p)

_ROOTS = [os.path.realpath(os.path.expanduser(p))
          for p in (os.environ.get("SEW_DATA_ROOTS") or "").split(os.pathsep)
          if p.strip()]
if _ROOTS:
    # 上传目录永远允许（否则网页上传后无法打开）
    _ROOTS.insert(0, os.path.realpath(DATA_DIR))


def _in_roots(path: str) -> bool:
    """未设置 SEW_DATA_ROOTS 时不限制；设置后只允许白名单目录内的路径。"""
    if not _ROOTS:
        return True
    rp = os.path.realpath(path)
    for root in _ROOTS:
        if rp == root or rp.startswith(root + os.sep):
            return True
    return False


@app.before_request
def _check_auth():
    # 预检请求不鉴权（否则浏览器拿不到 CORS 头）
    if request.method == "OPTIONS":
        return None
    # 工作区/API 口令（可选）
    if request.path.startswith("/api/") and wsmod.Cfg.token:
        got = request.headers.get("X-SEW-Token") or request.args.get("token") or ""
        if got != wsmod.Cfg.token:
            return jsonify(dict(ok=False, error="令牌无效")), 401
    if _AUTH_PAIR is None:
        return None
    a = request.authorization
    if a is None or a.username != _AUTH_PAIR[0] or a.password != _AUTH_PAIR[1]:
        return Response("authentication required", 401,
                        {"WWW-Authenticate": 'Basic realm="SEW web"'})
    return None


# ---------------------------------------------------------------------------
# 可选跨源（默认关闭）：页面与后端不同源时才需要，例如把页面放在别的静态托管上。
#   SEW_CORS="*"                      -> 回显请求的 Origin
#   SEW_CORS="https://a.com,http://localhost:8080"
# ---------------------------------------------------------------------------
_CORS = os.environ.get("SEW_CORS", "")


def _cors_origin(origin: str) -> str:
    if not origin:
        return ""
    if _CORS.strip() == "*":
        return origin
    allowed = [x.strip() for x in _CORS.split(",") if x.strip()]
    return origin if origin in allowed else ""


@app.after_request
def _add_cors(resp):
    origin = request.headers.get("Origin") or ""
    allow = _cors_origin(origin)
    if allow:
        resp.headers["Access-Control-Allow-Origin"] = allow
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,DELETE,OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = \
            "Content-Type, X-SEW-Token, Authorization"
        resp.headers["Access-Control-Expose-Headers"] = "Content-Length"
        # Chrome 私有网络访问（公网页面 -> 127.0.0.1 / 内网地址）
        resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


@app.route("/api/<path:_any>", methods=["OPTIONS"])
@app.route("/api/", methods=["OPTIONS"])
def _api_preflight(_any=""):
    resp = Response("", 204)
    return resp


# ---------------------------------------------------------------------------
# 服务器工作区（templates / results / data）
#   计算本来就在服务器上，这里只补「工作区落在服务器目录」的能力：
#   /api/ws/info|browse|file|list|get  /  /api/ws/root|put  /  /api/ws/delete
# ---------------------------------------------------------------------------
def _ws_err(e):
    code = 404 if isinstance(e, FileNotFoundError) else 403 \
        if isinstance(e, PermissionError) else 400
    return jsonify(dict(ok=False, error=str(e))), code


@app.route("/api/ws/info")
def ws_info():
    try:
        return jsonify(wsmod.info())
    except Exception as e:
        return _ws_err(e)


@app.route("/api/ws/browse")
def ws_browse():
    try:
        return jsonify(wsmod.browse(request.args.get("path", "")))
    except Exception as e:
        return _ws_err(e)


@app.route("/api/ws/file")
def ws_file():
    try:
        p = os.path.abspath(os.path.expanduser(request.args.get("path", "")))
        if not wsmod.in_allow_roots(p):
            raise PermissionError("路径不在允许范围内")
        if not os.path.isfile(p):
            raise FileNotFoundError("文件不存在")
        return send_file(p, mimetype="application/octet-stream",
                         as_attachment=False, download_name=os.path.basename(p))
    except Exception as e:
        return _ws_err(e)


@app.route("/api/ws/list")
def ws_list():
    try:
        return jsonify(wsmod.list_dir(request.args.get("dir", "")))
    except Exception as e:
        return _ws_err(e)


@app.route("/api/ws/get")
def ws_get():
    try:
        p = wsmod.resolve_file(request.args.get("dir", ""),
                               request.args.get("name", ""))
        return send_file(p, mimetype="application/octet-stream",
                         as_attachment=False, download_name=os.path.basename(p))
    except Exception as e:
        return _ws_err(e)


@app.route("/api/ws/root", methods=["POST"])
def ws_root():
    try:
        body = request.get_json(silent=True) or {}
        new_root = body.get("root") or request.args.get("root", "")
        wsmod.set_root(new_root)
        # 模板库跟着走
        templates.set_runtime_dir(os.path.join(wsmod.Cfg.root, "templates"))
        return jsonify(wsmod.info())
    except Exception as e:
        return _ws_err(e)


@app.route("/api/ws/put", methods=["POST"])
def ws_put():
    try:
        d = request.args.get("dir", "")
        name = request.args.get("name", "")
        length = int(request.headers.get("Content-Length") or 0)
        out = wsmod.write_stream(d, name,
                                 lambda n: request.stream.read(n), length)
        if str(d).strip("/") == "templates":
            templates.set_runtime_dir(os.path.join(wsmod.Cfg.root, "templates"))
        return jsonify(out)
    except Exception as e:
        return _ws_err(e)


@app.route("/api/ws/delete", methods=["DELETE", "POST"])
def ws_delete():
    try:
        d = request.args.get("dir", "")
        name = request.args.get("name", "")
        return jsonify(wsmod.delete_file(d, name))
    except Exception as e:
        return _ws_err(e)


# S/N 参考波段（观测系，Å）：与 PowerBin 分 bin 的估算波段保持一致
SNR_BAND = (5300.0, 5700.0)

# ---------------------------------------------------------------------------
# 全局状态（单用户本地工具）
# ---------------------------------------------------------------------------
STATE = {
    "ds": None,
    "ds_override": {},
    "jobs": {},
    "lock": threading.Lock(),
    "fit_lock": threading.Lock(),
}


def _err(msg, code=400, **extra):
    payload = dict(ok=False, error=str(msg))
    payload.update(extra)
    return jsonify(payload), code


def _ds():
    ds = STATE.get("ds")
    if ds is None:
        raise RuntimeError("尚未加载 FITS 文件")
    return ds


def _json_clean(o):
    """递归把 NaN/Inf/numpy 标量转成 JSON 合法值。

    ★ Python json 默认输出 NaN/Infinity（非法 JSON），浏览器 JSON.parse 直接
    失败，表现为“HTTP 200 却解析错误”。拟合结果里的 chi2/rms/气体流量都
    可能是 NaN（零气体模板），必须在这里兜住。
    """
    if isinstance(o, dict):
        return {str(k): _json_clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        v = float(o)
        return v if math.isfinite(v) else None
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, np.ndarray):
        return _json_clean(o.tolist())
    return o


# ---------------------------------------------------------------------------
# 静态页面
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(_page_dir(), "index.html")


@app.route("/static/<path:fn>")
def static_file(fn):
    return send_from_directory(STATIC_DIR, fn)


@app.route("/templates/<path:fn>")
def wasm_templates(fn):
    """浏览器端版本的模板目录（服务器需要下载模板时也走这里）。"""
    d = os.path.join(BASE_DIR, "webapp", "templates")
    return send_from_directory(d, fn)


@app.route("/vendor/plotly.min.js")
def plotly_js():
    try:
        import plotly
        p = os.path.join(os.path.dirname(plotly.__file__),
                         "package_data", "plotly.min.js")
        return send_file(p, mimetype="application/javascript")
    except Exception as e:
        return _err(f"plotly.js 不可用: {e}", 500)


@app.route("/api/health")
def health():
    import ppxf
    import ppxf_sew
    try:
        from importlib.metadata import version as _ver
        _pb_ver = _ver("powerbin")
    except Exception:
        _pb_ver = "?"
    return jsonify(ok=True, ppxf=ppxf.__version__,
                   ppxf_sew=getattr(ppxf_sew, "__version__", "?"),
                   powerbin=_pb_ver,
                   root=BASE_DIR)


@app.route("/api/samples")
def samples():
    cands = [
        os.path.join(DATA_DIR, "samples", "spectrum.fit"),
        os.path.join(WEBAPP_DIR, "data", "samples", "spectrum.fit"),
    ]
    return jsonify(ok=True, samples=[p for p in cands if os.path.isfile(p)])


# ---------------------------------------------------------------------------
# 谱线表（line_use.dat + ppxf_sew 拟合发射线）
#   路径按顺序探测：SEW_LINE_TABLE → data/ → webapp/data/ → 本机旧路径
# ---------------------------------------------------------------------------
_LINE_CACHE = {"key": None, "data": None}


def _line_table_candidates():
    env = os.environ.get("SEW_LINE_TABLE") or ""
    return [p for p in (env,
                        os.path.join(DATA_DIR, "line_use.dat"),
                        os.path.join(WEBAPP_DIR, "data", "line_use.dat")) if p]


def _line_table() -> str:
    for p in _line_table_candidates():
        if os.path.isfile(p):
            return p
    return _line_table_candidates()[0]


def _load_line_table():
    """读取谱线表（真空波长 Å）+ ppxf_sew 缺省拟合发射线。结果缓存。"""
    line_table = _line_table()
    try:
        st = os.stat(line_table)
        key = (line_table, st.st_mtime, st.st_size)
    except OSError:
        key = (line_table, None, None)
    if _LINE_CACHE["key"] == key and _LINE_CACHE["data"] is not None:
        return _LINE_CACHE["data"]
    rows = []
    if os.path.isfile(line_table):
        with open(line_table, encoding="utf-8") as f:
            next(f, None)                     # 跳过表头
            for ln in f:
                p = ln.rstrip("\n").split("\t")
                if len(p) < 3:
                    continue
                try:
                    lam = float(p[0])
                except ValueError:
                    continue
                rows.append(dict(lam=lam, name=p[1].strip(),
                                 kind=p[2].strip()))
    fit = []
    try:
        import numpy as np
        from ppxf_sew import ew_util as _ew
        _lam = np.linspace(3600.0, 10900.0, 1500)
        out = _ew.all_temp_make(
            np.ones((_lam.size, 1)), _lam,
            lam_range_gal=np.array([3650.0, 10800.0]),
            FWHM_gal=lambda x: np.asarray(x, float) / 2000.0,
            vacuum="vacuum")
        names, waves = list(out[5]), out[6]
        for nm, w in zip(names, waves):
            for ww in np.atleast_1d(w):
                fit.append(dict(lam=float(ww), name=str(nm)))
    except Exception:
        pass
    data = dict(path=line_table, lines=rows, fit_em=fit)
    _LINE_CACHE.update(key=key, data=data)
    return data


@app.route("/api/lines")
def lines_api():
    try:
        return jsonify(ok=True, **_load_line_table())
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# 文件浏览 / 上传
# ---------------------------------------------------------------------------
@app.route("/api/browse")
def browse():
    default_dir = _ROOTS[0] if _ROOTS else os.path.expanduser("~")
    path = request.args.get("path") or default_dir
    path = os.path.abspath(os.path.expanduser(path))
    if not _in_roots(path):
        return _err(f"路径不在允许范围内: {path}", 403)
    if not os.path.isdir(path):
        return _err(f"目录不存在: {path}")
    dirs, files = [], []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir():
                        dirs.append(dict(name=e.name, path=e.path))
                    elif e.is_file() and e.name.lower().endswith(
                            (".fits", ".fit", ".fts", ".fits.gz")):
                        st = e.stat()
                        files.append(dict(name=e.name, path=e.path,
                                          size=int(st.st_size),
                                          mtime=float(st.st_mtime)))
                except OSError:
                    continue
    except PermissionError:
        return _err(f"无权限读取: {path}")
    dirs.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())
    parent = os.path.dirname(path)
    if parent == path or not _in_roots(parent):
        parent = path
    return jsonify(ok=True, path=path, parent=parent, dirs=dirs, files=files)


@app.route("/api/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if f is None:
        return _err("没有上传文件")
    fn = secure_filename(f.filename or "upload.fits")
    dst = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex[:8]}_{fn}")
    f.save(dst)
    return jsonify(ok=True, path=dst, name=fn, size=os.path.getsize(dst))


# ---------------------------------------------------------------------------
# 打开 / 识别
# ---------------------------------------------------------------------------
@app.route("/api/open", methods=["POST"])
def open_file():
    body = request.get_json(force=True, silent=True) or {}
    path = body.get("path")
    override = body.get("override") or {}
    if not path:
        return _err("缺少 path")
    path = os.path.abspath(os.path.expanduser(path))
    if not _in_roots(path):
        return _err(f"路径不在允许范围内: {path}", 403)
    try:
        with STATE["lock"]:
            old = STATE.get("ds")
            ds = fits_io.open_dataset(path, override)
            if old is not None:
                old.close()
            STATE["ds"] = ds
            STATE["ds_override"] = override
        meta = ds.meta()
        meta["fwhm"] = fitting.detect_fwhm(ds)
        meta["samples"] = None
        return jsonify(ok=True, **_json_clean(meta))
    except Exception as e:
        traceback.print_exc()
        # 加载失败：关闭旧数据集，避免前端显示新文件却让 /api/image 返回旧数据
        with STATE["lock"]:
            old = STATE.get("ds")
            STATE["ds"] = None
            if old is not None:
                old.close()
        return _err(f"{type(e).__name__}: {e}", path=path)


@app.route("/api/inspect", methods=["POST"])
def inspect_path():
    """只读文件结构（自动识别失败后用于手动指定 HDU/列）。"""
    body = request.get_json(force=True, silent=True) or {}
    path = body.get("path")
    if not path:
        return _err("缺少 path")
    path = os.path.abspath(os.path.expanduser(path))
    if not _in_roots(path):
        return _err(f"路径不在允许范围内: {path}", 403)
    try:
        out = fits_io.inspect_file(path)
        return jsonify(ok=True, **_json_clean(out))
    except Exception as e:
        traceback.print_exc()
        return _err(f"{type(e).__name__}: {e}")


@app.route("/api/meta")
def meta():
    try:
        ds = _ds()
        out = ds.meta()
        out["fwhm"] = fitting.detect_fwhm(ds)
        return jsonify(ok=True, **_json_clean(out))
    except Exception as e:
        return _err(e)


@app.route("/api/close", methods=["POST"])
def close_file():
    with STATE["lock"]:
        ds = STATE.get("ds")
        if ds is not None:
            ds.close()
        STATE["ds"] = None
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# 显示
# ---------------------------------------------------------------------------
@app.route("/api/image")
def image():
    try:
        ds = _ds()
        mode = request.args.get("mode", "white")
        wmin = request.args.get("wave_min", type=float)
        wmax = request.args.get("wave_max", type=float)
        max_size = request.args.get("max_size", type=int) or 360
        out = ds.image(mode=mode, wave_min=wmin, wave_max=wmax,
                       max_size=max_size)
        out["ok"] = True
        return jsonify(_json_clean(out))
    except Exception as e:
        return _err(e)


@app.route("/api/bins")
def bins():
    """PowerBin 分 bin（cube）：?target_sn=50&wave_min=&wave_max=&max_bins="""
    try:
        ds = _ds()
        if ds.kind != "cube":
            return _err("只有数据立方才能分 bin")
        sn = request.args.get("target_sn", type=float) or 10.0
        lo = request.args.get("wave_min", type=float)
        hi = request.args.get("wave_max", type=float)
        max_bins = request.args.get("max_bins", type=int) or 400
        band = (lo, hi) if (lo is not None and hi is not None and hi > lo) \
            else None
        ds.make_bins(target_sn=sn, band=band, max_bins=max_bins)
        out = ds.bins_meta()
        out["ok"] = True
        return jsonify(_json_clean(out))
    except Exception as e:
        traceback.print_exc()
        return _err(e)


def _band_snr(wave, flux, err, mask=None, band=None):
    """观测 5500 Å（默认 5300–5700 Å）处的逐像素信噪比（中位数）。

    与 PowerBin 的目标 S/N 同一尺度（band_stats 用均值 flux / 均值误差），
    因此分 bin 时显示的 S/N 与这里可以直接对比。
    """
    lo, hi = band or SNR_BAND
    m = (wave >= lo) & (wave <= hi) & np.isfinite(flux) & np.isfinite(err) \
        & (err > 0)
    if mask is not None:
        m = m & (~mask)
    if not m.any():
        return None
    with np.errstate(invalid="ignore", divide="ignore"):
        snr = flux[m].astype(float) / err[m].astype(float)
    snr = snr[np.isfinite(snr)]
    if snr.size == 0:
        return None
    return float(np.median(snr))


@app.route("/api/spectrum")
def spectrum():
    try:
        ds = _ds()
        x = request.args.get("x", type=int)
        y = request.args.get("y", type=int)
        row = request.args.get("row", type=int)
        ap = request.args.get("aperture", type=float) or 0.0
        bsel = request.args.get("bin", type=int)
        sp = ds.spectrum(x=x, y=y, row=row, aperture=ap, bin_id=bsel)
        w = sp["wave"]
        flux = sp["flux"]
        var = sp["var"]
        mask = sp["mask"]
        err = (np.sqrt(var) if var is not None
               else np.full_like(flux, np.nan))
        good = (~mask) & np.isfinite(flux)
        # 观测 5500 Å 处（5300–5700 Å）的信噪比（与分 bin 目标 S/N 同尺度）
        snr = _band_snr(w, flux, err, mask) if var is not None else None
        # 全谱逐像素 S/N 中位数（辅助参考）
        snr_med = float(np.nanmedian(flux[good] / err[good])) \
            if (good.any() and var is not None) else None
        out = dict(
            ok=True,
            kind=sp.get("kind"), x=sp.get("x"), y=sp.get("y"),
            row=sp.get("row"), aperture=sp.get("aperture"),
            bin=sp.get("bin"),
            npix=sp.get("npix", 1), snr=snr, snr_med=snr_med,
            snr_band=list(SNR_BAND),
            nwave=int(w.size),
            wave=np.round(w, 6).tolist(),
            flux=np.where(np.isfinite(flux), flux, None).tolist(),
            err=np.where(np.isfinite(err), err, None).tolist(),
            mask=mask.astype(int).tolist(),
            wave_min=float(np.nanmin(w)), wave_max=float(np.nanmax(w)),
        )
        return jsonify(_json_clean(out))
    except Exception as e:
        traceback.print_exc()
        return _err(e)


# ---------------------------------------------------------------------------
# 红移 / FWHM 辅助
# ---------------------------------------------------------------------------
@app.route("/api/fwhm")
def fwhm_info():
    try:
        ds = _ds()
        return jsonify(ok=True, **fitting.detect_fwhm(ds))
    except Exception as e:
        return _err(e)


@app.route("/api/redshift", methods=["POST"])
def redshift():
    try:
        ds = _ds()
        body = request.get_json(force=True, silent=True) or {}
        sp = ds.spectrum(x=body.get("x"), y=body.get("y"),
                         row=body.get("row"),
                         aperture=body.get("aperture") or 0,
                         bin_id=body.get("bin"))
        name = body.get("template")
        tinfo = templates.find_template(name) if name else None
        zr = body.get("z_range") or [-0.01, 0.2]
        out = fitting.estimate_redshift(
            sp["wave"], sp["flux"],
            template_path=(tinfo or {}).get("path"), z_range=tuple(zr),
            obs_vacuum=body.get("obs_vacuum", "air"),
            template_vacuum=body.get("template_vacuum", "air"))
        return jsonify(ok=True, **_json_clean(out))
    except Exception as e:
        traceback.print_exc()
        return _err(e)


# ---------------------------------------------------------------------------
# 模板库
# ---------------------------------------------------------------------------
@app.route("/api/templates")
def template_list():
    try:
        return jsonify(ok=True, templates=templates.list_templates(),
                       registry=templates.load_registry(),
                       dirs=templates.search_dirs())
    except Exception as e:
        return _err(e)


_TPL_PROG = {}      # 模板文件名 -> {done, total, ts, state}


@app.route("/api/templates/progress")
def template_progress():
    fn = request.args.get("file", "")
    j = _TPL_PROG.get(os.path.basename(fn)) or {}
    return jsonify(ok=True, file=fn, **j)


@app.route("/api/templates/download", methods=["POST"])
def template_download():
    body = request.get_json(force=True, silent=True) or {}
    name = body.get("name") or "template"
    url = body.get("url")
    file = body.get("file")
    if not wsmod.Cfg.root:
        return _err("请先在工作区设置里选择目录", 400)
    fn = os.path.basename(str(file or (url or "").split("?")[0] or name))
    if not fn.lower().endswith(".npz"):
        fn = f"spectra_{name}_9.0.npz"
    prog = {"done": 0, "total": 0, "ts": time.time(), "state": "running"}
    _TPL_PROG[fn] = prog

    def _cb(done, total):
        prog.update(done=int(done), total=int(total or 0), ts=time.time())

    try:
        # 1) 优先用 SEW_web 自带的可下载模板（webapp/templates/*.npz）
        if file or (url and not str(url).lower().startswith(("http://", "https://"))):
            src = os.path.join(WEBAPP_DIR, "templates", fn)
            if not os.path.isfile(src):
                raise FileNotFoundError(f"自带模板不存在: {fn}（检查 webapp/templates/）")
            dst = os.path.join(templates.tpl_local(), fn)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            p = dst
        else:
            p = templates.download_template(name, url, progress=_cb)
        prog.update(state="done", ts=time.time())
        return jsonify(ok=True, path=p, name=name)
    except Exception as e:
        prog.update(state="error", ts=time.time())
        return _err(f"下载失败: {e}")


@app.route("/api/templates/upload", methods=["POST"])
def template_upload():
    f = request.files.get("file")
    if f is None:
        return _err("没有上传模板文件")
    if not wsmod.Cfg.root:
        return _err("请先在工作区设置里选择目录", 400)
    name = request.form.get("name") or None
    try:
        p = templates.save_uploaded_template(f, name=name)
        return jsonify(ok=True, path=p, name=name)
    except Exception as e:
        return _err(f"模板上传失败: {e}")


# ---------------------------------------------------------------------------
# 拟合
# ---------------------------------------------------------------------------
def _job_update(jid, **kw):
    with STATE["lock"]:
        j = STATE["jobs"].get(jid)
        if j is not None:
            j.update(kw)
            j["updated"] = time.time()


@app.route("/api/fit", methods=["POST"])
def fit_start():
    try:
        ds = _ds()
        body = request.get_json(force=True, silent=True) or {}
        target = body.get("target") or {}
        cfg = body.get("cfg") or {}
        sp = ds.spectrum(x=target.get("x"), y=target.get("y"),
                         row=target.get("row"),
                         aperture=target.get("aperture") or 0,
                         bin_id=target.get("bin"))
        # 目标描述
        if sp.get("kind") == "cube":
            if sp.get("bin") is not None:
                tdesc = (f"bin #{int(sp['bin'])} ({sp.get('npix', 1)} px, "
                         f"centroid {sp['x']},{sp['y']})")
            else:
                tdesc = (f"pixel ({sp['x']},{sp['y']})"
                         + (f" aperture r={sp['aperture']:g}"
                            if sp.get("aperture") else ""))
        else:
            tdesc = "spectrum" + (f" row {sp.get('row')}"
                                  if ds.nrows > 1 else "")

        tinfo = templates.find_template(cfg.get("template"))
        if tinfo is None:
            return _err(f"模板不可用: {cfg.get('template')}。"
                        "请先在模板面板下载/上传模板。")

        jid = uuid.uuid4().hex[:12]
        with STATE["lock"]:
            STATE["jobs"][jid] = dict(
                id=jid, status="running", progress=0.0, message="排队中",
                pkey="queued", pargs={},
                started=time.time(), updated=time.time(), target=tdesc,
                result=None, error=None, log=[])

        def _progress(frac, msg, key=None, **pargs):
            _job_update(jid, progress=float(frac), message=str(msg),
                        pkey=key, pargs=pargs)

        def _worker():
            with STATE["fit_lock"]:
                try:
                    _job_update(jid, message="提取光谱", pkey="extract",
                                pargs={})
                    res = fitting.run_fit(
                        sp["wave"], sp["flux"], sp["var"], sp["mask"],
                        cfg, tinfo, progress=_progress,
                        log=lambda s: STATE["jobs"][jid]["log"].append(str(s)))
                    _job_update(jid, status="done", progress=1.0,
                                message="完成", pkey="done", pargs={},
                                result=res)
                except Exception as e:
                    traceback.print_exc()
                    _job_update(jid, status="error", message=str(e),
                                error=f"{type(e).__name__}: {e}",
                                traceback=traceback.format_exc())

        threading.Thread(target=_worker, daemon=True).start()
        return jsonify(ok=True, job_id=jid, target=tdesc)
    except Exception as e:
        traceback.print_exc()
        return _err(e)


@app.route("/api/fit/status/<jid>")
def fit_status(jid):
    with STATE["lock"]:
        j = STATE["jobs"].get(jid)
        if j is None:
            return _err("任务不存在", 404)
        out = dict(id=j["id"], status=j["status"],
                   progress=j["progress"], message=j["message"],
                   pkey=j.get("pkey"), pargs=j.get("pargs"),
                   target=j.get("target"), error=j.get("error"),
                   log=list(j.get("log") or []),
                   started=j.get("started"), updated=j.get("updated"))
        if j["status"] == "done" and j.get("result") is not None:
            out["result"] = _json_clean(j["result"])
        return jsonify(ok=True, **_json_clean(out))


@app.route("/api/fit/export/<jid>")
def fit_export(jid):
    fmt = (request.args.get("fmt") or "json").lower()
    with STATE["lock"]:
        j = STATE["jobs"].get(jid)
    if not j or j.get("status") != "done" or not j.get("result"):
        return _err("结果不存在或尚未完成", 404)
    res = j["result"]
    if fmt == "json":
        buf = io.BytesIO(json.dumps(_json_clean(res), ensure_ascii=False,
                                    allow_nan=False, indent=1).encode("utf-8"))
        return send_file(buf, mimetype="application/json",
                         download_name=f"sew_fit_{jid}.json")
    # FITS
    from astropy.io import fits
    ph = fits.PrimaryHDU()
    ph.header["ENGINE"] = res.get("engine", "")
    ph.header["TEMPLATE"] = str(res.get("template", {}).get("name"))
    ph.header["REDSHIFT"] = res.get("z", 0.0)
    ph.header["CHI2"] = res.get("chi2", 0.0)
    ph.header["DATE"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    cols = [fits.Column(name="WAVE_REST", format="D",
                        array=np.asarray(res["wave_rest"], float)),
            fits.Column(name="WAVE_OBS", format="D",
                        array=np.asarray(res["wave_obs"], float)),
            fits.Column(name="GALAXY", format="D",
                        array=np.asarray(res["galaxy"], float)),
            fits.Column(name="BESTFIT", format="D",
                        array=np.asarray(res["bestfit"], float)),
            fits.Column(name="STELLAR", format="D",
                        array=np.asarray(res["stellar"], float)),
            fits.Column(name="GAS", format="D",
                        array=np.asarray(res["gas_model"], float)),
            fits.Column(name="RESIDUAL", format="D",
                        array=np.asarray(res["residual"], float)),
            fits.Column(name="NOISE", format="D",
                        array=np.asarray(res["noise"], float)),
            fits.Column(name="GOOD", format="J",
                        array=np.asarray(res["good"], int))]
    hdus = [ph, fits.BinTableHDU.from_columns(cols, name="FIT")]
    if res.get("weights") is not None:
        hdus.append(fits.ImageHDU(data=np.asarray(res["weights"], float),
                                  name="WEIGHTS"))
    buf = io.BytesIO()
    fits.HDUList(hdus).writeto(buf)
    buf.seek(0)
    return send_file(buf, mimetype="application/fits",
                     download_name=f"sew_fit_{jid}.fits")


@app.errorhandler(413)
def too_large(e):
    return _err("上传文件超过 4 GB 限制", 413)


# ---------------------------------------------------------------------------
# 兜底静态文件：webapp/ 构建产物（app.js / i18n.js / workspace.js / rpc.js …）
# 放在最后注册，避免抢占 /api/* 等具体路由。
# ---------------------------------------------------------------------------
@app.route("/<path:fn>")
def _fallback_static(fn):
    d = _page_dir()
    if os.path.isfile(os.path.join(d, fn)):
        return send_from_directory(d, fn)
    if d != STATIC_DIR and os.path.isfile(os.path.join(STATIC_DIR, fn)):
        return send_from_directory(STATIC_DIR, fn)
    return _err(f"找不到 {fn}", 404)


def create_app():
    return app
