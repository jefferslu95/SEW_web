# 浏览器端版本（Pyodide / WebAssembly）— 实验

**目标**：用访问者自己的 CPU 做全部计算，服务器只提供静态文件。
数据不进网络、不需要装 Python，打开网页就能用。

## 现状：核心链路已验证可用 ✅

`webapp/index.html` 是一个最小验证页（PoC），实测（Chrome / 单线程）：

```
python 3.13.2 | numpy 2.2.5 | scipy 1.14.1 | astropy 7.0.1
ppxf 9.5.0 | ppxf-sew 0.8.2 | powerbin 1.1.12
→ 拟合 spectrum.fit（183 KB）+ spectra_emiles_9.0.npz（12.8 MB）
  z        : 0.047403
  velscale : 43.4 km/s (dlam=6.28e-5)
  chi2/dof : 1.272   (dof=6090)
  S/N      : 31.05
  time     : 3.6 s          ← 纯浏览器内耗时
  <logAge> : 9.017  [M/H] = -0.363
```

初始化（冷启动、全部本地文件）：**≈15 s**；拟合：**3–4 s**。

## 目录

```
webapp/
├── index.html          验证页 UI
├── poc.js              主线程：文件选择 + 与 worker 的 RPC
├── worker.js           Web Worker：加载 Pyodide、装包、调用 Python
├── py/backend/         后端算法模块（与服务器版同一套代码）
│   ├── api.py          ★ 浏览器端入口：sew_fit(fits, tpl, cfg) -> JSON
│   ├── fits_io.py      （由 scripts/build_webapp.py 复制）
│   ├── fitting.py
│   ├── templates.py
│   └── binning.py
├── vendor/             ★ 不入 git，用 scripts/fetch_pyodide.py 下载
│   ├── pyodide/        pyodide.asm.wasm + numpy/scipy/matplotlib/astropy 轮子
│   └── wheels/         ppxf / ppxf-sew / plotbin / capfit / powerbin 纯 Python 轮子
├── templates/          ★ 不入 git（模板 npz 13–90 MB，按需下载）
└── samples/            小样例 FITS
```

## 本地跑起来

```bash
python scripts/fetch_pyodide.py        # 一次性下载 Pyodide + 轮子（约 57 MB）
python scripts/build_webapp.py         # 同步 backend/*.py 到 webapp/py/backend
cp <某个模板>.npz webapp/templates/     # 例如 spectra_emiles_9.0.npz (13 MB)
python -m http.server 8790 --directory webapp
# 浏览器打开 http://127.0.0.1:8790/
```

> 必须通过 HTTP 打开，`file://` 下 Worker 无法加载。

## 部署到 GitHub Pages

1. `python scripts/fetch_pyodide.py` 与 `build_webapp.py` 生成 `webapp/` 内容；
2. 把 `webapp/` 作为 Pages 根目录（或推到 `gh-pages` 分支）；
3. 模板 npz 放到 `webapp/templates/`（或改用外链，页面里自行 fetch）。

纯静态托管即可，**不需要服务器进程**；第一次访问下载 ~70 MB，之后走 HTTP 缓存。

## 与服务器版的分工

| | 浏览器端（本目录） | 服务器端（`backend/` + Flask） |
|---|---|---|
| 算力 | 访问者本机 CPU | 服务器 CPU |
| 安装 | 无 | 需 Python / Docker |
| 数据 | 永不出本机 | 需上传或挂载 |
| 首屏 | ~70 MB 下载 | 秒开 |
| 大 cube（100 MB+） | 受 WASM 内存限制，较吃紧 | 无压力 |
| 多用户 | 天然水平扩展（每人用自己的机器） | 需并发控制 |

## 已知限制 / 待办

- **单线程**：GitHub Pages 无法设置 `COOP/COEP` 响应头，Pyodide 的线程版不可用；
  若要并行 BLAS，需要 Cloudflare Pages / Netlify（可自定义 headers）或自建。
- **模板体积**：`ppxf_sew` 的 PyPI wheel 不含 `sps_models`，需单独托管
  （emiles 13 MB / fsps 15 MB / galaxev 20 MB / xsl 90 MB）。
- **完整 UI 尚未迁移**：目前 PoC 只验证「读 FITS → 拟合 → 出图」主链路。
  迁移现有界面需要把 `app.js` 里的 `api()/postJSON()` 换成对 worker 的 RPC，
  「浏览服务器目录」改成浏览器本地文件选择，导出改为 Blob 下载。
- 冷启动有 `astropy_iers_data` 的 fetch 警告（Pyodide 0.29 分发里没有该包，
  已改用 PyPI 轮子安装），不影响 FITS / 拟合。
