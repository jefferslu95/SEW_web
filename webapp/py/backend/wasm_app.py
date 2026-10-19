# -*- coding: utf-8 -*-
"""浏览器端（Pyodide）的「后端」：把 backend/app.py 的 Flask 路由搬成纯函数。

对外只有一个入口：

    req = WasmApp()
    req.handle("POST", "/api/fit", query={}, body={...}, progress=cb)

返回值是可直接 JSON 序列化的 dict。Flask 特有的东西（request/jsonify/
send_file/多线程任务）都换成了普通返回值：
  * 拟合同步执行，完成后 job 状态直接是 done；
  * 进度通过 progress 回调（JS 函数）推给前端；
  * 导出返回 base64，由 JS 做 Blob 下载。
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import time
import traceback
import uuid
from datetime import datetime

import numpy as np

from . import fits_io, fitting, templates

SNR_BAND = (5300.0, 5700.0)
DATA_DIR = "/sew/data"
TPL_DIR = os.path.join(DATA_DIR, "templates")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
SAMPLE_DIR = os.path.join(DATA_DIR, "samples")
LINE_TABLE = os.path.join(DATA_DIR, "line_use.dat")

for _d in (TPL_DIR, UPLOAD_DIR, SAMPLE_DIR):
    os.makedirs(_d, exist_ok=True)


# ---------------------------------------------------------------------------
def _json_clean(obj):
    """把 numpy / NaN / inf 等转成 JSON 友好的形式（与 app.py 一致）。"""
    if isinstance(obj, dict):
        return {str(k): _json_clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_clean(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _json_clean(obj.tolist())
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return v if math.isfinite(v) else None
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    return str(obj)


def _band_snr(wave, flux, err, mask=None, band=None):
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
    return float(np.median(snr)) if snr.size else None


# ---------------------------------------------------------------------------
class WasmApp:
    def __init__(self):
        self.ds = None
        self.ds_override = {}
        self.jobs = {}

    # ---------------- 基础 ----------------
    def _ds(self):
        if self.ds is None:
            raise RuntimeError("尚未加载 FITS 文件")
        return self.ds

    @staticmethod
    def _ok(**kw):
        d = dict(ok=True)
        d.update(kw)
        return _json_clean(d)

    @staticmethod
    def _err(msg, code=400, **kw):
        d = dict(ok=False, error=str(msg), code=int(code))
        d.update(kw)
        return _json_clean(d)

    # ---------------- 分发 ----------------
    def handle(self, method, path, query=None, body=None, progress=None):
        query = query or {}
        body = body or {}
        path = str(path)
        try:
            if path == "/api/health":
                import ppxf
                import ppxf_sew
                from importlib.metadata import version as _ver
                return self._ok(ppxf=ppxf.__version__,
                                ppxf_sew=getattr(ppxf_sew, "__version__", "?"),
                                powerbin=_ver("powerbin"),
                                engine="pyodide/wasm")
            if path == "/api/samples":
                out = []
                for fn in sorted(os.listdir(SAMPLE_DIR)):
                    if fn.lower().endswith((".fits", ".fit", ".fts")):
                        out.append(os.path.join(SAMPLE_DIR, fn))
                return self._ok(samples=out)
            if path == "/api/lines":
                return self._ok(**self._line_table())
            if path == "/api/open":
                return self.open_file(body)
            if path == "/api/inspect":
                return self.inspect_path(body)
            if path == "/api/meta":
                return self.meta()
            if path == "/api/close":
                self.close_file()
                return self._ok()
            if path == "/api/image":
                return self.image(query)
            if path == "/api/bins":
                return self.bins(query)
            if path == "/api/spectrum":
                return self.spectrum(query)
            if path == "/api/fwhm":
                return self._ok(**fitting.detect_fwhm(self._ds()))
            if path == "/api/redshift":
                return self.redshift(body)
            if path == "/api/templates":
                return self._ok(templates=templates.list_templates(),
                                registry=templates.load_registry(),
                                dirs=templates.search_dirs())
            if path == "/api/fit":
                return self.fit_start(body, progress=progress)
            if path.startswith("/api/fit/status/"):
                return self.fit_status(path.rsplit("/", 1)[-1])
            if path.startswith("/api/fit/export/"):
                jid = path.rsplit("/", 1)[-1]
                return self.fit_export(jid, query.get("fmt", "json"))
            if path == "/api/_restore":
                return self.restore_job(body.get("id"), body.get("result"))
            return self._err(f"未知接口: {path}", 404)
        except Exception as e:
            traceback.print_exc()
            return self._err(f"{type(e).__name__}: {e}")

    # ---------------- 谱线表 ----------------
    _lines_cache = None

    def _line_table(self):
        if WasmApp._lines_cache is not None:
            return WasmApp._lines_cache
        rows = []
        if os.path.isfile(LINE_TABLE):
            with open(LINE_TABLE, encoding="utf-8") as f:
                next(f, None)
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
        WasmApp._lines_cache = dict(path=LINE_TABLE, lines=rows, fit_em=fit)
        return WasmApp._lines_cache

    # ---------------- 文件 ----------------
    def open_file(self, body):
        path = body.get("path")
        override = body.get("override") or {}
        if not path:
            return self._err("缺少 path")
        try:
            old = self.ds
            ds = fits_io.open_dataset(path, override)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            self.ds = ds
            self.ds_override = override
            meta = ds.meta()
            meta["fwhm"] = fitting.detect_fwhm(ds)
            meta["samples"] = None
            d = dict(ok=True)
            d.update(meta)
            return _json_clean(d)
        except Exception as e:
            traceback.print_exc()
            self.ds = None
            return self._err(f"{type(e).__name__}: {e}", path=path)

    def inspect_path(self, body):
        path = body.get("path")
        if not path:
            return self._err("缺少 path")
        try:
            return self._ok(**fits_io.inspect_file(path))
        except Exception as e:
            traceback.print_exc()
            return self._err(f"{type(e).__name__}: {e}")

    def meta(self):
        ds = self._ds()
        out = ds.meta()
        out["fwhm"] = fitting.detect_fwhm(ds)
        return self._ok(**out)

    def close_file(self):
        if self.ds is not None:
            try:
                self.ds.close()
            except Exception:
                pass
        self.ds = None

    # ---------------- 显示 ----------------
    def image(self, q):
        ds = self._ds()
        out = ds.image(mode=q.get("mode", "white"),
                       wave_min=_f(q.get("wave_min")),
                       wave_max=_f(q.get("wave_max")),
                       max_size=int(_f(q.get("max_size")) or 360))
        out["ok"] = True
        return _json_clean(out)

    def bins(self, q):
        ds = self._ds()
        if ds.kind != "cube":
            return self._err("只有数据立方才能分 bin")
        sn = _f(q.get("target_sn")) or 10.0
        lo, hi = _f(q.get("wave_min")), _f(q.get("wave_max"))
        max_bins = int(_f(q.get("max_bins")) or 400)
        band = (lo, hi) if (lo is not None and hi is not None and hi > lo) \
            else None
        ds.make_bins(target_sn=sn, band=band, max_bins=max_bins)
        out = ds.bins_meta()
        out["ok"] = True
        return _json_clean(out)

    def spectrum(self, q):
        ds = self._ds()
        sp = ds.spectrum(x=_i(q.get("x")), y=_i(q.get("y")),
                         row=_i(q.get("row")),
                         aperture=_f(q.get("aperture")) or 0.0,
                         bin_id=_i(q.get("bin")))
        w, flux, var, mask = sp["wave"], sp["flux"], sp["var"], sp["mask"]
        err = (np.sqrt(var) if var is not None
               else np.full_like(flux, np.nan))
        good = (~mask) & np.isfinite(flux)
        snr = _band_snr(w, flux, err, mask) if var is not None else None
        snr_med = (float(np.nanmedian(flux[good] / err[good]))
                   if (good.any() and var is not None) else None)
        out = dict(
            ok=True, kind=sp.get("kind"), x=sp.get("x"), y=sp.get("y"),
            row=sp.get("row"), aperture=sp.get("aperture"), bin=sp.get("bin"),
            npix=sp.get("npix", 1), snr=snr, snr_med=snr_med,
            snr_band=list(SNR_BAND), nwave=int(w.size),
            wave=np.round(w, 6).tolist(),
            flux=np.where(np.isfinite(flux), flux, None).tolist(),
            err=np.where(np.isfinite(err), err, None).tolist(),
            mask=mask.astype(int).tolist(),
            wave_min=float(np.nanmin(w)), wave_max=float(np.nanmax(w)),
        )
        return _json_clean(out)

    def redshift(self, body):
        ds = self._ds()
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
        return self._ok(**out)

    # ---------------- 拟合 ----------------
    def fit_start(self, body, progress=None):
        ds = self._ds()
        target = body.get("target") or {}
        cfg = body.get("cfg") or {}
        sp = ds.spectrum(x=target.get("x"), y=target.get("y"),
                         row=target.get("row"),
                         aperture=target.get("aperture") or 0,
                         bin_id=target.get("bin"))
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
            return self._err(f"模板不可用: {cfg.get('template')}。"
                             "请先在模板面板下载/上传模板。")

        jid = uuid.uuid4().hex[:12]
        self.jobs[jid] = dict(id=jid, status="running", progress=0.0,
                              message="提取光谱", pkey="extract", pargs={},
                              started=time.time(), target=tdesc,
                              result=None, error=None, log=[])

        def _progress(frac, msg, key=None, **pargs):
            j = self.jobs.get(jid)
            if j is not None:
                j.update(progress=float(frac), message=str(msg),
                         pkey=key, pargs=pargs, updated=time.time())
            if progress is not None:
                try:
                    progress(float(frac), str(msg), key or "",
                             json.dumps(_json_clean(pargs)))
                except Exception:
                    pass

        try:
            res = fitting.run_fit(
                sp["wave"], sp["flux"], sp["var"], sp["mask"], cfg, tinfo,
                progress=_progress,
                log=lambda s: self.jobs[jid]["log"].append(str(s)))
            self.jobs[jid].update(status="done", progress=1.0, message="完成",
                                  pkey="done", pargs={}, result=res,
                                  updated=time.time())
        except Exception as e:
            traceback.print_exc()
            self.jobs[jid].update(status="error", message=str(e),
                                  error=f"{type(e).__name__}: {e}",
                                  updated=time.time())
        return self._ok(job_id=jid, target=tdesc)

    def fit_status(self, jid):
        j = self.jobs.get(jid)
        if j is None:
            return self._err("任务不存在", 404)
        out = dict(id=j["id"], status=j["status"], progress=j["progress"],
                   message=j["message"], pkey=j.get("pkey"),
                   pargs=j.get("pargs"), target=j.get("target"),
                   error=j.get("error"), log=list(j.get("log") or []),
                   started=j.get("started"), updated=j.get("updated"))
        if j["status"] == "done" and j.get("result") is not None:
            out["result"] = j["result"]
        return self._ok(**out)

    def restore_job(self, jid, result):
        """把工作区里保存的结果重新登记成任务（便于再次导出）。"""
        if not jid or result is None:
            return self._err("缺少 id/result")
        self.jobs[str(jid)] = dict(id=str(jid), status="done", progress=1.0,
                                   message="完成", pkey="done", pargs={},
                                   started=time.time(), target="",
                                   result=result, error=None, log=[])
        return self._ok()

    def fit_export(self, jid, fmt="json"):
        j = self.jobs.get(jid)
        if not j or j.get("status") != "done" or not j.get("result"):
            return self._err("结果不存在或尚未完成", 404)
        res = j["result"]
        if str(fmt).lower() == "json":
            raw = json.dumps(_json_clean(res), ensure_ascii=False,
                             allow_nan=False, indent=1).encode("utf-8")
            return self._ok(fmt="json", mime="application/json",
                            filename=f"sew_fit_{jid}.json",
                            b64=base64.b64encode(raw).decode("ascii"))
        from astropy.io import fits
        ph = fits.PrimaryHDU()
        ph.header["ENGINE"] = str(res.get("engine", ""))
        ph.header["TEMPLATE"] = str((res.get("template") or {}).get("name"))
        ph.header["REDSHIFT"] = float(res.get("z") or 0.0)
        ph.header["CHI2"] = float(res.get("chi2") or 0.0)
        ph.header["DATE"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        cols = [fits.Column(name=n, format="D",
                            array=np.asarray(res[k], float))
                for n, k in [("WAVE_REST", "wave_rest"),
                             ("WAVE_OBS", "wave_obs"),
                             ("GALAXY", "galaxy"), ("BESTFIT", "bestfit"),
                             ("STELLAR", "stellar"), ("GAS", "gas_model"),
                             ("RESIDUAL", "residual"), ("NOISE", "noise")]]
        cols.append(fits.Column(name="GOOD", format="J",
                                array=np.asarray(res["good"], int)))
        hdus = [ph, fits.BinTableHDU.from_columns(cols, name="FIT")]
        if res.get("weights") is not None:
            hdus.append(fits.ImageHDU(data=np.asarray(res["weights"], float),
                                      name="WEIGHTS"))
        buf = io.BytesIO()
        fits.HDUList(hdus).writeto(buf)
        return self._ok(fmt="fits", mime="application/fits",
                        filename=f"sew_fit_{jid}.fits",
                        b64=base64.b64encode(buf.getvalue()).decode("ascii"))


# ---------------------------------------------------------------------------
def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        if v is None or v == "":
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


APP = WasmApp()


def sew_request(method, path, query_json="{}", body_json="{}", progress=None):
    try:
        q = json.loads(query_json or "{}")
    except Exception:
        q = {}
    try:
        b = json.loads(body_json or "{}")
    except Exception:
        b = {}
    out = APP.handle(method, path, query=q, body=b, progress=progress)
    return json.dumps(out, allow_nan=False)
