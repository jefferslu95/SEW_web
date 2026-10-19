# -*- coding: utf-8 -*-
"""FITS 读写与格式自动识别。

支持：
  * 一维单光谱（flux 可能在 HDU0，也可能在任意扩展；波长可能在
    WAVE/IVAR/MASK 扩展、头卡 WCS、SDSS COEFF0/COEFF1 或 log10 波长）
  * 二维多光谱（每行一条谱）
  * 三维数据立方（谱轴可能在 numpy 轴 0/1/2；波长可能线性或 log10）

核心入口：
    ds = open_dataset(path, override=None)
    ds.meta()                    -> JSON 友好的元信息
    ds.spectrum(x=..., y=...)    -> 单谱/像素谱
    ds.image(mode=...)           -> 2D 预览图（用于前端 canvas）
"""
from __future__ import annotations

import gzip
import json
import math
import os
import shutil
import tempfile
import time
import warnings
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
from astropy.io import fits

from . import binning

C_KMS = 299792.458

# FITS BITPIX -> numpy dtype 名称（避免为了拿 dtype 去读整块数据）
_BITPIX_DTYPE = {8: "uint8", 16: "int16", 32: "int32", 64: "int64",
                 -32: "float32", -64: "float64"}

# ---------------------------------------------------------------------------
# HDU 角色候选名
# ---------------------------------------------------------------------------
_FLUX_NAMES = ("FLUX", "SCI", "SCIENCE", "DATA", "COUNTS", "PRIMARY")
_VAR_NAMES = ("VAR", "VARIANCE", "IVAR", "ERROR", "ERR", "SIGMA", "NOISE",
              "ERRMAP", "STAT", "STATISTICS")
_MASK_NAMES = ("MASK", "DQ", "BAD", "BADPIX", "QUALITY", "FLAGS")
_WAVE_NAMES = ("WAVE", "WAVELENGTH", "LAMBDA", "LOGWAVE", "WAVES")

_GZ_CACHE_DIR = os.path.join(tempfile.gettempdir(), "sew_web_fits_cache")


def memmap_path(path: str) -> str:
    """返回可用于 memmap 的本地未压缩路径。

    astropy 对 ``.fits.gz`` 不能 memmap，每次访问 ``.data`` 都要从 gzip
    流的开头重新解压（一个 200 MB 的压缩 cube 会被反复解压几十次，
    实测 50 s+）。这里只在首次打开时解压一次到临时缓存，之后直接
    memmap，后续所有 HDU 访问都是普通文件读取。
    """
    p = os.path.abspath(path)
    if not p.lower().endswith(".gz"):
        return p
    try:
        st = os.stat(p)
    except OSError:
        return p
    os.makedirs(_GZ_CACHE_DIR, exist_ok=True)
    base = os.path.basename(p)
    key = f"{base}.{st.st_size}.{int(st.st_mtime)}.fits"
    target = os.path.join(_GZ_CACHE_DIR, key)
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return target
    tmp = f"{target}.{os.getpid()}.{int(time.time() * 1000)}.part"
    with gzip.open(p, "rb") as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst, length=8 << 20)
    os.replace(tmp, target)
    return target


def hdu_shape_dtype(hdu):
    """从头部拿 (shape, dtype)，不读取数据本体。"""
    hdr = hdu.header
    nd = int(hdr.get("NAXIS", 0) or 0)
    if nd <= 0:
        return None, None
    shape = []
    for i in range(1, nd + 1):
        try:
            shape.append(int(hdr.get(f"NAXIS{i}", 0) or 0))
        except (TypeError, ValueError):
            return None, None
    shape = tuple(reversed(shape))
    dtype = _BITPIX_DTYPE.get(int(hdr.get("BITPIX", 0) or 0))
    return shape, dtype


def sample_blocks(d, max_blocks: int = 5):
    """取沿第一维均匀分布的少量连续块，用于统计（避免扫全数组）。

    memmap 的跨步切片仍会触碰大量文件页，连续块则只读几 MB。
    """
    if d is None:
        return None
    try:
        nd = d.ndim
        n = d.shape[0]
    except AttributeError:
        return np.asarray(d)
    if nd < 2 or n <= 0:
        return np.asarray(d)
    k = min(max_blocks, n)
    idx = np.unique(np.linspace(0, n - 1, k).astype(int))
    return np.asarray(d[idx])


def _name_has(name: str, keys) -> bool:
    n = (name or "").upper()
    return any(k in n for k in keys)


def _unit_scale(cunit: str) -> float:
    """波长单位 -> 埃 的倍数。"""
    u = (cunit or "").strip().lower().replace(" ", "")
    if not u:
        return 1.0
    if "angstrom" in u or u in ("a", "aa", "\u00c5", "ang"):
        return 1.0
    if "nanometer" in u or u in ("nm",):
        return 10.0
    if "micron" in u or u in ("um", "\u03bcm", "microns", "micrometer"):
        return 1e4
    if u in ("mm", "millimeter"):
        return 1e7
    if u in ("m", "meter", "metre"):
        return 1e10
    return 1.0


def _freq_to_angstrom(freq: np.ndarray, cunit: str) -> np.ndarray:
    """频率 -> 埃（真空）。"""
    u = (cunit or "").strip().lower().replace(" ", "")
    if "ghz" in u:
        f_hz = freq * 1e9
    elif "mhz" in u:
        f_hz = freq * 1e6
    elif "khz" in u:
        f_hz = freq * 1e3
    else:
        f_hz = freq
    f_hz = np.where(f_hz > 0, f_hz, np.nan)
    return 2.99792458e18 / f_hz


# ---------------------------------------------------------------------------
# 头卡 -> 波长
# ---------------------------------------------------------------------------
def _axis_wave(hdr, axis: int, n: int) -> Optional[tuple[np.ndarray, str]]:
    """由头卡构造某个 FITS 轴（1-based）的波长数组。

    返回 (wave_angstrom, source_description)；无法识别返回 None。
    """
    ctype = str(hdr.get(f"CTYPE{axis}", "")).strip()
    cunit = str(hdr.get(f"CUNIT{axis}", "")).strip()

    # SDSS 的 log10 波长
    if axis == 1 and ("COEFF0" in hdr and "COEFF1" in hdr):
        c0, c1 = float(hdr["COEFF0"]), float(hdr["COEFF1"])
        wave = 10.0 ** (c0 + c1 * np.arange(n))
        return wave, f"COEFF0/COEFF1 (log10, coeff0={c0:.6f}, coeff1={c1:.3g})"

    crval = hdr.get(f"CRVAL{axis}", None)
    if crval is None:
        return None
    crval = float(crval)
    crpix = float(hdr.get(f"CRPIX{axis}", 1.0))
    # CD 矩阵优先
    cd = hdr.get(f"CD{axis}_{axis}", None)
    if cd is not None:
        cdelt = float(cd)
    else:
        cdelt = hdr.get(f"CDELT{axis}", None)
        if cdelt is None:
            return None
        cdelt = float(cdelt)
    x = crval + cdelt * (np.arange(n) - (crpix - 1.0))
    ct = ctype.lower()

    # ---- 频率轴 ----
    if "freq" in ct:
        wave = _freq_to_angstrom(x, cunit or "Hz")
        return wave, f"CTYPE{axis}={ctype} (freq -> Angstrom)"

    # ---- 波长轴：判断是否 log10(λ) ----
    is_log = ("log" in ct)
    if not is_log:
        # WiFeS/MUSE 常用 CTYPE3='wave' + CRVAL3≈3.5 + CDELT3≈1e-5（log10）
        if 2.0 < abs(crval) < 7.0 and 0 < abs(cdelt) < 5e-3:
            is_log = True
        # COEFF 风格
        if "coeff" in ct:
            is_log = True
    if is_log:
        wave = (10.0 ** x) * _unit_scale(cunit)
        return wave, (f"CTYPE{axis}={ctype or 'log-wave'} "
                      f"(log10, CRVAL={crval:.6g}, CDELT={cdelt:.3g})")
    wave = x * _unit_scale(cunit)
    return wave, (f"CTYPE{axis}={ctype or 'linear'} "
                  f"(linear, CRVAL={crval:.6g}, CDELT={cdelt:.3g}, "
                  f"CUNIT={cunit or 'A'})")


def _wave_from_hdu(hdu, n: int) -> Optional[tuple[np.ndarray, str]]:
    """从一个扩展读取一维波长数组（WAVE / LAMBDA ...）。"""
    if hdu.data is None:
        return None
    d = np.asarray(hdu.data)
    if d.ndim == 2:
        if 1 in d.shape:
            d = d.ravel()
        else:
            return None
    if d.ndim != 1 or d.size != n:
        return None
    d = d.astype(float)
    hdr = hdu.header
    scale = _unit_scale(str(hdr.get("CUNIT1", hdr.get("BUNIT", ""))))
    nm = (hdu.name or "").upper()
    is_log = "LOG" in nm or "log" in str(hdr.get("CTYPE1", "")).lower()
    if is_log:
        d = 10.0 ** d
    wave = d * scale
    return wave, f"HDU[{hdu.name or '?'}] 1-D wavelength"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class Dataset:
    path: str
    override: dict = field(default_factory=dict)
    hdul: Any = None
    hdus: list = field(default_factory=list)
    kind: str = "spectrum"             # spectrum | cube
    warnings: list = field(default_factory=list)
    # 结构化的警告代码（供前端本地化）：[{"code": ..., "params": {...}}]
    warning_codes: list = field(default_factory=list)

    # 角色 -> HDU index
    flux_hdu: Optional[int] = None
    var_hdu: Optional[int] = None
    mask_hdu: Optional[int] = None
    wave_hdu: Optional[int] = None
    # BINTABLE 光谱（LAMOST 等）的列名
    flux_col: Optional[str] = None
    var_col: Optional[str] = None
    mask_col: Optional[str] = None
    wave_col: Optional[str] = None
    is_table: bool = False
    _auto_flux_col: Optional[str] = None
    var_kind: str = "var"              # var | sigma | ivar
    _fallback_var_kind: Optional[str] = None
    mask_semantics: str = "bad_nonzero"

    # 数组布局
    flux: Any = None                   # cube: (nwave, ny, nx); spec: (nwave,)
    var: Any = None
    mask: Any = None
    wave: Optional[np.ndarray] = None
    wave_source: str = ""
    nwave: int = 0
    ny: int = 1
    nx: int = 1
    spec_axis: Optional[int] = None    # numpy axis of wavelength in original data
    row: int = 0
    nrows: int = 1

    # 元信息
    objname: str = ""
    instrument: str = ""
    bunit: str = ""
    header0: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    def close(self):
        try:
            if self.hdul is not None:
                self.hdul.close()
        except Exception:
            pass
        self.hdul = None

    # ------------------------------------------------------------------
    def _inspect(self):
        h = self.hdul
        self.hdus = []
        for i, hdu in enumerate(h):
            if isinstance(hdu, fits.BinTableHDU):
                nrows = int(hdu.header.get("NAXIS2", 0) or 0)
                cols = []
                for c in hdu.columns:
                    try:
                        arr = np.asarray(hdu.data[c.name])
                        cshape = [int(x) for x in arr.shape]
                        rshape = (cshape[1:] if cshape and cshape[0] == nrows
                                  else cshape)
                        cdtype = str(arr.dtype)
                    except Exception:
                        cshape, rshape, cdtype = [], [], None
                    cols.append(dict(name=str(c.name), format=str(c.format),
                                     shape=cshape, row_shape=rshape,
                                     dtype=cdtype))
                self.hdus.append(dict(
                    index=i, name=(hdu.name or ""),
                    ver=int(hdu.header.get("VER", 1) or 1),
                    type="BinTableHDU", shape=[nrows] if nrows else [],
                    nrows=nrows, columns=cols, dtype=None,
                    bunit=str(hdu.header.get("BUNIT", "")),
                    ctype3=str(hdu.header.get("CTYPE3", "")),
                    ctype1=str(hdu.header.get("CTYPE1", "")),
                ))
                continue
            # ★ 不读原始数据：只用头部 NAXIS*/BITPIX。对大 cube（数 GB）
            #   以及 .gz 文件，读一次 data 可能就要几秒。
            shape, dtype = hdu_shape_dtype(hdu)
            self.hdus.append(dict(
                index=i, name=(hdu.name or ""),
                ver=int(hdu.header.get("VER", hdu.ver) or 1),
                type=type(hdu).__name__, shape=shape,
                dtype=dtype,
                bunit=str(hdu.header.get("BUNIT", "")),
                ctype3=str(hdu.header.get("CTYPE3", "")),
                ctype1=str(hdu.header.get("CTYPE1", "")),
            ))
        hdr0 = h[0].header
        self.objname = str(hdr0.get("OBJECT", "") or "").strip()
        self.instrument = str(hdr0.get("INSTRUME", "") or "").strip()
        if not self.instrument:      # 有些 spaxel/扩展把 INSTRUME 放在 HDU1+
            for _hdu in h:
                _v = str(_hdu.header.get("INSTRUME", "") or "").strip()
                if _v:
                    self.instrument = _v
                    break
        self.header0 = {k: hdr0[k] for k in hdr0
                        if isinstance(hdr0[k], (int, float, str))}
        self.bunit = str(hdr0.get("BUNIT", "") or "").strip()

    # ------------------------------------------------------------------
    def _pick_flux(self):
        ov = self.override
        if ov.get("flux_hdu") is not None:
            i = int(ov["flux_hdu"])
            if i >= len(self.hdus) or self.hdus[i]["shape"] is None:
                raise ValueError(f"flux_hdu={i} 无有效数据")
            return i
        best, best_score = None, -1e9
        for info in self.hdus:
            if info["shape"] is None or info["type"] == "BinTableHDU":
                continue
            nm = info["name"]
            if _name_has(nm, _WAVE_NAMES + _VAR_NAMES + _MASK_NAMES):
                continue
            nd = len(info["shape"])
            if self.kind == "cube" and nd != 3:
                continue
            if self.kind == "spectrum" and nd not in (1, 2):
                continue
            if self.kind == "spectrum" and nd == 1 and info["shape"][0] < 16:
                continue
            score = 0.0
            if _name_has(nm, _FLUX_NAMES):
                score += 8.0
            if info["index"] == 0:
                score += 4.0
            if nd == 1:
                score += 2.0
            # ★ 只统计少量连续块（避免对 GB 级 cube 全盘扫描）
            d = sample_blocks(self.hdul[info["index"]].data)
            frac = float(np.isfinite(d).mean()) if d is not None and d.size else 0.0
            score += 3.0 * frac
            # 常值扩展（例如 instrument map）降权
            sample = d.ravel()[::max(1, d.size // 5000)] if d is not None and d.size else d
            if sample is not None and sample.size and np.nanstd(sample) == 0:
                score -= 5.0
            if score > best_score:
                best, best_score = info["index"], score
        if best is None:
            # ---- BINTABLE 光谱兜底（LAMOST: COADD/FLUX/IVAR/WAVELENGTH） ----
            best_col = None
            for info in self.hdus:
                if info.get("type") != "BinTableHDU":
                    continue
                for c in info.get("columns", []):
                    rs = c.get("row_shape") or []
                    if len(rs) != 1 or rs[0] < 16:
                        continue
                    nm = str(c["name"]).upper()
                    score = 0.0
                    if _name_has(nm, _FLUX_NAMES):
                        score += 10.0
                    if nm in ("FLUX", "SCI", "DATA"):
                        score += 6.0
                    if str(info.get("name", "")).upper() in (
                            "COADD", "SCI", "PRIMARY"):
                        score += 3.0
                    if str(ov.get("flux_col", "")) == c["name"]:
                        score += 100.0
                    if score > best_score:
                        best, best_score = info["index"], score
                        best_col = c["name"]
            if best is not None:
                self._auto_flux_col = best_col
                return best
            raise ValueError("找不到可用的 flux HDU/列，请手动指定")
        return best

    def _pick_companion(self, role: str):
        """按名字 + 形状挑选 var / mask HDU。"""
        ov = self.override
        key = f"{role}_hdu"
        if key in ov:
            v = ov.get(key)
            if v in (None, -1, "none", "None", ""):
                return None
            return int(v)
        names = _VAR_NAMES if role == "var" else _MASK_NAMES
        fshape = self.hdus[self.flux_hdu]["shape"]
        best, best_score = None, -1e9
        for info in self.hdus:
            if info["index"] == self.flux_hdu or info["shape"] is None:
                continue
            if info["shape"] != fshape:
                # 允许 (ny,nx) 空间 mask 应用到整 cube
                if not (len(fshape) == 3 and info["shape"] == fshape[1:]):
                    continue
            nm = info["name"]
            if not _name_has(nm, names):
                continue
            score = 10.0
            if role == "var":
                if "IVAR" in nm.upper():
                    score += 3
                if "ERR" in nm.upper() or "SIGMA" in nm.upper():
                    score += 2
            else:
                if nm.upper() in ("DQ", "MASK"):
                    score += 3
            if score > best_score:
                best, best_score = info["index"], score
        if best is not None:
            return best
        # 无名扩展兜底：很多 cube 只有 [flux, variance] 两层，扩展名是空的。
        # 判据：形状相同、无名字、数值非负且量级远小于 flux（方差 ≈ σ²）。
        if role == "var":
            fd = sample_blocks(self.hdul[self.flux_hdu].data)
            mf = float(np.nanmedian(np.abs(fd))) if fd is not None and fd.size else 0.0
            cands = [i["index"] for i in self.hdus
                     if i["index"] != self.flux_hdu
                     and i["shape"] == fshape
                     and (not i["name"])]
            for i in cands:
                d = sample_blocks(self.hdul[i].data)
                if d is None or d.size == 0:
                    continue
                s = d.ravel()[::max(1, d.size // 20000)]
                s = s[np.isfinite(s)]
                if s.size == 0:
                    continue
                med = float(np.median(s))
                negfrac = float(np.mean(s < 0))
                if med > 0 and negfrac < 0.1:
                    # variance: 量级 ~ flux²；ivar: med·flux² ~ 1
                    ratio = med * mf * mf if mf > 0 else np.inf
                    if 1e-2 < ratio < 1e2:
                        self._fallback_var_kind = "ivar"
                        self.warnings.append(
                            f"HDU[{i}] 无扩展名，按数值特征识别为 IVAR")
                        self.warning_codes.append(
                            {"code": "ivar", "params": {"i": int(i)}})
                        return i
                    if mf <= 0 or med < 0.1 * mf:
                        self._fallback_var_kind = "var"
                        self.warnings.append(
                            f"HDU[{i}] 无扩展名，按数值特征识别为 variance")
                        self.warning_codes.append(
                            {"code": "var", "params": {"i": int(i)}})
                        return i
        return None

    def _is_table_hdu(self, idx) -> bool:
        return idx is not None and isinstance(self.hdul[idx], fits.BinTableHDU)

    def _pick_table_columns(self):
        """在 flux 所在的 BINTABLE 里挑 flux/var/mask/wave 列。"""
        ov = self.override
        tbl = self.hdul[self.flux_hdu]
        names = [str(x) for x in tbl.columns.names]

        def pick(key, candidates):
            if ov.get(key):
                return str(ov[key])
            up = {n.upper(): n for n in names}
            for c in candidates:
                if c in up:
                    return up[c]
            return None

        self.flux_col = (str(ov.get("flux_col"))
                         if ov.get("flux_col") else
                         (self._auto_flux_col or
                          next((n for n in names if n.upper() in
                                ("FLUX", "SCI", "DATA")),
                               names[0] if names else None)))
        self.var_col = pick("var_col", ("IVAR", "VAR", "VARIANCE", "ERR",
                                       "ERROR", "SIGMA", "NOISE"))
        self.mask_col = pick("mask_col", ("ORMASK", "ANDMASK", "MASK", "DQ",
                                         "BADPIX", "FLAGS"))
        self.wave_col = pick("wave_col", ("WAVELENGTH", "WAVE", "LAMBDA",
                                         "LOGWAVE", "WAVES"))
        # 显式 var_hdu/mask_hdu/wave_hdu = None ⇒ 禁用该角色
        for role in ("var", "mask", "wave"):
            if role + "_hdu" in ov and ov.get(role + "_hdu") in (
                    None, -1, "", "none", "None"):
                setattr(self, role + "_col", None)
        if self.var_col:
            self.var_hdu = self.flux_hdu
        if self.mask_col:
            self.mask_hdu = self.flux_hdu
        if self.wave_col:
            self.wave_hdu = self.flux_hdu

    def _var_kind(self):
        if self.override.get("var_kind"):
            return str(self.override["var_kind"])
        if self.var_col:
            nm = str(self.var_col).upper()
            if "IVAR" in nm:
                return "ivar"
            if any(k in nm for k in ("ERR", "SIGMA", "NOISE", "ERROR")):
                return "sigma"
            return "var"
        if self.var_hdu is None:
            return "none"
        nm = (self.hdus[self.var_hdu]["name"] or "").upper()
        if "IVAR" in nm:
            return "ivar"
        if any(k in nm for k in ("ERR", "SIGMA", "NOISE", "ERROR")):
            return "sigma"
        if self._fallback_var_kind:
            return self._fallback_var_kind
        return "var"

    def _mask_mode(self):
        return str(self.override.get("mask_semantics", "bad_nonzero"))

    # ------------------------------------------------------------------
    def _table_col_row(self, col: str, row=None):
        """读取 BINTABLE 某列的一行（LAMOST 列形状 (nrows, nwave)）。"""
        if not col:
            return None
        arr = np.asarray(self.hdul[self.flux_hdu].data[col], dtype=float)
        if arr.ndim == 3 and arr.shape[1] == 1:
            arr = arr[:, 0, :]
        if arr.ndim == 2:
            r = int(self.row if row is None else row)
            r = max(0, min(arr.shape[0] - 1, r))
            return np.asarray(arr[r], dtype=float)
        return np.asarray(arr, dtype=float).ravel()

    def _layout_table(self):
        arr = np.asarray(self.hdul[self.flux_hdu].data[self.flux_col],
                         dtype=float)
        if arr.ndim == 3 and arr.shape[1] == 1:
            arr = arr[:, 0, :]
        if arr.ndim == 2:
            self.nrows = int(arr.shape[0])
            self.spec_axis = 1
            self.row = int(self.override.get("row", 0) or 0)
            self.row = max(0, min(self.nrows - 1, self.row))
            self.flux = arr[self.row]
        elif arr.ndim == 1:
            self.nrows = 1
            self.row = 0
            self.spec_axis = 0
            self.flux = arr
        else:
            raise ValueError(f"BINTABLE flux 列维数不支持: {arr.shape}")
        self.nwave = int(np.asarray(self.flux).size)
        self.ny, self.nx = 1, 1

    def _layout(self):
        """确定谱轴 / canoncial 布局。"""
        if self.is_table:
            self._layout_table()
            return
        d = self.hdul[self.flux_hdu].data
        hdr = self.hdul[self.flux_hdu].header
        nd = d.ndim
        spec_np_axis = None

        if nd == 3:
            # 先找 CTYPE 含 wave/freq 的 FITS 轴
            for nap in range(nd):
                faxis = nd - nap  # numpy axis -> FITS axis number
                ct = str(hdr.get(f"CTYPE{faxis}", "")).lower()
                if any(k in ct for k in ("wave", "awav", "freq", "log")):
                    spec_np_axis = nap
                    break
            if spec_np_axis is None:
                # 再找 CRVAL 像 log10(λ)（WiFeS: CRVAL3≈3.54, CDELT3≈3e-5）
                for nap in range(nd):
                    faxis = nd - nap
                    cv = hdr.get(f"CRVAL{faxis}", None)
                    cd = hdr.get(f"CDELT{faxis}", hdr.get(f"CD{faxis}_{faxis}"))
                    if cv is not None and cd is not None:
                        if 2.0 < abs(float(cv)) < 7.0 and 0 < abs(float(cd)) < 5e-3:
                            spec_np_axis = nap
                            break
            if spec_np_axis is None:
                spec_np_axis = 0 if d.shape[0] >= max(d.shape[1:]) else nd - 1
                self.warnings.append(
                    "未在头卡识别谱轴，按最长轴自动判定（可在界面手动覆盖）")
                self.warning_codes.append({"code": "specaxis", "params": {}})
            self.spec_axis = int(spec_np_axis)
            order = [spec_np_axis] + [i for i in range(nd) if i != spec_np_axis]
            # 记录空间轴（canonical y/x 对应的原始 numpy 轴），供天体测量用
            self.nd_flux = int(nd)
            self.ny_np_axis = int(order[1])
            self.nx_np_axis = int(order[2])
            self.flux = np.moveaxis(d, order, list(range(nd)))
            if self.flux.shape[2] > self.flux.shape[1]:
                # 保持 (nwave, ny, nx)，不做翻转；y/x 只是显示顺序
                pass
            self.nwave, self.ny, self.nx = self.flux.shape
        elif nd == 2:
            # 多光谱 / RSS：谱轴取参数或最长轴
            sa = self.override.get("spec_axis")
            if sa is None:
                sa = 1 if d.shape[1] >= d.shape[0] else 0
            self.spec_axis = int(sa)
            other = 1 - self.spec_axis
            self.nrows = int(d.shape[other])
            self.row = int(self.override.get("row", 0) or 0)
            self.row = max(0, min(self.nrows - 1, self.row))
            view = np.moveaxis(d, self.spec_axis, 0)  # (nwave, nrows)
            self.flux = view[:, self.row]
            self.nwave, self.ny, self.nx = int(view.shape[0]), 1, 1
        elif nd == 1:
            self.flux = d
            self.nwave, self.ny, self.nx = int(d.size), 1, 1
        else:
            raise ValueError(f"不支持的 flux 维度 {nd}")

    # ------------------------------------------------------------------
    def _load_table_companions(self):
        self.var = None
        self.mask = None
        if self.var_col:
            v = self._table_col_row(self.var_col)
            kind = self._var_kind()
            v = np.asarray(v, float)
            if kind == "ivar":
                with np.errstate(divide="ignore", invalid="ignore"):
                    v = np.where(v > 0, 1.0 / v, np.nan)
            elif kind == "sigma":
                v = v ** 2
            self.var = v
            self.var[~np.isfinite(self.var)] = np.nan
            self.var[self.var <= 0] = np.nan
        if self.mask_col:
            m = np.asarray(self._table_col_row(self.mask_col))
            self.mask = ((m == 0) if self._mask_mode() == "good_nonzero"
                         else (m != 0))
        bad = ~np.isfinite(self.flux)
        self.mask = bad if self.mask is None else np.logical_or(self.mask, bad)

    def _load_companions(self):
        if self.is_table:
            self._load_table_companions()
            return
        d = self.hdul[self.flux_hdu].data
        nd = d.ndim
        # ---- variance ----
        if self.var_hdu is not None:
            v = np.asarray(self.hdul[self.var_hdu].data)
            if v.ndim == 3:
                order = [self.spec_axis] + [i for i in range(3)
                                            if i != self.spec_axis]
                v = np.moveaxis(v, order, [0, 1, 2])
            elif v.ndim == 2:
                if nd == 3:
                    # 空间 2D -> 广播成 3D
                    v = np.broadcast_to(v[None, :, :], self.flux.shape)
                else:
                    v = np.moveaxis(v, self.spec_axis, 0)[:, self.row]
            kind = self._var_kind()
            if kind == "ivar":
                with np.errstate(divide="ignore", invalid="ignore"):
                    v = np.where(np.asarray(v, float) > 0,
                                 1.0 / np.asarray(v, float), np.nan)
            elif kind == "sigma":
                v = np.asarray(v, float) ** 2
            self.var = np.asarray(v, float)
        # ---- mask ----
        if self.mask_hdu is not None:
            m = np.asarray(self.hdul[self.mask_hdu].data)
            if m.ndim == 3:
                order = [self.spec_axis] + [i for i in range(3)
                                            if i != self.spec_axis]
                m = np.moveaxis(m, order, [0, 1, 2])
            elif m.ndim == 2:
                if nd == 3:
                    m = np.broadcast_to(m[None, :, :], self.flux.shape)
                else:
                    m = np.moveaxis(m, self.spec_axis, 0)[:, self.row]
            if self._mask_mode() == "good_nonzero":
                self.mask = (np.asarray(m) == 0)
            else:
                self.mask = (np.asarray(m) != 0)
        else:
            self.mask = None
        # ★ 不再把 flux 的非有限值预先铺成整块 mask（GB 级 cube 会多占
        #   相等体积的 bool 数组 + 一次全盘扫描）；spectrum()/image() 里
        #   已经用 nanmean / mask|~isfinite 按需处理。

    # ------------------------------------------------------------------
    def _load_wave(self):
        ov = self.override
        mode = str(ov.get("wave_mode", "auto"))
        n = self.nwave

        # 0) BINTABLE 里的 WAVELENGTH 列（LAMOST）
        if self.is_table and self.wave_col and mode in ("auto", "hdu"):
            w = self._table_col_row(self.wave_col)
            if w is not None and w.size == n:
                mx = float(np.nanmax(w)) if np.isfinite(w).any() else 0.0
                scale = 10.0 if 0 < mx < 100 else (1e4 if 0 < mx < 10 else 1.0)
                self.wave = np.asarray(w, float) * scale
                self.wave_source = (f"BINTABLE 列 {self.wave_col}"
                                    + (f" (×{scale:g} -> A)" if scale != 1 else ""))
                return
            if mode == "hdu":
                raise ValueError(f"列 {self.wave_col} 长度与 flux 不匹配")

        # 1) 扩展里的波长
        if mode in ("auto", "hdu"):
            if ov.get("wave_hdu") is not None:
                cand = [int(ov["wave_hdu"])]
            else:
                cand = []
                for info in self.hdus:
                    if info["shape"] is None:
                        continue
                    if _name_has(info["name"], _WAVE_NAMES):
                        cand.append(info["index"])
            for i in cand:
                got = _wave_from_hdu(self.hdul[i], n)
                if got is not None:
                    self.wave, self.wave_source = got
                    self.wave_hdu = i
                    return
            if mode == "hdu":
                raise ValueError("指定的 HDU 不包含长度匹配的一维波长")

        # 2) 头卡 WCS（频谱：轴1；cube：自动找波长轴）
        if mode in ("auto", "wcs"):
            hdr = self.hdul[self.flux_hdu].header
            axes = []
            if self.kind == "cube":
                nd = self.hdul[self.flux_hdu].data.ndim
                for nap in range(nd):
                    axes.append(nd - nap)  # FITS axis numbers
                # 谱轴优先
                if self.spec_axis is not None:
                    axes = [nd - self.spec_axis] + [a for a in axes
                                                    if a != nd - self.spec_axis]
            else:
                axes = [1]
            for a in axes:
                got = _axis_wave(hdr, a, n)
                if got is not None and np.all(np.isfinite(got[0])) \
                        and np.any(got[0] > 0):
                    self.wave, self.wave_source = got
                    return
            # SDSS COEFF0/COEFF1 可能在 HDU0 而 flux 在 HDU1
            if self.flux_hdu != 0:
                got = _axis_wave(self.hdul[0].header, 1, n)
                if got is not None:
                    self.wave, self.wave_source = got
                    return
            if mode == "wcs":
                raise ValueError("头卡里没有可用的波长 WCS")

        # 3) 用户自定义 start/step
        if mode == "custom" or ov.get("wave_start") is not None:
            start = float(ov.get("wave_start"))
            step = float(ov.get("wave_step", 1.0))
            x = start + step * np.arange(n)
            if bool(ov.get("wave_log", False)):
                x = 10.0 ** x
            self.wave = x
            self.wave_source = "custom start/step"
            return

        raise ValueError(
            "无法自动识别波长（未找到 WAVE 扩展或 WCS 头卡）。"
            "请在界面选择波长来源或手动输入起始波长/步长。")

    # ------------------------------------------------------------------
    def build(self):
        self._inspect()
        # 自动判断 kind
        ovkind = self.override.get("kind")
        if ovkind in ("spectrum", "cube"):
            self.kind = ovkind
        else:
            has3 = any(i["shape"] and len(i["shape"]) == 3
                       and min(i["shape"]) > 1 for i in self.hdus)
            has1 = any(i["shape"] and len(i["shape"]) in (1, 2)
                       and max(i["shape"]) >= 32 for i in self.hdus)
            self.kind = "cube" if has3 else ("spectrum" if has1 else "spectrum")

        self.flux_hdu = self._pick_flux()
        self.is_table = self._is_table_hdu(self.flux_hdu)
        if self.is_table:
            self.kind = "spectrum"
            self._pick_table_columns()
        else:
            self.var_hdu = self._pick_companion("var")
            self.mask_hdu = self._pick_companion("mask")
        self._layout()
        self._load_companions()
        self._load_wave()

        # 统一 dtype：已经是浮点时保持 memmap 视图（>f8 也不转成原
        # 生字节序，否则要对整个 cube 做一次 GB 级拷贝）
        if self.flux is not None and not np.issubdtype(
                np.asarray(self.flux).dtype, np.floating):
            self.flux = np.asarray(self.flux, dtype=float)
        if self.var is not None:
            self.var = np.asarray(self.var, dtype=float)
            self.var[~np.isfinite(self.var)] = np.nan
            self.var[self.var <= 0] = np.nan
        if self.mask is not None:
            self.mask = np.asarray(self.mask, dtype=bool)

        # 波长单调递增检查
        w = np.asarray(self.wave, float)
        if w.size > 2 and np.nanmedian(np.diff(w)) < 0:
            # 倒序：翻转谱轴
            self.flux = np.flip(self.flux, axis=0)
            if self.var is not None:
                self.var = np.flip(self.var, axis=0)
            if self.mask is not None:
                self.mask = np.flip(self.mask, axis=0)
            self.wave = w[::-1]
            self.warnings.append("波长轴为递减，已翻转为递增")
            self.warning_codes.append({"code": "descwave", "params": {}})

        self.wave = np.asarray(self.wave, float)
        self.nwave = int(self.wave.size)

    # ------------------------------------------------------------------
    def meta(self):
        w = np.asarray(self.wave, float)
        dw = np.diff(w)
        log_like = bool(dw.size and np.all(dw > 0)
                        and np.nanmax(np.abs(dw / w[:-1] - np.median(dw / w[:-1])))
                        < 1e-6 and np.median(dw / w[:-1]) > 1e-6)
        return dict(
            path=self.path,
            basename=os.path.basename(self.path),
            kind=self.kind,
            objname=self.objname,
            instrument=self.instrument,
            bunit=self.bunit,
            shape=[int(self.nwave), int(self.ny), int(self.nx)],
            nwave=int(self.nwave), ny=int(self.ny), nx=int(self.nx),
            nrows=int(self.nrows), row=int(self.row),
            spec_axis=self.spec_axis,
            is_table=bool(self.is_table),
            wave=dict(min=float(np.nanmin(w)), max=float(np.nanmax(w)),
                      median_step=float(np.nanmedian(dw)) if dw.size else 0.0,
                      log=log_like, source=self.wave_source),
            roles=dict(flux=self.flux_hdu, var=self.var_hdu,
                       mask=self.mask_hdu, wave=self.wave_hdu,
                       flux_col=self.flux_col, var_col=self.var_col,
                       mask_col=self.mask_col, wave_col=self.wave_col,
                       var_kind=self._var_kind(),
                       mask_semantics=self._mask_mode()),
            hdus=self.hdus,
            sky=self.sky_wcs(),
            header={k: self.header0[k] for k in
                    ("OBJECT", "TELESCOP", "INSTRUME", "BUNIT", "NAXIS",
                     "CRVAL1", "CDELT1", "CRVAL3", "CDELT3", "CTYPE3") if k in self.header0},
            warnings=list(dict.fromkeys(self.warnings)),
            warning_codes=list(self.warning_codes),
        )

    # ------------------------------------------------------------------
    def spectrum(self, x=None, y=None, row=None, aperture=0, bin_id=None):
        """提取单条谱。

        cube: x,y 为像素坐标；aperture>0 时做圆形孔径求和。
        spectrum: 忽略 x,y；row 可选（二维多光谱 / BINTABLE 行）。
        """
        if self.is_table:
            r0 = int(row if row is not None else self.row)
            r0 = max(0, min(self.nrows - 1, r0))
            flux = np.asarray(self._table_col_row(self.flux_col, r0), float)
            var = None
            if self.var_col:
                v = np.asarray(self._table_col_row(self.var_col, r0), float)
                kind = self._var_kind()
                if kind == "ivar":
                    with np.errstate(divide="ignore", invalid="ignore"):
                        v = np.where(v > 0, 1.0 / v, np.nan)
                elif kind == "sigma":
                    v = v ** 2
                var = v
            if self.wave_col:
                w = np.asarray(self._table_col_row(self.wave_col, r0), float)
                mx = float(np.nanmax(w)) if np.isfinite(w).any() else 0.0
                scale = 10.0 if 0 < mx < 100 else (1e4 if 0 < mx < 10 else 1.0)
                wave = w * scale
            else:
                wave = np.asarray(self.wave, float)
            mask = ~np.isfinite(flux)
            if self.mask_col:
                m = np.asarray(self._table_col_row(self.mask_col, r0))
                mask = mask | ((m == 0) if self._mask_mode() == "good_nonzero"
                               else (m != 0))
            if var is not None:
                mask = mask | ~np.isfinite(var) | (var <= 0)
            return dict(wave=wave, flux=flux, var=var, mask=mask,
                        kind="spectrum", row=r0)
        if self.kind == "cube":
            x = int(x if x is not None else self.nx // 2)
            y = int(y if y is not None else self.ny // 2)
            if not (0 <= x < self.nx and 0 <= y < self.ny):
                raise IndexError(f"像素 ({x},{y}) 超出范围")
            r = float(aperture or 0)
            if bin_id is not None and int(bin_id) >= 0:
                # ---- PowerBin bin 叠加（代替孔径）----
                info = self._bins
                if info is None:
                    info = self.make_bins()
                bi = int(bin_id)
                if not (0 <= bi < int(info["nbin"])):
                    # 前端可能留着旧的分 bin 结果 → 夹到有效范围而不是直接报错
                    bi = min(max(bi, 0), int(info["nbin"]) - 1)
                idx = np.asarray(info["pix_flat"][bi], int)
                if idx.size == 0:
                    raise ValueError(f"bin {bi} 没有有效像素")
                ysb, xsb = np.divmod(idx, self.nx)
                x, y = int(round(xsb.mean())), int(round(ysb.mean()))
                flat = np.asarray(self.flux).reshape(self.nwave, -1)
                sub = np.asarray(flat[:, idx], float)
                if self.mask is not None:
                    msub = np.asarray(self.mask).reshape(self.nwave, -1)[:, idx]
                    sub = np.where(msub, np.nan, sub)
                else:
                    msub = ~np.isfinite(sub)
                flux = np.nansum(sub, axis=1)
                if self.var is not None:
                    vsub = np.asarray(self.var).reshape(self.nwave, -1)[:, idx]
                    vsub = np.where(msub, np.nan, np.asarray(vsub, float))
                    var = np.nansum(vsub, axis=1)
                    var[~np.isfinite(var) | (var <= 0)] = np.nan
                else:
                    var = None
                mask = np.all(msub, axis=1) | ~np.isfinite(flux)
                npix = int(idx.size)
                meta = dict(kind="cube", x=x, y=y, aperture=0.0,
                            bin=bi, npix=npix)
            elif r <= 0:
                flux = np.asarray(self.flux[:, y, x], float)
                var = (np.asarray(self.var[:, y, x], float)
                       if self.var is not None else None)
                mask = (np.asarray(self.mask[:, y, x], bool)
                        if self.mask is not None else np.zeros(self.nwave, bool))
                npix = 1
            else:
                yy, xx = np.mgrid[0:self.ny, 0:self.nx]
                sel = (xx - x) ** 2 + (yy - y) ** 2 <= r ** 2
                if not sel.any():
                    sel[y, x] = True
                sub = np.asarray(self.flux[:, sel], float)
                msub = (np.asarray(self.mask[:, sel], bool)
                        if self.mask is not None
                        else np.zeros(sub.shape, bool))
                sub = np.where(msub, np.nan, sub)
                flux = np.nansum(sub, axis=1)
                if self.var is not None:
                    vsub = np.asarray(self.var[:, sel], float)
                    vsub = np.where(msub, np.nan, vsub)
                    var = np.nansum(vsub, axis=1)
                    var[~np.isfinite(var) | (var <= 0)] = np.nan
                else:
                    var = None
                mask = np.all(msub, axis=1) | ~np.isfinite(flux)
                npix = int(sel.sum())
            if bin_id is None or int(bin_id) < 0:
                meta = dict(kind="cube", x=x, y=y, aperture=r, npix=npix)
        else:
            if self.nrows > 1:
                r0 = int(row if row is not None else self.row)
                r0 = max(0, min(self.nrows - 1, r0))
                d = np.asarray(self.hdul[self.flux_hdu].data)
                view = np.moveaxis(d, self.spec_axis, 0)
                flux = np.asarray(view[:, r0], float)
                var = None
                mask = ~np.isfinite(flux)
                if self.var_hdu is not None:
                    v = np.asarray(self.hdul[self.var_hdu].data)
                    vv = np.moveaxis(v, self.spec_axis, 0)[:, r0]
                    kind = self._var_kind()
                    if kind == "ivar":
                        vv = np.where(np.asarray(vv, float) > 0, 1 / vv, np.nan)
                    elif kind == "sigma":
                        vv = np.asarray(vv, float) ** 2
                    var = np.asarray(vv, float)
                if self.mask_hdu is not None:
                    m = np.asarray(self.hdul[self.mask_hdu].data)
                    mm = np.moveaxis(m, self.spec_axis, 0)[:, r0]
                    mask = ((np.asarray(mm) != 0)
                            if self._mask_mode() == "bad_nonzero"
                            else (np.asarray(mm) == 0))
                meta = dict(kind="spectrum", row=r0)
            else:
                flux = np.asarray(self.flux, float)
                var = (np.asarray(self.var, float)
                       if self.var is not None else None)
                mask = (np.asarray(self.mask, bool)
                        if self.mask is not None else np.zeros(self.nwave, bool))
                meta = dict(kind="spectrum", row=0)

        flux = np.asarray(flux, float)
        mask = np.asarray(mask, bool) | ~np.isfinite(flux)
        if var is not None:
            var = np.asarray(var, float)
            bad = ~np.isfinite(var) | (var <= 0)
            mask = mask | bad
        return dict(
            wave=np.asarray(self.wave, float),
            flux=flux,
            var=var,
            mask=mask,
            **meta,
        )

    # ---- cube 分 bin（PowerBin，见 binning.py / cube.ipynb）----
    _bins: Any = None
    _bins_key: Any = None
    _sky: Any = None

    def sky_wcs(self):
        """天体测量：canonical (x, y) → (RA, Dec) 的线性化映射。

        IFU 视场很小（几十角秒），用局部雅可比做线性近似已足够，
        前端可据此画 RA/Dec 网格与刻度。返回::

            dict(has_wcs=True, frame='RA---TAN',
                 crpix=[x0, y0],      # 参考像素（canonical 0-based）
                 crval=[ra0, dec0],   # 参考世界坐标（度）
                 mat=[[dra/dx, dra/dy], [ddec/dx, ddec/dy]])

        无 WCS / 非 cube → dict(has_wcs=False)。
        """
        if self._sky is not None:
            return self._sky
        self._sky = dict(has_wcs=False)
        try:
            nd = int(getattr(self, "nd_flux", 0) or 0)
            if self.kind != "cube" or nd != 3:
                return self._sky
            from astropy.wcs import WCS
            hdrs = [self.hdul[self.flux_hdu].header]
            if self.flux_hdu != 0:
                hdrs.append(self.hdul[0].header)   # 有些 cube 的 WCS 在 HDU0
            w = None
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                for hdr in hdrs:
                    try:
                        w_try = WCS(hdr, relax=True)
                    except Exception:
                        continue
                    if w_try.has_celestial:
                        w = w_try
                        break
            if w is None:
                return self._sky
            lng = int(w.wcs.lng)
            lat = int(w.wcs.lat)
            ndw = int(w.naxis)
            # canonical x/y → 0-based FITS 像素轴号
            fx = ndw - 1 - int(self.nx_np_axis)
            fy = ndw - 1 - int(self.ny_np_axis)

            def world(x, y):
                p = [0.0] * ndw
                p[fx] = float(x)
                p[fy] = float(y)
                out = w.pixel_to_world_values(*p)
                return float(out[lng]), float(out[lat])

            x0 = (self.nx - 1) / 2.0
            y0 = (self.ny - 1) / 2.0
            ra0, dec0 = world(x0, y0)
            rax, decx = world(x0 + 1.0, y0)
            ray, decy = world(x0, y0 + 1.0)
            drax = ((rax - ra0 + 180.0) % 360.0) - 180.0
            dray = ((ray - ra0 + 180.0) % 360.0) - 180.0
            self._sky = dict(
                has_wcs=True,
                frame=str(w.wcs.ctype[lng]),
                crpix=[float(x0), float(y0)],
                crval=[float(ra0), float(dec0)],
                mat=[[float(drax), float(dray)],
                     [float(decx - dec0), float(decy - dec0)]],
            )
        except Exception:
            self._sky = dict(has_wcs=False)
        return self._sky

    def make_bins(self, target_sn=10.0, band=None, max_bins=400):
        """对 cube 做 PowerBin 分 bin（结果缓存）。

        target_sn : bin 的目标 S/N（(ΣF)²/ΣV 的平方根）
        band      : 计算 S/N 用的波段 (Å)，None → 5300–5700
        max_bins  : bin 数上限（超出则自动提高 target_sn），便于下拉选择
        """
        if self.kind != "cube":
            raise ValueError("只对数据立方做分 bin")
        key = (round(float(target_sn), 4),
               None if band is None else (round(float(band[0]), 2),
                                          round(float(band[1]), 2)),
               int(max_bins))
        if self._bins is not None and self._bins_key == key:
            return self._bins
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            info = binning.compute_bins(self.flux, self.var, self.wave,
                                        target_sn=target_sn, band=band,
                                        max_bins=max_bins)
        info["max_bins"] = int(max_bins)
        self._bins = info
        self._bins_key = key
        return info

    def bins_meta(self, full_map=True):
        """给 JSON 友好的分 bin 信息（供 /api/bins）。"""
        info = self._bins
        if info is None:
            raise ValueError("尚未分 bin")
        out = dict(
            nbin=int(info["nbin"]),
            target_sn=float(info["target_sn"]),
            achieved_sn=float(info.get("achieved_sn", info["target_sn"])),
            npix=[int(v) for v in info["npix"]],
            sn=[float(v) for v in info["sn"]],
            band=[float(info["band"][0]), float(info["band"][1])],
            ny=int(self.ny), nx=int(self.nx),
        )
        if full_map:
            out["map"] = np.asarray(info["bin_map"], int).tolist()
        return out

    # ------------------------------------------------------------------
    def image(self, mode="white", wave_min=None, wave_max=None,
              max_size=360, snr_clip=(1, 99.5)):
        """生成 2D 预览数组（cube）。返回 dict(map, factor, extent, vmin, vmax)。"""
        if self.kind != "cube":
            raise ValueError("单光谱没有空间图像")
        w = self.wave
        lo = float(wave_min if wave_min is not None else np.nanmin(w))
        hi = float(wave_max if wave_max is not None else np.nanmax(w))
        band = (w >= lo) & (w <= hi)
        idx = np.flatnonzero(band)
        if idx.size == 0:
            raise ValueError("所选波段内没有波长点")
        if idx.size > 240:
            idx = idx[np.linspace(0, idx.size - 1, 240).astype(int)]
        sub = np.asarray(self.flux[idx], float)
        if self.mask is not None:
            sub = np.where(np.asarray(self.mask[idx], bool), np.nan, sub)
        with warnings.catch_warnings(), np.errstate(invalid="ignore"):
            warnings.simplefilter("ignore", RuntimeWarning)
            if mode == "snr":
                if self.var is None:
                    img = np.nanmean(sub, axis=0)
                else:
                    vsub = np.asarray(self.var[idx], float)
                    vsub = np.where(np.asarray(self.mask[idx], bool)
                                    if self.mask is not None else False,
                                    np.nan, vsub)
                    f = np.nanmean(sub, axis=0)
                    e = np.sqrt(np.nanmean(vsub, axis=0))
                    img = f / e
            else:
                img = np.nanmean(sub, axis=0)
        img = np.asarray(img, float)
        if not np.isfinite(img).any():
            img = np.zeros((self.ny, self.nx))
        # 下采样
        ny, nx = img.shape
        s = max(1, int(math.ceil(max(ny, nx) / float(max_size))))
        if s > 1:
            ny2, nx2 = (ny // s) * s, (nx // s) * s
            a = img[:ny2, :nx2]
            a = a.reshape(ny2 // s, s, nx2 // s, s)
            img_ds = np.nanmean(a, axis=(1, 3))
        else:
            img_ds = img
        finite = img_ds[np.isfinite(img_ds)]
        lo_p, hi_p = ((1.0, 99.5) if mode == "snr" else (2.0, 98.0))
        if finite.size:
            vmin, vmax = np.nanpercentile(finite, [lo_p, hi_p])
            if not np.isfinite(vmin) or not np.isfinite(vmax) or vmax <= vmin:
                vmin, vmax = float(np.nanmin(finite)), float(np.nanmax(finite))
            if mode != "snr":
                # 白光照：负的 sky 残差不占色阶，下界抬到 0
                vmin = max(0.0, float(vmin))
        else:
            vmin, vmax = 0.0, 1.0
        # ★ 必须把 NaN 转成 None：Python json 会输出非法 JSON 的 `NaN`，
        #   浏览器 JSON.parse 直接失败（表现为 HTTP 200 却解析错误）。
        # ★ 用 6 位有效数字（.6g），不能用 round(x, 6)：天文通量 ~1e-18，
        #   固定小数位会把整幅图打平成 0。
        arr = [[None if not np.isfinite(v) else float(f"{v:.6g}") for v in row]
               for row in img_ds]
        return dict(
            map=arr,
            factor=int(s),
            shape=[int(img_ds.shape[0]), int(img_ds.shape[1])],
            full_shape=[int(ny), int(nx)],
            extent=[0, int(nx), 0, int(ny)],
            vmin=float(vmin), vmax=float(vmax),
            mode=mode, wave_min=lo, wave_max=hi,
        )

    # ------------------------------------------------------------------
    def wave_stats(self):
        w = np.asarray(self.wave, float)
        if w.size < 2:
            return dict(min=float(w[0]), max=float(w[-1]), step=0.0)
        return dict(min=float(np.nanmin(w)), max=float(np.nanmax(w)),
                    step=float(np.nanmedian(np.diff(w))))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def inspect_file(path: str) -> dict:
    """只读文件结构，不尝试完整构建。

    自动识别失败时，前端用它拿到 HDU / BINTABLE 列清单，让用户手动指定。
    同时给出**建议的角色/列**（按 FLUX/IVAR/ORMASK/WAVELENGTH 等命名），
    用户只需点“应用覆盖”即可重试。
    """
    ds = Dataset(path=os.path.abspath(path), override={})
    ds.hdul = fits.open(memmap_path(path), memmap=True)
    try:
        ds._inspect()
        has3 = any(i["shape"] and len(i["shape"]) == 3
                   and min(i["shape"]) > 1 for i in ds.hdus)
        ds.kind = "cube" if has3 else "spectrum"
        roles = dict(flux=None, var=None, mask=None, wave=None,
                     flux_col=None, var_col=None, mask_col=None, wave_col=None,
                     var_kind="var", mask_semantics="bad_nonzero")
        nrows = 0
        # 1) BINTABLE 建议：LAMOST 等
        for info in ds.hdus:
            if info.get("type") != "BinTableHDU":
                continue
            cmap = {}
            for c in info.get("columns", []):
                rs = c.get("row_shape") or []
                if len(rs) == 1 and rs[0] >= 16:
                    cmap[str(c["name"]).upper()] = c["name"]
            f = (cmap.get("FLUX") or cmap.get("SCI") or cmap.get("DATA"))
            if not f:
                continue
            roles.update(flux=info["index"], flux_col=f)
            nrows = int(info.get("nrows", 0) or 0)
            v = (cmap.get("IVAR") or cmap.get("VAR") or cmap.get("VARIANCE")
                 or cmap.get("ERR") or cmap.get("ERROR") or cmap.get("SIGMA")
                 or cmap.get("NOISE"))
            m = (cmap.get("ORMASK") or cmap.get("ANDMASK") or cmap.get("MASK")
                 or cmap.get("DQ"))
            w = (cmap.get("WAVELENGTH") or cmap.get("WAVE")
                 or cmap.get("LAMBDA") or cmap.get("LOGWAVE"))
            if v:
                roles.update(var=info["index"], var_col=v)
                _vu = str(v).upper()
                roles["var_kind"] = ("ivar" if "IVAR" in _vu else
                                     ("sigma" if any(k in _vu for k in
                                      ("ERR", "SIGMA", "NOISE", "ERROR"))
                                      else "var"))
            if m:
                roles.update(mask=info["index"], mask_col=m)
            if w:
                roles.update(wave=info["index"], wave_col=w)
            break
        # 2) 普通 ImageHDU 建议
        if roles["flux"] is None:
            for info in ds.hdus:
                if info.get("type") == "BinTableHDU" or not info.get("shape"):
                    continue
                nm = str(info.get("name", ""))
                if _name_has(nm, _WAVE_NAMES + _VAR_NAMES + _MASK_NAMES):
                    continue
                if len(info["shape"]) in (1, 2) and max(info["shape"]) >= 16:
                    roles["flux"] = info["index"]
                    break
        hdr0 = ds.hdul[0].header
        header = {k: hdr0[k] for k in
                  ("OBJECT", "TELESCOP", "INSTRUME", "BUNIT", "NAXIS",
                   "CRVAL1", "CDELT1", "CRVAL3", "CDELT3", "CTYPE3")
                  if k in hdr0}
        return dict(
            path=ds.path, basename=os.path.basename(ds.path),
            kind=ds.kind, objname=ds.objname, instrument=ds.instrument,
            bunit=ds.bunit,
            shape=None, nwave=None, ny=None, nx=None,
            nrows=nrows, row=0, spec_axis=None, is_table=bool(
                roles["flux"] is not None and any(
                    i["index"] == roles["flux"]
                    and i.get("type") == "BinTableHDU" for i in ds.hdus)),
            wave=None, roles=roles, hdus=ds.hdus, header=header,
            warnings=["自动识别失败：已列出文件结构，请手动指定 HDU/列"],
            loadable=False,
        )
    finally:
        ds.close()


def open_dataset(path: str, override: Optional[dict] = None) -> Dataset:
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    ov = dict(override or {})
    ds = Dataset(path=os.path.abspath(path), override=ov)
    ds.hdul = fits.open(memmap_path(path), memmap=True)
    try:
        ds.build()
    except Exception:
        ds.close()
        raise
    return ds


def list_fits(dirpath: str, recursive=False, max_files=2000):
    """列出目录中的 FITS 文件（供网页端文件选择）。"""
    out = []
    dirpath = os.path.abspath(os.path.expanduser(dirpath or "."))
    if not os.path.isdir(dirpath):
        raise FileNotFoundError(dirpath)
    if recursive:
        walker = os.walk(dirpath)
        for root, dirs, files in walker:
            for fn in files:
                if fn.lower().endswith((".fits", ".fit", ".fts", ".fits.gz")):
                    p = os.path.join(root, fn)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    out.append(dict(path=p, name=fn, dir=root,
                                    size=int(st.st_size),
                                    mtime=float(st.st_mtime)))
                    if len(out) >= max_files:
                        return out
    else:
        for fn in sorted(os.listdir(dirpath)):
            p = os.path.join(dirpath, fn)
            if os.path.isfile(p) and fn.lower().endswith(
                    (".fits", ".fit", ".fts", ".fits.gz")):
                st = os.stat(p)
                out.append(dict(path=p, name=fn, dir=dirpath,
                                size=int(st.st_size),
                                mtime=float(st.st_mtime)))
                if len(out) >= max_files:
                    break
    return out
