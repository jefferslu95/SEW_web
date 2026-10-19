# 浏览器端版本（Pyodide / WebAssembly）— 完整版

**全部计算在访问者的浏览器里完成**，服务器只提供静态文件（GitHub Pages / 任意 CDN 都行）。
不需要安装任何 Python；数据不上传网络。

## 已验证（Chrome，本地静态服务）

```
python 3.13.2 | numpy 2.2.5 | scipy 1.14.1 | astropy 7.0.1
ppxf 9.5.0 | ppxf-sew 0.8.2 | powerbin 1.1.12

打开示例谱 spectrum.fit → 选模板 emiles → 开始拟合
  z        : 0.047403
  velscale : 43.4 km/s (dlam=6.28e-5)
  chi2/dof : 1.272  (dof=6090)
  S/N      : 31.05
  耗时     : 3.8 s             ← 纯浏览器 CPU
  <logAge> : 9.017  [M/H] = -0.363
结果自动存入工作区，刷新页面后仍可重新打开 / 导出 JSON、FITS
```

冷启动（全部本地文件）≈15 s，之后走 HTTP 缓存。

## 目录

```
webapp/
├── index.html          ← 由 static/index.html 生成（改路径 + 注入 rpc.js）
├── app.js / style.css / i18n.js / i18n/    ← 由 static/ 复制
├── rpc.js              ★ 浏览器端桥接层：把 fetch('/api/...') 路由到 worker
│                          并实现「工作区」（IndexedDB）
├── worker.js           ★ Web Worker：加载 Pyodide、装包、调用 Python
├── py/backend/
│   ├── wasm_app.py     ★ 浏览器端「后端」：把 Flask 路由搬成纯函数
│   └── fits_io.py / fitting.py / templates.py / binning.py（构建时复制）
├── vendor/             ★ 不入 git：Pyodide 运行时 + 纯 Python 轮子
├── data/               ★ 构建产物：line_use.dat、samples/spectrum.fit
└── templates/          manifest.json（入库）；*.npz 不入库
```

## 构建 / 本地运行

```bash
python scripts/fetch_pyodide.py        # 一次性下载 Pyodide + 轮子（≈57 MB）
python scripts/build_webapp.py         # 组装静态站点（复制 backend + static）
cp <模板>.npz webapp/templates/         # 可选：放入模板（也可页面里填 URL 下载）
python -m http.server 8790 --directory webapp
# 打开 http://127.0.0.1:8790/
```

> 必须通过 HTTP 打开（`file://` 下 Worker/Pyodide 不可用）。

## 工作区（模板 / 数据 / 结果）

- 全部保存在浏览器 **IndexedDB**（`sew-workspace`），刷新/重开浏览器仍在。
- **模板**：可从 URL 下载（需 CORS）、上传 `.npz`、或拖拽文件到页面；同名模板自动出现在
  「拟合设置 → 模板」下拉框。
- **数据**：拖拽或「上传」的 FITS 会进入工作区，列表里可直接「打开」。
- **拟合结果**：每次拟合自动保存（摘要 + PNG + 完整 JSON），可重新打开查看、导出 JSON / FITS。
- 页面上没有「服务器路径」概念：`浏览` 列的是工作区里的文件。

## 与服务器版的关系

同一套算法代码：`scripts/build_webapp.py` 把 `backend/*.py` 复制到 `webapp/py/backend/`，
`wasm_app.py` 复刻 `app.py` 的路由（`request/jsonify/send_file` → 普通 dict/返回值）。
服务器版继续可用（`python run.py`），浏览器版是完全独立的静态站点。

## 已知限制

- **单线程**：GitHub Pages 无法设置 `COOP/COEP`，Pyodide 线程版不可用；要并行 BLAS 需
  Cloudflare Pages / Netlify（可自定义响应头）或自建。
- **首屏体积**：Pyodide + numpy/scipy/matplotlib/astropy ≈ 57 MB，模板 13–90 MB 另算。
- **内存**：WASM 上限约 2–4 GB；13223×45×43 的 cube 可用，更大的数据建议用服务器版。
- 冷启动会有一条 `astropy_iers_data` 的 fetch 警告（Pyodide 0.29 分发中缺少该包，
  已改用 PyPI 轮子安装），不影响 FITS 读取与拟合。
