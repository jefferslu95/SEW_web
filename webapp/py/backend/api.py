# -*- coding: utf-8 -*-
"""浏览器端（Pyodide）入口：把 backend 里的算法直接跑在网页里。

与 Flask 版共用同一套 `fits_io / fitting / templates`，只是把 HTTP 层换成
JS 直接调用。所有路径都在 Pyodide 的内存文件系统（MEMFS）里：
    /sew/backend/*.py      <- 由 worker.js 写入
    /sew/data/templates/*  <- 用户选择的模板 npz
    /sew/data/uploads/*    <- 用户选择的 FITS
"""
from __future__ import annotations

import json
import os
import traceback

import numpy as np

from . import fits_io, fitting, templates

DATA_DIR = "/sew/data"
TPL_DIR = os.path.join(DATA_DIR, "templates")


def _spec_of(ds):
    """取一条谱：cube 取中心像素，其余直接取整条。"""
    meta = ds.meta()
    if meta.get("kind") == "cube":
        x = int(meta["nx"]) // 2
        y = int(meta["ny"]) // 2
        return ds.spectrum(x=x, y=y), dict(x=x, y=y, bin=None, aperture=0)
    return ds.spectrum(), dict(x=None, y=None, row=int(meta.get("row") or 0),
                               bin=None, aperture=0)


def _default_cfg(sp, tpl_name):
    wave = np.asarray(sp["wave"], float)
    good = np.isfinite(wave)
    lo, hi = float(np.nanmin(wave[good])), float(np.nanmax(wave[good]))
    return {
        "template": tpl_name,
        "engine": "ppxf_sew",
        "obs_vacuum": "air",
        "template_vacuum": "air",
        "fwhm": {"mode": "lambda/R", "R": 2000.0},
        "z": "auto",
        "z_range": [-0.01, 0.2],
        "moments": 2,
        "degree": -1,
        "fit_min": lo,
        "fit_max": hi,
        "vmin": -300.0, "vmax": 300.0,
        "sig_min": 10.0, "sig_max": 300.0,
        "gas": True, "tie_balmer": False, "limit_doublets": True,
        "mask_sync": True,
        "noise": "file",
        "regul": 0.0,
        "dlam": 1e-4,
        "dlam_mode": "template",
        "dust": {"mode": "none"},
    }


def sew_fit(fits_path: str, tpl_name: str, cfg_json: str = "{}") -> str:
    """浏览器端的一次拟合，返回 JSON 字符串（含 base64 PNG）。"""
    out = {"ok": False}
    try:
        try:
            user_cfg = json.loads(cfg_json or "{}")
        except Exception:
            user_cfg = {}

        ds = fits_io.open_dataset(fits_path)
        try:
            sp, target = _spec_of(ds)
            cfg = _default_cfg(sp, tpl_name)
            cfg.update(user_cfg)

            tinfo = templates.find_template(cfg.get("template") or tpl_name)
            if tinfo is None:
                raise RuntimeError(f"模板不可用: {cfg.get('template')}")

            logs = []
            res = fitting.run_fit(
                sp["wave"], sp["flux"], sp["var"], sp["mask"],
                cfg, tinfo,
                log=lambda s: logs.append(str(s)))

            out.update(
                ok=True,
                target=target,
                template=tinfo["name"],
                z=res.get("z"),
                velscale=res.get("velscale"),
                dlam=res.get("dlam"),
                chi2=res.get("chi2"),
                dof=res.get("dof"),
                rms=res.get("rms"),
                snr=res.get("snr"),
                fit_seconds=res.get("fit_seconds"),
                components=res.get("components"),
                gas=res.get("gas"),
                stellar_stats=res.get("stellar_stats"),
                plot_png=res.get("plot_png"),
                weight_png=res.get("weight_png"),
                log=res.get("log") or logs,
                log_i18n=res.get("log_i18n"),
            )
        finally:
            try:
                ds.close()
            except Exception:
                pass
    except Exception:
        out = {"ok": False, "error": traceback.format_exc(limit=6)}
    return json.dumps(out, default=str)


def sew_templates() -> str:
    """列出内存文件系统里可用的模板（调试用）。"""
    return json.dumps([dict(name=t["name"], label=t["label"],
                            shape=t["shape"], size=t["size"])
                       for t in templates.list_templates()])
