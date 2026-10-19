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

## 服务器工作区模式（SSH 转发 / 团队共用同一台机器）

**算力归属规则**：工作区在哪里，就用那里的算力。

| 工作区 | 算力 | 打开 FITS |
|---|---|---|
| 服务器（`run.py --root ...`） | **服务器**（Flask 直接读文件跑 ppxf） | 零下载，只把路径发给后端 |
| 服务器静态托管（`server/workspace_server.py`） | 浏览器（Pyodide） | 按需流式下载到内存 FS |
| 本地目录 / 浏览器存储 | 浏览器（Pyodide） | 本地文件 |

前端通过 `/api/ws/info` 的 `compute` 字段判定：`compute=server` 时**完全不启动 Pyodide**
（不下载 57 MB 运行时），所有 `/api/*` 直接打后端。

### 推荐：`run.py`（服务器算力 + 服务器文件）

```bash
# 服务器上
python run.py --root ~/sew-ws --port 8791 \
    --allow-root ~ --allow-root /data
# 本地
ssh -N -L 8791:127.0.0.1:8791 user@server
# 打开 http://127.0.0.1:8791/
```

- `--root` 初始工作区；`--allow-root`（可多次）限定可切换/可浏览的目录范围，
  不传时默认 `[--root, $HOME]`；`--token` 可给 `/api/ws/*` 加 `X-SEW-Token` 校验。
- 模板、FITS、结果都写在服务器的 `<root>/templates|results|data/`；
  模板库（`/api/templates`）也优先读 `<root>/templates`。
- **`浏览` 列的是服务器端目录**（`/api/ws/browse`，限制在 `--allow-root` 内），
  选中 FITS 后只把**路径**发给后端，不传输文件内容。
- **切换工作区**：点左上角 **SEWweb 图标** → 「工作区设置」面板 → 填服务器目录 → 应用
  （POST `/api/ws/root`，会做 `--allow-root` 校验并自动 `mkdir`）。同一面板也能切回
  本地目录 / OPFS / 仅浏览器存储（切回去就用浏览器算力）。
- 接口：`GET /api/ws/info|browse|file|list|get`，`POST /api/ws/root|put`，`DELETE /api/ws/delete`。

### 仅存储：`server/workspace_server.py`（纯标准库）

只想托管静态站点、算力仍然留在浏览器时用这个：

```bash
python server/workspace_server.py --root ~/sew-ws --port 8790 --allow-root ~
```

它对 `/api/ws/*` 提供同样能力，但 `compute=browser`，前端会照常启动 Pyodide，
打开服务器 FITS 时先流式下载到内存文件系统。

## 工作区（首次进入会引导设置）

首次打开会弹出「设置工作区」：选一个本地文件夹（File System Access API）作为工作区，
结构如下：

```
<工作区>/
├── workspace.json    说明文件
├── templates/        星族模板 npz
├── results/          拟合结果 <时间>_<模板>.json / .png
├── data/             导入的 FITS
└── cache/vendor/     运行依赖副本（Pyodide + 轮子）
```

- 目录句柄存在 IndexedDB，下次打开自动恢复（浏览器只需再确认一次权限）。
- **运行依赖不会重复下载**：`sw.js`（Service Worker）把 `vendor/**` 缓存在 CacheStorage；
  首次引导时还会把依赖镜像到 `<工作区>/cache/vendor/`，即使浏览器缓存被清也能从目录恢复。
  实测：第二次打开 vendor 请求 100% 命中缓存，网络传输 0 MB（启动 ~9 s 主要是 WASM 初始化）。
- **拟合结果默认写入 `<工作区>/results/`**（同时保留 IndexedDB 索引供页面列出）。
- 模板从 `<工作区>/templates/` 读取；手动拷进去的 npz 也会自动导入。
- 不支持目录访问的浏览器（Firefox/Safari）退化为「仅浏览器存储」（IndexedDB），功能不变，
  但清站点数据会丢。
- 工作区面板里有「把运行依赖保存到工作区」按钮，可随时重新镜像。
- 本地目录模式下页面上没有「服务器路径」概念；只有在 SSH 转发 + `server/workspace_server.py`
  的「服务器工作区」模式下，`浏览` 才会列出服务器目录。

## 旧版说明（IndexedDB 工作区）

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
