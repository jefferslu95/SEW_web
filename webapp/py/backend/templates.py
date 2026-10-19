# -*- coding: utf-8 -*-
"""星族模板库管理。

优先级（同名模板取最先找到者）：
  1. SEW_web/data/templates        （用户上传/下载）
  2. $SEW_TEMPLATE_DIR
  3. ~/.ppxf_sps_models
  4. ppxf_sew/example/sps_models   （pip 安装自带的 emiles/fsps/galaxev/xsl）

模板格式：npz，至少包含 keys:
    templates (nwave, nage, nmetal), lam (nwave,), ages, metals, fwhm
"""
from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import urllib.request
from typing import Optional

import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
TPL_LOCAL = os.path.join(DATA_DIR, "templates")
CONFIG_PATH = os.path.join(BASE_DIR, "templates.json")

# 运行时目录（服务器工作区 <root>/templates）：优先级最高，可随工作区切换。
RUNTIME_DIRS: list = []


def set_runtime_dir(path: Optional[str]) -> None:
    """设置服务器工作区的模板目录（None / '' 表示不启用）。"""
    RUNTIME_DIRS.clear()
    if path:
        os.makedirs(path, exist_ok=True)
        RUNTIME_DIRS.append(os.path.abspath(os.path.expanduser(path)))


def tpl_local() -> str:
    """模板写入目录：服务器工作区优先，否则回到 data/templates。"""
    return RUNTIME_DIRS[0] if RUNTIME_DIRS else TPL_LOCAL


def _ppxf_sew_dir() -> Optional[str]:
    try:
        from importlib import resources
        p = resources.files("ppxf_sew") / "example" / "sps_models"
        return str(p)
    except Exception:
        return None


def search_dirs():
    dirs = list(RUNTIME_DIRS) + [TPL_LOCAL]
    env = os.environ.get("SEW_TEMPLATE_DIR")
    if env:
        dirs.append(os.path.expanduser(env))
    dirs.append(os.path.expanduser("~/.ppxf_sps_models"))
    p = _ppxf_sew_dir()
    if p:
        dirs.append(p)
    return [d for d in dirs if d and os.path.isdir(d)]


def _npz_keys_ok(path: str) -> bool:
    try:
        z = np.load(path, allow_pickle=False)
        need = {"templates", "lam", "ages", "metals", "fwhm"}
        return need.issubset(set(z.keys()))
    except Exception:
        return False


def _short_name(filename: str) -> str:
    base = os.path.basename(filename)
    if base.startswith("spectra_") and base.endswith(".npz"):
        mid = base[len("spectra_"):-4]
        # spectra_emiles_9.0 -> emiles
        parts = mid.split("_")
        if len(parts) >= 2 and parts[-1].replace(".", "").isdigit():
            return "_".join(parts[:-1])
        return mid
    return os.path.splitext(base)[0]


def list_templates():
    seen = {}
    for d in search_dirs():
        try:
            fns = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in fns:
            if not fn.lower().endswith(".npz"):
                continue
            p = os.path.join(d, fn)
            if not _npz_keys_ok(p):
                continue
            name = _short_name(fn)
            if name in seen:
                continue
            try:
                st = os.stat(p)
                z = np.load(p, allow_pickle=False)
                shape = list(z["templates"].shape)
                try:
                    ages = float(np.ptp(np.log10(np.asarray(z["ages"], float))))
                except Exception:
                    ages = None
                # 模板自己的波长采样（典型 Δlnλ，dex）与波长范围
                # 只统计光学常用波段 3500-9000 Å（线性 λ 网格下 Δλ/λ 随 λ 变化）
                try:
                    lam_t = np.asarray(z["lam"], float)
                    lam_t = lam_t[np.isfinite(lam_t) & (lam_t > 0)]
                    dlam = None
                    if lam_t.size > 2:
                        m = (lam_t[:-1] >= 3500) & (lam_t[:-1] <= 9000)
                        if not m.any():
                            m = np.ones(lam_t.size - 1, bool)
                        # Δλ/λ 是 Δlnλ，换成 Δlog10λ（dex）
                        dlam = float(np.median(np.diff(lam_t)[m] / lam_t[:-1][m])
                                     / math.log(10.0))
                    lam_range = [float(lam_t[0]), float(lam_t[-1])]
                except Exception:
                    dlam, lam_range = None, None
            except Exception:
                continue
            seen[name] = dict(
                name=name,
                label=name.upper() if name.lower() in ("emiles", "fsps", "xsl")
                else name.replace("_", " ").title(),
                filename=fn, path=p, dir=d,
                size=int(st.st_size),
                shape=shape, dlam=dlam, lam_range=lam_range,
                n_age=int(shape[1]) if len(shape) == 3 else None,
                n_metal=int(shape[2]) if len(shape) == 3 else None,
                age_span=ages,
                builtin=(d == _ppxf_sew_dir()),
            )
    return sorted(seen.values(), key=lambda x: (not x["builtin"], x["name"]))


def find_template(name: str) -> Optional[dict]:
    name = str(name or "").strip()
    for t in list_templates():
        if t["name"] == name or t["filename"] == name:
            return t
    # 允许直接传绝对路径
    if os.path.isfile(name) and _npz_keys_ok(name):
        return dict(name=_short_name(name), label=name, filename=os.path.basename(name),
                    path=name, dir=os.path.dirname(name), size=os.path.getsize(name),
                    shape=list(np.load(name)["templates"].shape), builtin=False)
    return None


def load_registry():
    if not os.path.isfile(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def download_template(name: str, url: str, timeout=60, progress=None):
    """下载模板到工作区 templates/（默认源见 webapp/templates/manifest.json）。

    ``timeout`` 是「单次读取超时」：只要还有数据在流动就不会触发；
    这里用 ``read1`` 小步读取，保证进度回调能及时更新（大文件/慢链路也适用）。
    """
    if not url or not str(url).lower().startswith(("http://", "https://")):
        raise ValueError("请提供 http(s) 模板下载地址")
    local = tpl_local()
    os.makedirs(local, exist_ok=True)
    fn = os.path.basename(url.split("?")[0]) or f"spectra_{name}_9.0.npz"
    if not fn.lower().endswith(".npz"):
        fn = f"spectra_{name}_9.0.npz"
    dst = os.path.join(local, fn)
    tmp = dst + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "SEW-web/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, \
                open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read1(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if not _npz_keys_ok(tmp):
            raise ValueError("下载的文件不是有效的 ppxf 模板 npz（缺少 "
                             "templates/lam/ages/metals/fwhm）")
        os.replace(tmp, dst)
    except Exception:
        # 失败时不要留下半截 .part 文件
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return dst


def save_uploaded_template(file_storage, name: Optional[str] = None):
    local = tpl_local()
    os.makedirs(local, exist_ok=True)
    fn = getattr(file_storage, "filename", "") or "template.npz"
    if not fn.lower().endswith(".npz"):
        raise ValueError("模板必须是 .npz 文件")
    if name:
        fn = f"spectra_{name}_9.0.npz"
    dst = os.path.join(local, os.path.basename(fn))
    tmp = dst + ".part"
    file_storage.save(tmp)
    if not _npz_keys_ok(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ValueError("上传文件不是有效的 ppxf 模板 npz")
    os.replace(tmp, dst)
    return dst
