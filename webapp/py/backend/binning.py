# -*- coding: utf-8 -*-
"""PowerBin 分 bin（Cappellari & Copin 2003），与 cube.ipynb 同口径。

用法::

    info = compute_bins(flux, var, wave, target_sn=50, band=(5300, 5700))
    info["bin_map"]   # (ny,nx) int，-1 = 无效像素
    info["pix_flat"]  # list[np.ndarray]：每个 bin 的 spaxel 扁平索引（y*nx+x）
    info["npix"]      # 每个 bin 的像素数
    info["sn"]        # 每个 bin 的 (ΣF)^2/ΣV 的平方根（≈ 目标 S/N）
"""
from __future__ import annotations

import numpy as np
from scipy import sparse

import powerbin.powerbin as _pb
from powerbin import PowerBin

import warnings

# ---------------------------------------------------------------------------
# 兼容补丁：旧版 powerbin 的 update_bins 用 ``S.count_nonzero(1)``，
# 当前 scipy 的 sparse 不再接受位置参数（cube.ipynb 里有同一份补丁）。
# ---------------------------------------------------------------------------
def _update_bins_patched(capacity_spec, xy, xybin, rbin, args=()):
    bin_num = _pb.power_diagram(xy, xybin, rbin)
    n = len(xy)
    S = sparse.csr_array((np.ones(n), (bin_num, np.arange(n))),
                         shape=(rbin.size, n))
    npix = np.diff(S.indptr)
    xybin = (S @ xy) / npix[:, None]
    if callable(capacity_spec):
        groups = np.split(S.indices, S.indptr[1:-1])
        capacity = np.fromiter((capacity_spec(idx, *args) for idx in groups),
                               float)
    else:
        capacity = S @ capacity_spec
    return xybin, npix, capacity, bin_num


_pb.update_bins = _update_bins_patched


def _capacity_spec(idx, Fb, Vb):
    """bin 的 (ΣF)²/ΣV（PowerBin 的默认容量定义）。"""
    return float(np.sum(Fb[idx]) ** 2 / np.sum(Vb[idx]))


def _pixel_noise(planes):
    """无 variance 时的逐像素噪声：相邻波长差分的 MAD。"""
    d = np.diff(np.asarray(planes, float), axis=0)
    med = np.nanmedian(d, axis=0)
    sig = 1.4826 * np.nanmedian(np.abs(d - med), axis=0) / np.sqrt(2.0)
    return sig


def band_stats(flux, var, wave, band=None, max_planes=80):
    """取波段（默认 5300–5700 Å）内的逐像素平均 flux / var 与 S/N。

    返回 (Fbar, Vbar, snr)，形状均为 (ny, nx)；无法计算时为 NaN。
    """
    wave = np.asarray(wave, float)
    if band:
        lo, hi = float(band[0]), float(band[1])
    else:
        lo, hi = 5300.0, 5700.0
    idx = np.flatnonzero((wave >= lo) & (wave <= hi))
    if idx.size == 0:
        idx = np.flatnonzero((wave >= np.nanmin(wave))
                             & (wave <= np.nanmax(wave)))
    if idx.size > max_planes:
        idx = idx[np.linspace(0, idx.size - 1, max_planes).astype(int)]
    sub = np.asarray(flux[idx], float)
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        Fbar = np.nanmean(sub, axis=0)
        if var is not None:
            vsub = np.asarray(var[idx], float)
            Vbar = np.nanmean(vsub, axis=0)
        else:
            sig = _pixel_noise(sub)
            Vbar = sig ** 2
        snr = Fbar / np.sqrt(Vbar)
    return Fbar, Vbar, snr


def compute_bins(flux, var, wave, target_sn=50.0, band=None,
                 sn_floor=1.0, max_bins=400, maxiter=20):
    """PowerBin 分 bin，返回 dict（键见模块 docstring）。

    - 只对有效像素（flux/var 有限且 var>0、S/N ≥ sn_floor）分 bin；
      其余像素 bin_map = -1。
    - target_sn 无法达成（bin 数过少/全部 bin 判为不合格）时自动降级：
      target_sn 逐次减半，仍失败则把全部有效像素并成一个 bin。
    - max_bins 限制（逐像素级分组时防止数千个选项），超过则增大 target。
    """
    Fbar, Vbar, snr = band_stats(flux, var, wave, band=band)
    good = (np.isfinite(Fbar) & np.isfinite(Vbar) & (Vbar > 0)
            & np.isfinite(snr) & (snr >= sn_floor))
    ny, nx = Fbar.shape
    if not good.any():
        raise ValueError("没有可用于分 bin 的有效像素")

    yy, xx = np.nonzero(good)
    xy = np.column_stack([xx, yy]).astype(float)
    Fg = np.asarray(Fbar[good], float)
    Vg = np.asarray(Vbar[good], float)

    sn = float(max(1.0, target_sn))
    bin_num = None
    capacity = None
    for _try in range(12):
        try:
            pb = PowerBin(xy, _capacity_spec, target_capacity=sn ** 2,
                          pixelsize=1.0, args=(Fg, Vg), verbose=0,
                          maxiter=int(maxiter))
            bn = np.asarray(pb.bin_num, int)
            if bn.size != xy.shape[0] or bn.max() < 0:
                raise ValueError("PowerBin 输出无效")
            nbin = int(bn.max()) + 1
        except Exception:
            # 目标 S/N 太高（全部 bin 不合格）→ 降一档重试
            if sn <= 1.0:
                break
            sn = max(1.0, sn / 2.0)
            continue
        if nbin > int(max_bins):
            # bin 太多（接近逐像素）→ 提高目标 S/N，让 bin 更大
            sn *= 1.6
            continue
        bin_num = bn
        capacity = np.asarray(pb.bin_capacity, float)
        break
    if bin_num is None:
        # 兜底：全部有效像素一个 bin
        bin_num = np.zeros(xy.shape[0], int)
        capacity = np.array([np.sum(Fg) ** 2 / np.sum(Vg)])

    nbin = int(bin_num.max()) + 1
    bin_map = -np.ones((ny, nx), int)
    bin_map[good] = bin_num

    good_flat = np.flatnonzero(good.ravel())
    pix_flat = [good_flat[bin_num == b] for b in range(nbin)]
    npix = np.array([p.size for p in pix_flat], int)
    if capacity is None or capacity.size != nbin:
        capacity = np.array([np.sum(Fg[bin_num == b]) ** 2
                             / np.sum(Vg[bin_num == b]) for b in range(nbin)])
    return dict(
        bin_map=bin_map,
        nbin=nbin,
        npix=npix,
        capacity=capacity,
        sn=np.sqrt(np.maximum(capacity, 0.0)),
        pix_flat=pix_flat,
        target_sn=float(target_sn),
        achieved_sn=sn,
        band=(float(band[0]) if band else 5300.0,
              float(band[1]) if band else 5700.0),
    )
