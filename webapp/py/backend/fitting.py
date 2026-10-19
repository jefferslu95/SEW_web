# -*- coding: utf-8 -*-
"""pPXF / ppxf-sew 拟合封装。

所有 matplotlib 渲染都在后端完成（Agg，串行化），前端只接收 PNG / JSON。
"""
from __future__ import annotations

import base64
import contextlib
import io
import math
import os
import re
import threading
import time
import traceback

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ppxf import ppxf_util
from ppxf.ppxf import ppxf as _Ppxf
from ppxf_sew import ew_util as sewlib
from ppxf_sew import ewfit as _Ewfit
from ppxf_sew.ew_fit import ewfit as _EwfitClass

C_KMS = 299792.458


# ---------------------------------------------------------------------------
# 波长系（air / vacuum）
# ---------------------------------------------------------------------------
def _n_air(lam_air):
    """Ciddor (1996) 空气折射率 - 1（λ 单位 Å）。"""
    lam = np.asarray(lam_air, float)
    sigma2 = (1e4 / np.maximum(lam, 1e-6)) ** 2
    return (5.792105e-2 / (238.0185 - sigma2)
            + 1.67917e-3 / (57.362 - sigma2))


def air_to_vacuum(lam_air):
    """air -> vacuum。λ < 1500 Å 处 Ciddor 公式有极点，保持原值。"""
    lam = np.asarray(lam_air, float)
    conv = np.ones_like(lam)
    ok = lam >= 1500.0
    if np.any(ok):
        conv[ok] = 1.0 + _n_air(lam[ok])
    return lam * conv


def vacuum_to_air(lam_vac):
    """vacuum -> air（反解 n，迭代两次即可到 ~1e-9 Å）。"""
    lam = np.asarray(lam_vac, float)
    out = lam.copy()
    ok = lam >= 1500.0
    if np.any(ok):
        lv = lam[ok]
        la = lv.copy()
        for _ in range(3):
            la = lv / (1.0 + _n_air(la))
        out[ok] = la
    return out


def align_wave_system(wave, obs_vacuum: bool, tpl_vacuum=None):
    """把观测谱波长转换到【拟合用的 vacuum 网格】。

    ppxf_sew 内部总是把模板转到 vacuum（``temp(vacuum='air')`` 会做
    air→vacuum），发射线表也是 vacuum，所以观测谱只要是 air 就必须转
    vacuum，否则会带来 ~80-90 km/s 的系统速度偏移。

    返回 (wave, 说明或 None)。
    """
    if obs_vacuum:
        return np.asarray(wave, float), None
    return (air_to_vacuum(wave),
            "观测谱 λ: air → vacuum（拟合在 vacuum 网格）")

# matplotlib 非线程安全：所有渲染串行
_MPL_LOCK = threading.RLock()

# 发射线（rest，真空 Å），用于红移互相关
_EM_LINES = np.array([
    3727.06, 3729.06,            # [O II]
    3869.80, 3968.62,            # [Ne III]
    4102.86, 4341.72, 4862.66, 6564.61,   # Hdelta..Halpha
    4959.28, 5008.20,            # [O III]
    6302.04, 6365.56,            # [O I]
    6549.91, 6585.32,            # [N II]
    6718.25, 6732.66,            # [S II]
    9071.09, 9534.41,            # [S III]
], float)
_EM_AMP = np.array([1.2, 1.5, 0.6, 0.2, 0.5, 0.7, 1.0, 3.0,
                    1.0, 3.0, 0.4, 0.1, 0.5, 1.5, 0.6, 0.5, 0.5, 0.8])


# ---------------------------------------------------------------------------
# FWHM 自动识别
# ---------------------------------------------------------------------------
def _instrument_default(instr: str) -> tuple[float, str]:
    u = (instr or "").upper()
    if "WIFES" in u or "WIFE" in u:
        return 7000.0, "WiFeS 默认 R=7000 (λ/R)"
    if "MUSE" in u:
        return 3000.0, "MUSE 默认 R=3000 (λ/R)"
    if "SDSS" in u or "BOSS" in u:
        return 2000.0, "SDSS/BOSS 默认 R=2000 (λ/R)"
    if "MANGA" in u:
        return 2000.0, "MaNGA 默认 R=2000 (λ/R)"
    if "XSHOOTER" in u:
        return 5000.0, "XSHOOTER 默认 R=5000 (λ/R)"
    if "LAMOST" in u:
        return 1800.0, "LAMOST 默认 R=1800 (λ/R)"
    return 2000.0, "未知仪器默认 R=2000 (λ/R)"


def detect_fwhm(dataset) -> dict:
    """从 FITS 头卡/仪器名推断仪器线扩展函数。"""
    hdr = dataset.hdul[dataset.flux_hdu].header

    def g(key):
        """在 flux HDU、HDU0、其余扩展里依次查找。"""
        order = [hdr, dataset.hdul[0].header]
        order += [h.header for h in dataset.hdul]
        for h in order:
            if key in h:
                return h[key]
        return None

    # 文件来源兜底（MaNGA spaxel 的 INSTRUME 常为空，但有 SRCFILE/PLATEIFU）
    _src = (" ".join(str(g(k) or "") for k in
                     ("SRCFILE", "PLATEIFU", "FILENAME", "OBJECT"))
            + " " + str(dataset.path)).lower()
    _instr = str(dataset.instrument or "")
    if "lamost" in _src and not _instr:
        _instr = "LAMOST"
    if "manga" in _src or "manga" in _instr.lower() or g("PLATEIFU") is not None:
        _instr = "MaNGA"

    # 1) 明确给出 FWHM（Å）
    for key in ("FWHM_GAL", "FWHM_INST", "FWHM", "SPECFWHM"):
        v = g(key)
        if v is None:
            continue
        try:
            v = float(v)
        except Exception:
            continue
        unit = str(g("FWHMUNIT") or g("CUNIT1") or "").lower()
        scale = 1.0
        if "nm" in unit:
            scale = 10.0
        if v > 0:
            return dict(mode="constant", value=v * scale,
                        source=f"头卡 {key}={v:g} Å", confidence="high")

    # 2) 分辨率 R（无单位）或 Δλ
    for key in ("R", "RRESOL", "RESOLUT", "RESOLUTION", "SPECRES",
                "SPEC_RES", "SPTRES", "RES"):
        v = g(key)
        if v is None:
            continue
        try:
            v = float(v)
        except Exception:
            continue
        if v > 100:
            return dict(mode="lambda/R", R=v,
                        source=f"头卡 {key}={v:g} (λ/R)", confidence="high")
        if 0 < v < 100:
            # 可能是 Δλ（Å/px）；FWHM ≈ 2.35 Δλ
            return dict(mode="constant", value=v * 2.355,
                        source=f"头卡 {key}={v:g} (按 Å/px 处理)",
                        confidence="low")

    # 3) 仪器名默认
    R, src = _instrument_default(_instr)
    return dict(mode="lambda/R", R=R, source=src,
                confidence=("medium" if _instr else "low"))


_LSF_CACHE = {}


def load_lsf_scatter(path: str, hdu_name: str = "LSF") -> dict:
    """从参考 FITS 的 LSF HDU 读取散点 (λ, FWHM)。

    该 HDU 的最后一维 = 3（FWHM, λ, 权重/质量），倒数第二维 = 散点节点数，
    其余为空间维（如 CSST IFS: (y, x, 27, 3)）。对每个节点取所有有效
    spaxel 的中位数，得到一条 LSF(λ) 散点曲线。
    若第二列不是波长（如全 0），退而使用 HDU0 头卡的波长 WCS 生成节点波长
    （CRVAL3/CDELT3/CRPIX3/PC3_3）。
    """
    if not path:
        raise ValueError("散点 LSF 模式需要提供参考 FITS 文件路径")
    path = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(path):
        raise ValueError(f"LSF 参考文件不存在: {path}")
    key = (path, os.path.getmtime(path), str(hdu_name or "LSF"))
    if key in _LSF_CACHE:
        return _LSF_CACHE[key]
    from astropy.io import fits
    hdul = fits.open(path, memmap=True)
    try:
        target = str(hdu_name or "LSF").upper()
        idx = None
        for i, h in enumerate(hdul):
            if (h.name or "").upper() == target and h.data is not None:
                idx = i
                break
        if idx is None:
            for i, h in enumerate(hdul):
                d = h.data
                if d is not None and getattr(d, "ndim", 0) >= 2 \
                        and d.shape[-1] == 3:
                    idx = i
                    break
        if idx is None:
            raise ValueError(f"参考文件中找不到 {hdu_name} HDU")
        arr = np.asarray(hdul[idx].data, dtype=float)
        if arr.ndim < 2:
            raise ValueError("LSF HDU 维数不足")
        if arr.shape[-1] != 3:
            if arr.shape[0] == 3:
                arr = np.moveaxis(arr, 0, -1)
            else:
                raise ValueError("LSF HDU 最后一维应为 3（FWHM, λ, 权重）")
        npts = int(arr.shape[-2])
        flat = arr.reshape(-1, npts, 3)
        fwhm = flat[:, :, 0]
        wave = flat[:, :, 1]
        qual = flat[:, :, 2]
        valid = (np.isfinite(fwhm) & np.isfinite(wave) & (fwhm > 0)
                 & (wave > 0) & np.isfinite(qual))
        W = np.full(npts, np.nan)
        F = np.full(npts, np.nan)
        for j in range(npts):
            m = valid[:, j]
            if m.any():
                W[j] = float(np.median(wave[m, j]))
                F[j] = float(np.median(fwhm[m, j]))
        medw = (float(np.nanmedian(W)) if np.isfinite(W).any() else 0.0)
        if not (100.0 < medw < 1e5):
            # 第二列不是波长 -> 用 HDU0 的波长 WCS
            hdr = hdul[0].header
            crval = hdr.get("CRVAL3")
            cdelt = hdr.get("CDELT3")
            if crval is not None and cdelt is not None:
                crpix = float(hdr.get("CRPIX3", 1.0))
                pc = float(hdr.get("PC3_3", 1.0))
                W = (float(crval) + (np.arange(npts) - (crpix - 1.0))
                     * float(cdelt) * pc)
        good = np.isfinite(W) & np.isfinite(F) & (W > 0) & (F > 0)
        W, F = W[good], F[good]
        order = np.argsort(W)
        W, F = W[order], F[order]
        uq = np.r_[True, np.diff(W) > 0]
        W, F = W[uq], F[uq]
        if W.size < 2:
            raise ValueError("LSF 散点不足（<2 个有效点）")
        res = dict(wave=W, fwhm=F, path=path,
                   hdu=(hdul[idx].name or f"HDU{idx}"),
                   n_points=int(W.size))
        _LSF_CACHE[key] = res
        return res
    finally:
        hdul.close()


def make_fwhm(spec: dict):
    """把界面/自动识别的 FWHM 配置变成可调用函数 fwhm(λ)=Å。"""
    mode = str((spec or {}).get("mode", "lambda/R"))
    if mode == "constant":
        v = float(spec.get("value", 2.5) or 2.5)
        return lambda lam: np.full_like(np.asarray(lam, float), v)
    if mode == "lambda/R":
        R = float(spec.get("R", 2000.0) or 2000.0)
        if R <= 0:
            R = 2000.0
        return lambda lam: np.asarray(lam, float) / R
    if mode == "function":
        expr = str(spec.get("expr") or "lam/7000").strip().replace("^", "**")
        if not re.fullmatch(r"[0-9eE+\-*/().,\sA-Za-z_]+", expr):
            raise ValueError(f"FWHM 函数含非法字符: {expr}")
        allowed = {
            "lam", "wave", "np", "numpy", "math", "e",
            "exp", "log", "log10", "sqrt", "sin", "cos", "tan",
            "pi", "abs", "absolute", "power", "arctan", "arcsin",
            "arccos", "minimum", "maximum", "clip", "where",
            "ones_like", "zeros_like", "full_like", "float", "int",
        }
        names = set(re.findall(r"[A-Za-z_]\w*", expr))
        bad = names - allowed
        if bad:
            raise ValueError(f"FWHM 函数不允许的变量/函数: {sorted(bad)}")
        env = {
            "np": np, "numpy": np, "math": math,
            "exp": np.exp, "log": np.log, "log10": np.log10,
            "sqrt": np.sqrt, "sin": np.sin, "cos": np.cos,
            "tan": np.tan, "pi": np.pi, "abs": np.abs,
            "absolute": np.abs, "power": np.power,
            "arctan": np.arctan, "arcsin": np.arcsin,
            "arccos": np.arccos, "minimum": np.minimum,
            "maximum": np.maximum, "clip": np.clip,
            "where": np.where, "ones_like": np.ones_like,
            "zeros_like": np.zeros_like, "full_like": np.full_like,
            "float": float, "int": int,
        }

        def fn(lam):
            lam = np.asarray(lam, float)
            env2 = dict(env)
            env2["lam"] = lam
            env2["wave"] = lam
            try:
                out = eval(expr, {"__builtins__": {}}, env2)
            except Exception as e:
                raise ValueError(f"FWHM 函数求值失败: {e}")
            out = np.asarray(out, float)
            if out.ndim == 0:
                out = np.full_like(lam, float(out))
            out = np.broadcast_to(out, lam.shape).astype(float)
            if not np.all(np.isfinite(out)) or np.any(out <= 0):
                raise ValueError("FWHM 函数必须对全部波长返回有限正值")
            return out
        fn.desc = f"函数 FWHM: {expr}"
        return fn
    if mode == "scatter":
        info = load_lsf_scatter(spec.get("path"), spec.get("hdu") or "LSF")
        W, F = info["wave"], info["fwhm"]
        lw = np.log(W)

        def fn(lam):
            lam = np.asarray(lam, float)
            return np.interp(np.log(np.maximum(lam, 1e-6)), lw, F,
                             left=F[0], right=F[-1])
        fn.desc = (f"散点 LSF: {os.path.basename(info['path'])} "
                   f"[{info['hdu']}] {info['n_points']} 点, "
                   f"{W[0]:.0f}-{W[-1]:.0f} Å")
        fn.info = info
        return fn
    if mode == "auto" or mode is None:
        return lambda lam: np.asarray(lam, float) / 2000.0
    raise ValueError(f"未知 FWHM 模式: {mode}")


# ---------------------------------------------------------------------------
# 尘埃消光曲线
# ---------------------------------------------------------------------------
DUST_MODES = ("calzetti", "powerlaw", "kc13", "custom")


def _vec(lam, v):
    """把标量/数组统一成与 lam 同形状的浮点数组。"""
    out = np.asarray(v, float)
    if out.ndim == 0:
        out = np.full_like(np.asarray(lam, float), float(out))
    return np.broadcast_to(out, np.shape(lam)).astype(float)


def make_dust_curve(spec: dict):
    """把尘埃配置变成 (func(lam,*pars)->乘性衰减因子, 参数名列表)。

    所有曲线返回的都是【乘到模板上的因子】= 10^(-0.4 A_λ)，λ 单位为 Å。
    """
    from ppxf.ppxf import attenuation as _att
    mode = str((spec or {}).get("mode") or "calzetti").lower()
    if mode == "calzetti":
        # Calzetti et al. (2000)，1 参数 A_V
        def fn(lam, av):
            return _att(np.asarray(lam, float), _vec(lam, av))
        return fn, ["A_V"], "Calzetti (C+00)"
    if mode == "powerlaw":
        # A_λ = A_V (λ/5500)^β，2 参数 (A_V, β)
        def fn(lam, av, beta):
            lam = np.asarray(lam, float)
            a = _vec(lam, av) * (lam / 5500.0) ** _vec(lam, beta)
            return 10.0 ** (-0.4 * a)
        return fn, ["A_V", "β"], "幂律 A_V(λ/5500)^β"
    if mode == "kc13":
        # Cappellari (2023) / KC13：（A_V, δ），UV bump 由 δ 推出
        def fn(lam, av, delta):
            return _att(np.asarray(lam, float), _vec(lam, av),
                        _vec(lam, delta))
        return fn, ["A_V", "δ"], "KC13 / Cappellari(2023) (A_V, δ)"
    if mode == "custom":
        expr = str((spec or {}).get("expr") or "av*(lam/5500.)**-0.7").strip()
        expr = expr.replace("^", "**")
        if not re.fullmatch(r"[0-9eE+\-*/().,\sA-Za-z_]+", expr):
            raise ValueError(f"消光曲线表达式含非法字符: {expr}")
        allowed = {
            "lam", "wave", "av", "a_v", "beta", "np", "numpy", "math", "e",
            "exp", "log", "log10", "sqrt", "power", "abs", "absolute",
            "clip", "where", "minimum", "maximum", "pi", "float",
        }
        names = set(re.findall(r"[A-Za-z_]\w*", expr))
        bad = names - allowed
        if bad:
            raise ValueError(f"消光曲线表达式不允许的变量/函数: {sorted(bad)}")
        env = {
            "np": np, "numpy": np, "math": math, "e": np.e, "pi": np.pi,
            "exp": np.exp, "log": np.log, "log10": np.log10, "sqrt": np.sqrt,
            "power": np.power, "abs": np.abs, "absolute": np.abs,
            "clip": np.clip, "where": np.where,
            "minimum": np.minimum, "maximum": np.maximum, "float": float,
        }

        def fn(lam, av, beta):
            lam = np.asarray(lam, float)
            env2 = dict(env)
            env2.update(lam=lam, wave=lam, av=_vec(lam, av),
                        a_v=_vec(lam, av), beta=_vec(lam, beta))
            try:
                a_lam = eval(expr, {"__builtins__": {}}, env2)
            except Exception as e:
                raise ValueError(f"消光曲线求值失败: {e}")
            a_lam = _vec(lam, a_lam)
            if not np.all(np.isfinite(a_lam)):
                raise ValueError("消光曲线对部分波长返回了非有限值")
            return 10.0 ** (-0.4 * a_lam)
        return fn, ["A_V", "β"], f"自定义 A_λ: {expr}"
    raise ValueError(f"未知消光曲线: {mode}")


def _dust_ranges(spec: dict):
    """返回 (A_V 范围, 第二参数范围, 第二参数说明)。"""
    spec = spec or {}
    av = spec.get("av_range") or [0.0, 4.0]
    av = [float(av[0]), float(av[1])]
    if av[1] < av[0]:
        av = [av[1], av[0]]
    mode = str(spec.get("mode") or "calzetti").lower()
    dflt = {"powerlaw": [-3.0, 1.0], "kc13": [-1.0, 0.4],
            "custom": [-3.0, 1.0]}.get(mode, [0.0, 0.0])
    sl = spec.get("slope_range") or dflt
    sl = [float(sl[0]), float(sl[1])]
    if sl[1] < sl[0]:
        sl = [sl[1], sl[0]]
    return av, sl


def template_mass_light(path, age_range=(1e-3, 13.7), norm_range=(5070.0, 5950.0)):
    """模板库的逐 (age, metal) 质量与归一化光（供质量加权平均）。

    与 ``ppxf_sew.ew_util.temp`` 的口径对齐：
      · 年龄取值 ``log10(age[Gyr]) + 9``（同 temp: tpl_age = log10(age)+9）；
      · 年龄筛选 ``log10(lo)+9 <= logAge <= log10(hi)+9``；
      · 归一化光 = 该 SSP 在 norm_range 内的平均流量（temp 里 ``flux``）——
        展宽 + log-rebin 都近似保通量，直接用原生网格求均值即可。
    返回 ``(mass, light)``，形状与 ``temp.templates.shape[1:]`` 一致；
    模板没有 masses 或不是 3D 时返回 ``(None, None)``。
    """
    try:
        with np.load(str(path), allow_pickle=False) as z:
            if "masses" not in z:
                return None, None
            lam = np.asarray(z["lam"], float)
            ages = np.asarray(z["ages"], float)
            metals = np.asarray(z["metals"], float)
            tpl = np.asarray(z["templates"], float)
            masses = np.asarray(z["masses"], float)
    except Exception:
        return None, None
    if tpl.ndim != 3 or masses.shape != (ages.size, metals.size):
        return None, None
    log_age = np.log10(np.maximum(ages, 1e-6)) + 9.0
    lo, hi = float(age_range[0]), float(age_range[1])
    keep = (np.log10(max(lo, 1e-6)) + 9.0 <= log_age) & \
        (log_age <= np.log10(max(hi, 1e-6)) + 9.0)
    if not keep.any():
        return None, None
    tpl = tpl[:, keep, :]
    masses = masses[keep, :]
    # 归一化光：原生 λ 上取 norm_range 内平均流量
    b = (lam >= norm_range[0]) & (lam <= norm_range[1])
    if b.sum() < 3:
        b = np.ones_like(lam, bool)
    light = np.nanmean(tpl[b], axis=0)
    light = np.where(np.isfinite(light) & (light > 0), light, np.nan)
    return masses, light


def _weighted_stats(w, xgrid, ygrid, extra=None):
    """加权平均 (logAge, [M/H])；extra 给定则为质量权重（w*extra）。"""
    ww = np.asarray(w, float)
    if extra is not None:
        ww = ww * np.asarray(extra, float)
    ww = np.where(np.isfinite(ww) & (ww > 0), ww, 0.0)
    s = float(ww.sum())
    if not (s > 0):
        return None
    return dict(
        log_age=float((ww * xgrid).sum() / s),
        metal=float((ww * ygrid).sum() / s),
        wsum=s)


def describe_fwhm(spec: dict) -> str:
    mode = (spec or {}).get("mode")
    if mode == "constant":
        return f"常量 {float(spec.get('value', 0)):.3g} Å"
    if mode == "lambda/R":
        return f"λ/R, R={float(spec.get('R', 0)):.6g}"
    if mode == "function":
        return f"函数 FWHM: {spec.get('expr', '')}"
    if mode == "scatter":
        return f"散点 LSF: {os.path.basename(str(spec.get('path', '')))}"
    return str(mode or "auto")


# ---------------------------------------------------------------------------
# 红移自动估计（模板互相关）
# ---------------------------------------------------------------------------
def _load_template_1d(template_path: str):
    z = np.load(template_path, allow_pickle=False)
    lam = np.asarray(z["lam"], float)
    tpl = np.asarray(z["templates"], float)
    ages = np.asarray(z["ages"], float)
    metals = np.asarray(z["metals"], float)
    if tpl.ndim != 3:
        raise ValueError("模板 arrays 不是 3D (nwave,nage,nmetal)")
    # 选 logAge≈10（10 Gyr）且 [M/H]≈0 的模板
    try:
        ia = int(np.argmin(np.abs(np.log10(np.maximum(ages, 1e-3)) - 1.0)))
    except Exception:
        ia = tpl.shape[1] // 2
    try:
        im = int(np.argmin(np.abs(metals)))
    except Exception:
        im = tpl.shape[2] // 2
    s = np.asarray(tpl[:, ia, im], float)
    s = np.where(np.isfinite(s), s, np.nanmedian(s[np.isfinite(s)]))
    return lam, s


def _highpass(y, n=51):
    y = np.asarray(y, float)
    if y.size < n:
        n = max(5, y.size // 4 * 2 + 1)
    from scipy.ndimage import uniform_filter1d
    sm = uniform_filter1d(y, size=int(n), mode="nearest")
    r = y - sm
    s = np.nanstd(r)
    return r / s if s > 0 else r


def _parabolic(y, j):
    if 0 < j < len(y) - 1:
        den = y[j - 1] - 2 * y[j] + y[j + 1]
        if abs(den) > 1e-20:
            return 0.5 * (y[j - 1] - y[j + 1]) / den
    return 0.0


def estimate_redshift(wave, flux, template_path=None,
                      z_range=(-0.01, 0.2), velscale=None,
                      obs_vacuum="air", template_vacuum="air",
                      progress=None):
    """用恒星模板 + 发射线模板互相关估计系统红移。

    返回 dict(z, method, snr, z_star, snr_star, z_em, snr_em, warning)
    """
    from scipy.signal import correlate
    wave = np.asarray(wave, float)
    _obs_vac = str(obs_vacuum or "air").lower().startswith("vac")
    _tpl_vac = str(template_vacuum or "air").lower().startswith("vac")
    # 拟合/互相关统一到 vacuum 网格
    wave, _ = align_wave_system(wave, _obs_vac)
    flux = np.asarray(flux, float)
    good = np.isfinite(flux) & np.isfinite(wave) & (wave > 0)
    if good.sum() < 100:
        return dict(z=0.0, method="none", snr=0.0,
                    warning="有效像素太少，红移回退为 0",
                    warning_code="fewpix", warning_arg="")
    w = wave[good]
    f = flux[good]
    # 单调 & 均匀性检查：log_rebin 需要递增波长
    if np.any(np.diff(w) <= 0):
        idx = np.argsort(w)
        w, f = w[idx], f[idx]
    if velscale is None:
        velscale = C_KMS * math.log(10.0) * 1e-4
    try:
        fout, ln_obs = ppxf_util.log_rebin(w, f, velscale=velscale)[:2]
    except Exception as e:
        return dict(z=0.0, method="none", snr=0.0,
                    warning=f"log_rebin 失败: {e}",
                    warning_code="logrebin", warning_arg=str(e))
    npix = fout.size
    dln = float(ln_obs[-1] - ln_obs[0]) / max(npix - 1, 1)
    zlo, zhi = float(z_range[0]), float(z_range[1])
    lag_lo = int(math.floor(math.log1p(zlo) / dln))
    lag_hi = int(math.ceil(math.log1p(zhi) / dln))

    def _cc(tpl_rest_ln, tpl_rest):
        # 把 rest 模板插到观测 ln 网格上（只差一个整体平移）
        t = np.interp(ln_obs, tpl_rest_ln, tpl_rest, left=0.0, right=0.0)
        if np.allclose(t, 0):
            return None
        a = _highpass(fout, n=max(15, int(80 * 1e-4 / dln) | 1))
        b = _highpass(t, n=max(15, int(80 * 1e-4 / dln) | 1))
        cc = correlate(a, b, mode="full")
        lags = np.arange(-npix + 1, npix)
        sel = (lags >= lag_lo) & (lags <= lag_hi)
        if not sel.any():
            return None
        cc = cc[sel]
        lags = lags[sel]
        j = int(np.argmax(cc))
        med = np.median(cc)
        mad = 1.4826 * np.median(np.abs(cc - med)) + 1e-30
        snr = float((cc[j] - med) / mad)
        dl = _parabolic(cc, j)
        z = float(math.expm1((lags[j] + dl) * dln))
        return z, snr, cc, lags

    # ---- 恒星模板 ----
    z_star, snr_star = None, 0.0
    if template_path:
        try:
            lam_t, s_t = _load_template_1d(template_path)
            if not _tpl_vac:      # 模板文件是 air -> 转到 vacuum
                lam_t = air_to_vacuum(lam_t)
            # 仅用与观测重叠的 rest 范围
            lo = w[0] / (1.0 + zhi)
            hi = w[-1] / (1.0 + zlo)
            m = (lam_t >= lo) & (lam_t <= hi)
            if m.sum() > 50:
                tr, ln_t = ppxf_util.log_rebin(lam_t[m], s_t[m],
                                               velscale=velscale)[:2]
                got = _cc(ln_t, tr)
                if got:
                    z_star, snr_star = got[0], got[1]
        except Exception as e:
            z_star, snr_star = None, 0.0
    # ---- 发射线模板（rest 网格）----
    z_em, snr_em = None, 0.0
    try:
        # 构造 rest 线梳：在 kms 网格上，线宽 ~2 px
        n_rest = int(np.log(1.5) / dln) + npix  # 足够覆盖
        ln_r = np.linspace(ln_obs[0] - abs(math.log1p(zhi)) - 0.05,
                           ln_obs[-1] + 0.05, n_rest)
        comb = np.zeros(n_rest)
        sig = 1.5 * dln
        for l0, amp in zip(_EM_LINES, _EM_AMP):
            comb += amp * np.exp(-0.5 * ((ln_r - math.log(l0)) / sig) ** 2)
        got = _cc(ln_r, comb)
        if got:
            z_em, snr_em = got[0], got[1]
    except Exception:
        pass

    if z_star is None and z_em is None:
        return dict(z=0.0, method="none", snr=0.0,
                    warning="模板互相关失败，红移回退为 0")
    # 选择：SNR 高者；若恒星稍低但发射线很弱则仍用恒星
    if z_em is not None and (z_star is None or snr_em > snr_star + 1.0):
        z, method, snr = z_em, "emission-lines", snr_em
    else:
        z, method, snr = z_star, "stellar-template", snr_star
    return dict(z=float(z), method=method, snr=float(snr),
                z_star=(None if z_star is None else float(z_star)),
                snr_star=float(snr_star),
                z_em=(None if z_em is None else float(z_em)),
                snr_em=float(snr_em))


# ---------------------------------------------------------------------------
# 噪声估计
# ---------------------------------------------------------------------------
def estimate_noise_variance(flux, mask=None):
    f = np.asarray(flux, float)
    m = (np.isfinite(f) if mask is None else (np.isfinite(f) & ~mask))
    if m.sum() < 10:
        return np.full_like(f, np.nan)
    d = np.diff(f[m])
    sig = 1.4826 * np.median(np.abs(d - np.median(d))) / math.sqrt(2.0)
    if not np.isfinite(sig) or sig <= 0:
        sig = np.nanstd(f[m]) * 1e-3
    return np.full_like(f, max(sig, 1e-30) ** 2)


# ---------------------------------------------------------------------------
# PNG 渲染
# ---------------------------------------------------------------------------
def _fig_to_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(
        buf.getvalue()).decode("ascii")


def render_fit_plot(p, figsize=(13, 7), scale=1.0):
    """调用 ppxf / ppxf_sew 自带的 plot()。

    ppxf_sew 内部会把 ``galaxy``/``noise`` 除以 ``mean_gal``（归一化到 ~1），
    所以直接 ``p.plot()`` 画出来的纵轴是归一化后的相对值（例如 1），
    与输入的观测流量（例如 1e-17）不同量纲。这里在绘图前临时把相关
    数组乘回 ``mean_gal``，画完再恢复，使图与观测谱同一单位。
    """
    saved = {}
    if scale and scale != 1.0:
        for key in ("galaxy", "bestfit", "gas_bestfit",
                    "gas_bestfit_templates", "noise", "noise_full",
                    "bestfit_full", "galaxy_full", "residual"):
            v = getattr(p, key, None)
            if isinstance(v, np.ndarray):
                saved[key] = v
                setattr(p, key, v * scale)
    with _MPL_LOCK:
        fig = plt.figure(figsize=figsize)
        fig.add_subplot(111)
        try:
            try:
                p.plot(gas_clip=False)
            except TypeError:
                p.plot()
            fig.tight_layout()
            return _fig_to_b64(fig)
        finally:
            for key, v in saved.items():
                setattr(p, key, v)


def render_weight_plot(temp, weights_grid, figsize=(6.2, 5.2)):
    """调用 ppxf_sew 模板的 plot()（age–metal 权重）。"""
    if weights_grid is None:
        return None
    with _MPL_LOCK:
        fig = plt.figure(figsize=figsize)
        fig.add_subplot(111)
        try:
            temp.plot(np.asarray(weights_grid, float))
        except Exception:
            plt.close(fig)
            return None
        fig.tight_layout()
        return _fig_to_b64(fig)


# ---------------------------------------------------------------------------
# 拟合主流程
# ---------------------------------------------------------------------------
_TPL_DLAM_CACHE = {}


def template_dlam(path, lam_lo=None, lam_hi=None):
    """模板波长网格的典型 Δlnλ（dex/px，取中位数）。

    给定 lam_lo/lam_hi（静止系，Å）时只统计这个区间（线性 λ 网格下
    Δλ/λ 随 λ 变化，用区间中位比全范围中位更有代表性）。
    """
    if not path:
        return 1e-4
    key = (str(path), None if lam_lo is None else round(float(lam_lo), 1),
           None if lam_hi is None else round(float(lam_hi), 1))
    if key in _TPL_DLAM_CACHE:
        return _TPL_DLAM_CACHE[key]
    d = 1e-4
    try:
        with np.load(str(path), allow_pickle=False) as z:
            lam = np.asarray(z["lam"], float)
        lam = lam[np.isfinite(lam) & (lam > 0)]
        if lam.size > 2:
            m = np.ones(lam.size - 1, bool)
            if lam_lo is not None and lam_hi is not None:
                m = (lam[:-1] >= lam_lo) & (lam[:-1] <= lam_hi)
            if m.any():
                # Δλ/λ = Δlnλ → Δlog10λ（dex）
                dd = float(np.median(np.diff(lam)[m] / lam[:-1][m])
                           / math.log(10.0))
                if np.isfinite(dd) and dd > 0:
                    d = dd
    except Exception:
        pass
    _TPL_DLAM_CACHE[key] = float(d)
    return float(d)


def run_fit(wave, flux, var, badmask, cfg, template_info,
            progress=None, log=None):
    """执行一次拟合。

    wave/flux/var: 观测系一维数组（var 可为 None）
    badmask: bool，True=坏点
    cfg: 见 README / app.py
    template_info: templates.find_template() 的返回（含 path）
    返回 JSON 友好 dict。
    """
    t0 = time.time()
    msgs = []
    log_i18n = []          # 与 msgs 一一对应：[key, args] 或 None

    def _log(s, key=None, **args):
        msgs.append(str(s))
        log_i18n.append([key, args] if key else None)
        if log:
            log(str(s))

    def _num(key, default):
        """cfg 数值读取：None/NaN/空串 → default。"""
        v = cfg.get(key, default)
        if v is None or v == "":
            return float(default)
        try:
            v = float(v)
        except (TypeError, ValueError):
            return float(default)
        return float(default) if not np.isfinite(v) else v

    wave = np.asarray(wave, float)
    flux = np.asarray(flux, float)
    bad = np.asarray(badmask, bool) | ~np.isfinite(flux)
    if var is None:
        if str(cfg.get("noise", "auto")) == "file":
            _log("未提供误差 HDU，自动改用残差噪声估计", key="noerr")
        var = estimate_noise_variance(flux, bad)
    else:
        var = np.asarray(var, float)
        bad = bad | ~np.isfinite(var) | (var <= 0)

    # --- 拟合波长范围（观测系） ---
    wmin = _num("fit_min", np.nanmin(wave))
    wmax = _num("fit_max", np.nanmax(wave))
    sel = (wave >= wmin) & (wave <= wmax)
    if sel.sum() < 50:
        raise ValueError("拟合波段内有效像素 < 50")
    w = wave[sel]
    f = flux[sel]
    v = var[sel]
    b = bad[sel]
    order = np.argsort(w)
    w, f, v, b = w[order], f[order], v[order], b[order]

    # 去掉重复波长
    uq = np.r_[True, np.diff(w) > 0]
    w, f, v, b = w[uq], f[uq], v[uq], b[uq]
    good = (~b) & np.isfinite(f) & np.isfinite(v) & (v > 0)
    if good.sum() < 50:
        raise ValueError("拟合波段内有效像素 < 50（掩膜过多）")

    # --- 波长系对齐：拟合统一在 vacuum 网格 ---
    _obs_vac = str(cfg.get("obs_vacuum", "air") or "air").lower() \
        .startswith("vac")
    w, _ws = align_wave_system(w, _obs_vac)
    _wave_system = "观测 " + ("vacuum" if _obs_vac else "air") + " → 拟合 vacuum"
    if _ws:
        _log(_ws, key="wavesys")

    # --- 速度网格 dlam（dex/px）→ velscale = c·ln10·dlam ---
    #   template：跟模板自身的波长采样一致（默认，不无谓过采样）
    #   obs     ：跟观测谱的采样一致
    #   custom  ：用户给定
    dlam_mode = str(cfg.get("dlam_mode") or "template").lower()
    dlam_obs = (float(np.median(np.diff(w) / w[:-1]) / math.log(10.0))
                if w.size > 2 else 1e-4)
    # 观测谱是否已经是均匀 log 网格（Δlnλ 恒定）？
    #   是：静止波长 = 观测波长/(1+z) 且 Δlnλ 不变 → 根本不需要重采样，
    #       只把模板重采样到这个速度网格（静止系）上即可。
    _dln_w = np.diff(np.log(w)) if w.size > 2 else np.array([])
    log_grid = bool(_dln_w.size > 2 and np.all(_dln_w > 0)
                    and (_dln_w.max() - _dln_w.min())
                    <= 1e-6 * float(np.median(_dln_w)))
    dlam_log_exact = (float(np.median(_dln_w)) / math.log(10.0)
                      if log_grid else None)

    def _resolve_dlam(z_guess=0.0):
        """返回 (dlam, 来源说明)。模板模式按静止系拟合区间统计。"""
        if dlam_mode.startswith("obs"):
            return dlam_obs, f"观测谱采样 {dlam_obs:.3e} dex/px"
        if dlam_mode.startswith("custom") or dlam_mode.startswith("manual"):
            dc = _num("dlam", 1e-4)
            return dc, f"自定义 {dc:.3e} dex/px"
        if template_info:
            lo = float(np.nanmin(w)) / (1.0 + z_guess)
            hi = float(np.nanmax(w)) / (1.0 + z_guess)
            dt = template_dlam(template_info["path"], lo, hi)
            return dt, f"模板采样 {dt:.3e} dex/px"
        return 1e-4, "默认 1.000e-04 dex/px"

    dlam_pre, dlam_src_pre = _resolve_dlam(0.0)
    velscale_pre = C_KMS * math.log(10.0) * dlam_pre

    # --- 红移 ---
    if progress:
        progress(0.05, "估计红移", key="redshift")
    zcfg = cfg.get("z", "auto")
    zinfo = None
    if isinstance(zcfg, str) and zcfg.lower() == "auto":
        _zr = cfg.get("z_range") or [-0.01, 0.2]
        zinfo = estimate_redshift(
            w, f, template_path=template_info["path"] if template_info else None,
            z_range=tuple(_zr), velscale=velscale_pre)
        z = float(zinfo["z"])
        _log(f"自动红移 z = {z:.6f} ({zinfo['method']}, "
             f"SNR={zinfo['snr']:.1f})", key="zauto", z=f"{z:.6f}",
             method=str(zinfo["method"]), snr=f"{zinfo['snr']:.1f}")
        if zinfo.get("warning"):
            _log(zinfo["warning"], key=zinfo.get("warning_code"),
                 err=zinfo.get("warning_arg", ""))
    else:
        z = float(zcfg or 0.0)

    # 用确定的 z 把静止系区间定下来，再定最终网格
    dlam, dlam_src = _resolve_dlam(z)
    # 若所选步长就是观测谱自己的 log 网格步长 → 原样使用，不重采样
    identity = bool(
        log_grid and dlam_log_exact
        and abs(dlam - dlam_log_exact)
        <= 1e-4 * abs(dlam_log_exact))
    if identity:
        dlam = float(dlam_log_exact)
        dlam_src += "（观测谱即均匀 log 网格，原样使用）"
    dlam = float(min(max(dlam, 2e-6), 2e-3))
    velscale = C_KMS * math.log(10.0) * dlam
    _log(f"速度网格：{dlam_src} → velscale ≈ {velscale:.1f} km/s",
         key="velscale", src_kind=dlam_mode, src=dlam_src,
         v=f"{velscale:.1f}")

    # --- log-rebin 到均匀速度网格 ---
    if progress:
        progress(0.1, "log-rebin", key="logrebin")
    # 用中值噪声替代坏点方差（避免巨大的方差把 log_rebin 结果污染）
    vmed = float(np.nanmedian(v[good]))
    vv = np.where(good, v, vmed * 1e6)
    ff = np.asarray(f, float).copy()
    if not good.all():
        idx = np.flatnonzero(good)
        ff[~good] = np.interp(w[~good], w[idx], ff[idx])
    if identity:
        # 观测谱已经是均匀 log 网格：直接用原数组，flux/variance 都不动。
        # 模板仍由 sewlib.temp(velscale=...) 从自身原生波长重采样到
        # 相同的 Δlnλ 网格（其静止系波长 = 观测波长/(1+z)，只差整体平移）。
        gal = ff
        noise_r = np.sqrt(np.maximum(vv, 0))
        gfrac = good.astype(float)
        ln_lam = np.log(w)
        _log("观测谱已均匀 log 网格 → 不重采样，仅模板重采样到同一 Δlnλ 网格",
             key="loggrid")
    else:
        try:
            gal, ln_lam = ppxf_util.log_rebin(w, ff, velscale=velscale)[:2]
            noise_r = np.sqrt(np.maximum(
                ppxf_util.log_rebin(w, vv, velscale=velscale)[0], 0))
            gfrac = ppxf_util.log_rebin(
                w, good.astype(float), velscale=velscale)[0]
        except Exception as e:
            raise RuntimeError(f"log_rebin 失败: {e}")
    lam_obs = np.asarray(w, float) if identity else np.exp(ln_lam)
    gal = np.asarray(gal, float)
    good_r = (gfrac > 0.999) & np.isfinite(gal) & np.isfinite(noise_r) \
        & (noise_r < 100 * math.sqrt(vmed))
    lam_rest = lam_obs / (1.0 + z)

    # --- 模板 ---
    if progress:
        progress(0.25, "构建恒星模板", key="stars")
    fwhm_spec = cfg.get("fwhm") or dict(mode="lambda/R", R=2000)
    fwhm = make_fwhm(fwhm_spec)
    _log(f"FWHM: {getattr(fwhm, 'desc', describe_fwhm(fwhm_spec))}",
         key="fwhm", desc=getattr(fwhm, "desc", describe_fwhm(fwhm_spec)),
         mode=str((fwhm_spec or {}).get("mode") or ""),
         R=f"{(fwhm_spec or {}).get('R', '')}",
         val=f"{(fwhm_spec or {}).get('value', '')}",
         expr=str((fwhm_spec or {}).get("expr", "")),
         path=os.path.basename(str((fwhm_spec or {}).get("path", ""))))
    rest_lo, rest_hi = float(lam_rest[good_r].min()), float(lam_rest[good_r].max())
    norm = [max(5070.0, rest_lo), min(5950.0, rest_hi)]
    if norm[1] - norm[0] < 120:
        mid = 0.5 * (rest_lo + rest_hi)
        norm = [mid - 200.0, mid + 200.0]
    try:
        temp = sewlib.temp(
            template_info["path"], velscale=velscale, FWHM_gal=fwhm,
            age_range=cfg.get("age_range") or [1e-3, 13.7],
            norm_range=norm,
            wave_range=None,
            vacuum=str(cfg.get("template_vacuum", "air")))
    except Exception as e:
        raise RuntimeError(f"模板装载失败: {type(e).__name__}: {e}")
    _log(f"模板 {template_info['name']}: {temp.templates.shape[1]} age × "
         f"{temp.templates.shape[2]} metal，λ={temp.lam_temp[0]:.0f}-"
         f"{temp.lam_temp[-1]:.0f} Å",
         key="template", name=str(template_info["name"]),
         na=int(temp.templates.shape[1]), nm=int(temp.templates.shape[2]),
         lo=f"{temp.lam_temp[0]:.0f}", hi=f"{temp.lam_temp[-1]:.0f}")

    # --- 模板的 (质量, 归一化光) 网格（供质量加权平均年龄/金属丰度用）---
    mass_grid, light_grid = template_mass_light(
        template_info["path"],
        age_range=cfg.get("age_range") or [1e-3, 13.7],
        norm_range=norm)

    # --- 用模板覆盖范围裁观测谱（ppxf 的速度余量要求） ---
    vspan = _num("vmax", 300) - _num("vmin", -300)
    pad = vspan + 900.0
    lo_cov = float(temp.lam_temp[0]) * math.exp(pad / C_KMS)
    hi_cov = float(temp.lam_temp[-1]) * math.exp(-pad / C_KMS)
    # ★ 只能从两端【连续】裁剪：ppxf 要求 lam 是均匀 log 网格
    #   (velscale = c*Δlnλ)。若按布尔掩膜索引，中间坏点会留下"洞"，
    #   数组首尾跨度与长度对不上 → 触发 eq.8 断言。
    valid = (lam_rest >= lo_cov) & (lam_rest <= hi_cov)
    _idx = np.flatnonzero(valid)
    if _idx.size < 50:
        raise ValueError(
            f"模板覆盖范围不足：谱 {rest_lo:.0f}-{rest_hi:.0f} Å，"
            f"模板 {temp.lam_temp[0]:.0f}-{temp.lam_temp[-1]:.0f} Å")
    i0, i1 = int(_idx[0]), int(_idx[-1]) + 1
    lam_rest = lam_rest[i0:i1]
    lam_obs = lam_obs[i0:i1]
    gal = gal[i0:i1]
    noise_r = noise_r[i0:i1]
    good_r = good_r[i0:i1]

    nstar = temp.templates.shape[1] * temp.templates.shape[2]
    tpl_flat = temp.templates.reshape(temp.templates.shape[0], -1).copy()

    moments_deg = int(_num("moments", 2))
    if moments_deg not in (2, 4):
        moments_deg = 2
    vmin = _num("vmin", -300); vmax = _num("vmax", 300)
    smin = _num("sig_min", 5); smax = _num("sig_max", 300)

    use_gas = bool(cfg.get("gas", True))
    gas_names = []
    gas_waves = []
    if use_gas:
        if progress:
            progress(0.35, "构建发射线模板", key="gas")
        try:
            templates, start, moments, component, gas_component, \
                gas_names, gas_waves = sewlib.all_temp_make(
                    tpl_flat, temp.lam_temp,
                    lam_range_gal=np.array([lam_rest[0], lam_rest[-1]]),
                    FWHM_gal=fwhm,
                    tie_balmer=bool(cfg.get("tie_balmer", False)),
                    limit_doublets=bool(cfg.get("limit_doublets", True)),
                    vacuum="vacuum")
        except Exception as e:
            raise RuntimeError(f"发射线模板构建失败: {e}")
        ncomp = int(max(component)) + 1
        if moments_deg == 4:
            start = [np.array([0.0, 150.0, 0.0, 0.0]) for _ in range(ncomp)]
            moments = [4] * ncomp
        else:
            start = [np.array([0.0, 150.0]) for _ in range(ncomp)]
            moments = [2] * ncomp
        bounds = [[[vmin, vmax], [smin, smax]] for _ in range(ncomp)]
        if moments_deg == 4:
            for bb in bounds:
                bb += [[-0.3, 0.3], [-0.3, 0.3]]
        _log(f"发射线模板: {len(gas_names)} 条 ({', '.join(gas_names[:6])}"
             f"{' ...' if len(gas_names) > 6 else ''})",
             key="gas", n=len(gas_names),
             names=", ".join(str(g) for g in gas_names[:6])
             + (" ..." if len(gas_names) > 6 else ""))
    else:
        templates = tpl_flat
        component = np.zeros(nstar, int)
        gas_component = np.zeros(nstar, bool)
        ncomp = 1
        if moments_deg == 4:
            start = [np.array([0.0, 150.0, 0.0, 0.0])]
            moments = [4]
            bounds = [[[vmin, vmax], [smin, smax], [-0.3, 0.3],
                       [-0.3, 0.3]]]
        else:
            start = [np.array([0.0, 150.0])]
            moments = [2]
            bounds = [[[vmin, vmax], [smin, smax]]]

    # ★ ppxf 对 ncomp==1 的 start/bounds 约定不同：它内部会再做
    #   `start1 = [start]` / `bounds = [bounds]`，因此这里必须传【单向量】，
    #   不能再套一层列表（否则 2 <= len(START[0]) 断言失败）。
    if ncomp == 1:
        start = np.asarray(start[0] if isinstance(start, (list, tuple)) else start,
                           float)
        if isinstance(bounds, (list, tuple)) and len(bounds) == 1:
            bounds = bounds[0]

    # --- 拟合 ---
    engine = str(cfg.get("engine", "ppxf_sew"))
    if progress:
        progress(0.45, f"pPXF 拟合（{engine}）", key="ppxf", engine=engine)
    deg = int(_num("degree", -1))
    regul = _num("regul", 0.0)
    # --- 尘埃消光曲线（作用到模板上，A_V / 斜率作为非线性参数一起拟合）---
    #   ★ 仅 pPXF 引擎需要：ppxf-sew 拟合的是等值宽度谱，消光被 att_curve 吸收。
    dust_cfg = cfg.get("dust") or {}
    dust_mode = str(dust_cfg.get("mode") or "none").lower()
    if engine == "ppxf_sew" and dust_mode not in ("none", "off", "", "no"):
        _log("ppxf-sew 拟合等值宽度谱（消光被 att_curve 吸收），已忽略尘埃消光设置",
             key="sewignore")
        dust_mode = "none"
    dust_list = None
    dust_info = None
    if dust_mode not in ("none", "off", "", "no"):
        _dcurve, _dnames, _ddesc = make_dust_curve(dust_cfg)
        _av_rng, _sl_rng = _dust_ranges(dust_cfg)
        _on = str(dust_cfg.get("on") or "stars").lower()
        _stars_mask = (np.asarray(component, int) == 0)
        _gas_mask = np.asarray(gas_component, bool) if use_gas else None
        _npar = len(_dnames)

        def _mk(mask, tag):
            if mask is None or not np.any(mask):
                return None
            if _npar == 1:
                bnds = [[_av_rng[0], _av_rng[1]]]
                st = [float(np.clip(0.2, _av_rng[0], _av_rng[1]))]
            else:
                bnds = [[_av_rng[0], _av_rng[1]], [_sl_rng[0], _sl_rng[1]]]
                st = [float(np.clip(0.2, _av_rng[0], _av_rng[1])),
                      float(0.5 * (_sl_rng[0] + _sl_rng[1]))]
            return dict(start=st, bounds=bnds,
                        component=np.asarray(mask, bool), func=_dcurve,
                        _tag=tag)

        dust_parts = [d for d in [_mk(_stars_mask, "stars")] if d] 
        if _on.startswith("both") or _on.startswith("gas"):
            _g = _mk(_gas_mask, "gas")
            if _g:
                dust_parts.append(_g)
        if dust_parts:
            dust_list = []
            for _d in dust_parts:
                _d2 = {k: v for k, v in _d.items() if k != "_tag"}
                dust_list.append(_d2)
            dust_info = dict(mode=dust_mode, desc=_ddesc, names=list(_dnames),
                             tags=[_d["_tag"] for _d in dust_parts],
                             av_range=_av_rng, slope_range=_sl_rng,
                             on=_on)
            _log(f"尘埃消光: {_ddesc}，作用于 "
                 f"{'/'.join(dust_info['tags'])}，"
                 f"{'/'.join(_dnames)} 范围 "
                 f"{_av_rng[0]:g}~{_av_rng[1]:g}"
                 + (f" / {_sl_rng[0]:g}~{_sl_rng[1]:g}" if _npar > 1 else ""),
                 key="dust", mode=dust_mode,
                 tags="/".join(dust_info["tags"]),
                 names="/".join(_dnames),
                 av=f"{_av_rng[0]:g}~{_av_rng[1]:g}",
                 sl=(f"{_sl_rng[0]:g}~{_sl_rng[1]:g}" if _npar > 1 else ""))

    kw = dict(moments=moments, bounds=bounds, degree=deg,
              linear_method="lsq_box", quiet=False, lam=lam_rest,
              lam_temp=temp.lam_temp)
    if dust_list:
        kw["dust"] = dust_list
    if regul > 0:
        kw["regul"] = regul
        try:
            kw["reg_dim"] = (temp.templates.shape[1], temp.templates.shape[2])
        except Exception:
            pass

    # ★ 无气体时必须传 gas_component=None：ppxf 对"非 None 但全零"会断言
    #   `gas_component` must be nonzero。
    _gcomp = gas_component if use_gas else None
    _gnames = np.asarray(gas_names) if use_gas else None
    # 以 quiet=False 运行，并把 ppxf 的终端输出收进日志
    _buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(_buf):
            if engine == "ppxf":
                p = _Ppxf(templates, gal, noise_r, velscale, start,
                          component=component, gas_component=_gcomp,
                          gas_names=_gnames,
                          goodpixels=np.flatnonzero(good_r), **kw)
            else:
                # mask=True 表示"参与拟合"
                if cfg.get("mask_sync") is not None:
                    _EwfitClass.mask_sync = bool(cfg.get("mask_sync"))
                p = _Ewfit(templates, gal, noise_r, velscale, start,
                           component=component, gas_component=_gcomp,
                           gas_names=_gnames,
                           gas_waves=(gas_waves if use_gas else None),
                           mask=np.asarray(good_r, bool),
                           l=_num("l", 1e-8),
                           norm_lambda=_num("norm_lambda", 5500),
                           delta_lambda=_num("delta_lambda", 200),
                           FWHM=fwhm, clean=bool(cfg.get("clean", False)),
                           **kw)
    finally:
        _txt = _buf.getvalue()
        if _txt.strip():
            _log("ppxf 输出（quiet=False）：", key="ppxfout")
            for _line in _txt.rstrip().splitlines():
                _log("  " + _line)

    if progress:
        progress(0.9, "整理结果", key="results")

    # --- 结果 ---
    sol = np.asarray(p.sol, float)
    if sol.ndim == 1:
        sol = sol.reshape(len(moments), -1)
    comp_names = []
    for i in range(ncomp):
        if i == 0:
            comp_names.append("stellar")
        elif use_gas:
            # component 1 = Balmer, 2 = forbidden
            comp_names.append("gas (Balmer)" if i == 1 else
                              ("gas (forbidden)" if i == 2 else f"gas #{i}"))
        else:
            comp_names.append(f"comp{i}")
    components = []
    for i in range(min(ncomp, sol.shape[0])):
        row = dict(name=comp_names[i],
                   V=float(sol[i, 0]), sigma=float(sol[i, 1]))
        if sol.shape[1] >= 4:
            row["h3"] = float(sol[i, 2])
            row["h4"] = float(sol[i, 3])
        components.append(row)

    ntemp = int(np.asarray(component).size)
    npoly = int(p.weights.size - ntemp)
    star_cols = np.flatnonzero(np.asarray(component, int) == 0)
    gas_cols = np.flatnonzero(np.asarray(gas_component, bool))
    try:
        mat = np.asarray(p.matrix, float)
        wts = np.asarray(p.weights, float)
        stellar_fit = mat[:, star_cols] @ wts[star_cols]
        gas_fit = (mat[:, gas_cols] @ wts[gas_cols]
                   if gas_cols.size else np.zeros_like(stellar_fit))
    except Exception:
        stellar_fit = np.asarray(p.bestfit, float)
        gas_fit = np.zeros_like(stellar_fit)
    bestfit = np.asarray(p.bestfit, float)
    galaxy_n = np.asarray(p.galaxy, float)
    resid = galaxy_n - bestfit
    rms = float(np.sqrt(np.mean(resid[good_r] ** 2))) if good_r.any() else np.nan
    # ★ 用【未归一】的输入谱/噪声算 S/N：p.galaxy 已被 ppxf 除以 mean_gal，
    #   而 noise_r 是原始单位，两者相除会得到 ~1e17 的假值。
    snr = float(np.nanmedian(gal[good_r] / noise_r[good_r])) \
        if good_r.any() else np.nan
    # ★ pPXF 内部把 galaxy/noise 除以 mean_gal；输出数组/流量乘回来，
    #   使交互图与输入谱同一量纲。EW 是强度比，不需要乘。
    _mg = float(getattr(p, "mean_gal", 1.0) or 1.0)
    if not np.isfinite(_mg) or _mg <= 0:
        _mg = 1.0      # 标准 pPXF 不做归一化
    galaxy_out = galaxy_n * _mg
    bestfit = bestfit * _mg
    stellar_fit = np.asarray(stellar_fit, float) * _mg
    gas_fit = np.asarray(gas_fit, float) * _mg
    resid = resid * _mg
    rms = rms * _mg
    noise_out = np.asarray(noise_r, float) * _mg

    # 星族权重网格
    weights_grid = None
    try:
        sw = np.asarray(p.weights[:nstar], float)
        weights_grid = sw.reshape(temp.templates.shape[1],
                                  temp.templates.shape[2])
    except Exception:
        pass

    # ---- 平均年龄 / 金属丰度：光度加权 与 质量加权 ----
    #   光度加权 = Σw·X/Σw（ppxf_sew 的 mean_age_metal 同口径）；
    #   质量加权 = Σ(w·M/L)·X/Σ(w·M/L)，M/L 用模板库的 masses/归一化光。
    stellar_stats = None
    if weights_grid is not None:
        try:
            _lg = np.asarray(temp.age_grid, float)      # logAge[yr]
            _metalg = np.asarray(temp.metal_grid, float)    # [M/H]
            _light = _weighted_stats(weights_grid, _lg, _metalg)
            _mass = None
            if mass_grid is not None and light_grid is not None \
                    and mass_grid.shape == weights_grid.shape:
                with np.errstate(divide="ignore", invalid="ignore"):
                    _ml = mass_grid / light_grid
                _ml = np.where(np.isfinite(_ml) & (_ml > 0), _ml, 0.0)
                _mass = _weighted_stats(weights_grid, _lg, _metalg, _ml)
            stellar_stats = dict(light=_light, mass=_mass,
                                 log_age_grid=float(np.nanmin(_lg)),
                                 log_age_max=float(np.nanmax(_lg)))
            if _light:
                _log(f"光度加权: <logAge>={_light['log_age']:.3f} "
                     f"(≈{10 ** (_light['log_age'] - 9):.3g} Gyr), "
                     f"<[M/H]>={_light['metal']:.3f}",
                     key="lightw", logage=f"{_light['log_age']:.3f}",
                     gyr=f"{10 ** (_light['log_age'] - 9):.3g}",
                     mh=f"{_light['metal']:.3f}")
            if _mass:
                _log(f"质量加权: <logAge>={_mass['log_age']:.3f} "
                     f"(≈{10 ** (_mass['log_age'] - 9):.3g} Gyr), "
                     f"<[M/H]>={_mass['metal']:.3f}",
                     key="massw", logage=f"{_mass['log_age']:.3f}",
                     gyr=f"{10 ** (_mass['log_age'] - 9):.3g}",
                     mh=f"{_mass['metal']:.3f}")
        except Exception as _e:
            _log(f"平均年龄/金属丰度计算失败: {type(_e).__name__}: {_e}",
                 key="statsfail", err=f"{type(_e).__name__}: {_e}")
            stellar_stats = None

    # 气体线表
    gas_table = []
    if use_gas:
        gf = getattr(p, "gas_flux", None)
        ge = getattr(p, "gas_flux_error", None)
        # EW（仅 ppxf_sew 有 galaxy_l）
        ew = None
        try:
            if getattr(p, "galaxy_l", None) is not None and gas_cols.size:
                fr = (((velscale / C_KMS) * p.lam) / np.asarray(p.galaxy_l)) \
                    @ np.asarray(p.matrix)[:, gas_cols]
                ew = np.asarray(p.weights)[gas_cols] * fr
        except Exception:
            ew = None
        for j, name in enumerate(np.asarray(p.gas_names)):
            comp_id = int(np.asarray(component)[gas_cols[j]]) \
                if j < gas_cols.size else -1
            gas_table.append(dict(
                name=str(name),
                flux=(None if gf is None else float(gf[j]) * _mg),
                flux_err=(None if ge is None else float(ge[j]) * _mg),
                ew=(None if ew is None else float(np.asarray(ew)[j])),
                component=(comp_names[comp_id]
                           if 0 <= comp_id < len(comp_names) else ""),
                V=(float(sol[comp_id, 0]) if 0 <= comp_id < sol.shape[0]
                   else None),
                sigma=(float(sol[comp_id, 1]) if 0 <= comp_id < sol.shape[0]
                        else None),
            ))

    # --- PNG（使用官方 plot 接口；ppxf_sew 需乘回 mean_gal）---
    plot_png = None
    try:
        plot_png = render_fit_plot(p, scale=_mg)
    except Exception as e:
        _log(f"p.plot() 渲染失败: {e}", key="plotfail", err=str(e))
    weight_png = None
    try:
        weight_png = render_weight_plot(temp, weights_grid)
    except Exception as e:
        _log(f"template.plot() 渲染失败: {e}", key="wplotfail", err=str(e))

    dt = time.time() - t0
    if progress:
        progress(1.0, "完成", key="done")

    # 尘埃消光拟合结果（ppxf 把每个 dust 分量的参数放在 d["sol"]）
    _dust_out = None
    if dust_info:
        _dust_out = []
        _dl = getattr(p, "dust", None) or []
        for _i, _tag in enumerate(dust_info["tags"]):
            _sol = None
            if _i < len(_dl) and "sol" in _dl[_i]:
                _sol = [float(v) for v in np.asarray(_dl[_i]["sol"], float)]
            _dust_out.append(dict(
                tag=_tag, mode=dust_info["mode"], desc=dust_info["desc"],
                names=list(dust_info["names"]),
                values=_sol))
        _log("尘埃消光拟合: " + "；".join(
            f"{d['tag']} " + ", ".join(
                f"{n}={v:.3f}" for n, v in zip(d["names"], d["values"] or []))
            for d in _dust_out),
            key="dustfit",
            parts="; ".join(
                f"{d['tag']} " + ", ".join(
                    f"{n}={v:.3f}" for n, v in zip(d["names"], d["values"] or []))
                for d in _dust_out))

    def _lst(a, nd=6):
        """转 JSON 列表：非有限 -> None；用有效数字（.g），不可用固定小数位。

        天文光谱 flux ~1e-18，`np.round(x, 6)` 会全部变成 0（曾把整幅图/
        整个拟合谱打平）。"""
        a = np.asarray(a, float)
        fmt = "{:." + str(int(nd) if nd else 8) + "g}"
        return [None if not np.isfinite(v) else float(fmt.format(v))
                for v in a]

    result = dict(
        ok=True,
        engine=engine,
        template=dict(name=template_info.get("name") if template_info else None,
                      path=template_info.get("path") if template_info else None),
        z=float(z),
        redshift=zinfo,
        fwhm=dict(spec=fwhm_spec,
                  text=getattr(fwhm, "desc", describe_fwhm(fwhm_spec))),
        wave_system=_wave_system,
        velscale=float(velscale),
        dlam=float(dlam), dlam_mode=dlam_mode, dlam_src=dlam_src,
        moments=moments,
        degree=deg,
        use_gas=use_gas,
        gas_names=[str(x) for x in (p.gas_names if use_gas else [])],
        components=components,
        stellar_stats=stellar_stats,
        dust=_dust_out,
        dust_desc=(dust_info["desc"] if dust_info else None),
        chi2=float(getattr(p, "chi2", np.nan)),
        dof=int(getattr(p, "dof", 0)),
        rms=rms,
        snr=snr,
        # 通量归一因子：p.galaxy = 原始谱 / mean_gal（pPXF 内部约定）
        mean_gal=float(_mg),
        n_pixels=int(gal.size),
        n_good=int(good_r.sum()),
        fit_seconds=float(dt),
        gas=gas_table,
        wave_rest=_lst(lam_rest, 8),
        wave_obs=_lst(lam_obs, 8),
        galaxy=_lst(galaxy_out, 0),
        bestfit=_lst(bestfit, 0),
        stellar=_lst(stellar_fit, 0),
        gas_model=_lst(gas_fit, 0),
        noise=_lst(noise_out, 0),
        residual=_lst(resid, 0),
        good=good_r.astype(int).tolist(),
        weights=(None if weights_grid is None
                 else np.round(weights_grid, 8).tolist()),
        age_grid=np.round(np.asarray(temp.age_grid, float), 6).tolist(),
        metal_grid=np.round(np.asarray(temp.metal_grid, float), 6).tolist(),
        plot_png=plot_png,
        weight_png=weight_png,
        log=msgs,
        log_i18n=log_i18n,
    )
    return result
