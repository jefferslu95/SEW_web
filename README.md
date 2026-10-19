# SEWweb — 网页端的 FITS 光谱 / 数据立方查看与拟合

**打开网页 → 拖入 FITS → 看图、点像素 → pPXF 拟合 → 导出。**

👉 **<https://jefferslu95.github.io/SEW_web/>**

不需要安装任何东西：算力用你浏览器里的 WebAssembly（Pyodide），数据不出本机。
做大 cube 或想用服务器 CPU 时，再让页面连到一台算力服务器 —— 见[第 3 节](#3-用服务器的算力可选)。

---

## 1. 三十秒上手（纯浏览器）

1. 打开 <https://jefferslu95.github.io/SEW_web/>（推荐 Chrome / Edge）
2. 首次会让你**设置工作区**，随便选一个：
   - **选择工作目录…** —— 挑一个本机文件夹（模板、FITS、结果都存这里，可跨浏览器复用）
   - **浏览器私有目录** —— 不弹系统对话框，存在浏览器沙箱里（最省事）
   - **仅用浏览器存储** —— 存在 IndexedDB（清站点数据会丢）
3. 把 FITS 拖进页面（或用“从本机选择文件…”/“上传”）→ 自动识别 → 看图/取谱
4. 右侧「拟合设置」挑模板、设波段 → 点「拟合」
5. 底部结果面板看 χ²/dof、「ppxf plot()」、模板权重、逐线参数 → 导出 JSON / FITS

> 首次打开会下载约 60 MB 运行时，之后就缓存在本地，第二次几乎不下载。

**打开文件的 5 种方式**：拖拽 / 上传 / 从本机选择文件 / 工作区列表 / 服务器路径（连服务器时）。

---

## 2. 界面速览

```
┌───────────────────────────────────────────────┬──────────────────┐
│ 中栏（三个标签页）                              │ 右栏：拟合设置      │
│   [图像]  白光照 / S/N，点像素取谱（可选孔径）   │   模板 / 波长系     │
│   [光谱]  Plotly 光照曲线；拟合后叠加            │   FWHM / 红移      │
│           flux·bestfit·stellar·gas 与误差带     │   波段 / moments   │
│   [结果]  摘要 · ppxf plot() · 模板权重 ·        │   发射线 / 噪声     │
│           参数·气体线 · 日志 · 导出              │   正则化（通高）    │
├───────────────────────────────────────────────┤                  │
│ ═══ 可拖拽分隔条（调上/下区高度）═══            │                  │
├──────────────┬──────────────┬─────────────────┤                  │
│ 数据集        │ 显示选项      │ 工作区            │                  │
│ 文件信息      │ 图像模式、波段 │ 模板库、结果、     │                  │
│ 识别/手动覆盖  │ 孔径、bin     │ 从工作区打开       │                  │
└──────────────┴──────────────┴─────────────────┴──────────────────┘
```

**中栏（三个标签页）**
- **图像** — cube 的白光照 / S/N 图，可调波段与色标；**点图像任意像素**取该点光谱（可设圆形孔径）；单光谱则直接进「光谱」
- **光谱** — Plotly 交互图：缩放、悬停读数；可切观测系/静止系、显示 mask 坏点与 1σ 误差带；拟合完成后自动切到这一页并叠加 `flux / bestfit / stellar / gas` 四条曲线
- **结果** — 拟合摘要 + 子标签：
  - 「ppxf plot()」：直接调用 pPXF 自带 `p.plot()` 出的 PNG
  - 「模板权重」：模板类 `temp.plot(weights)` 的 age–metal 权重图
  - 「参数 / 气体线」：逐成分 V/σ/h3/h4；逐线 flux ± error、EW、V、σ
  - 「日志」：红移方法、模板信息、覆盖范围
  - 摘要含 χ²/dof、dof、RMS、S/N、红移、通量标度、耗时；可导出 JSON / FITS（含数组与 GOOD 掩膜）

**底部三张卡**（中栏下方，与中栏之间是可拖拽分隔条）
- **数据集** — 文件信息、「识别结果 / 手动覆盖」（HDU、BINTABLE 列、谱轴、行号、波长来源，识别不准时在这里改）
- **显示选项** — 图像模式（白光照 / S/N）、拉伸、波段与 coadd、孔径、显示 bin、刷新
- **工作区** — 模板库（下载/上传 npz）、拟合结果列表、从工作区打开文件

**右栏「拟合设置」**（通高，可滚动）

| 参数 | 说明 |
|---|---|
| 模板 | emiles / fsps / galaxev / xsl，或自己上传的 npz |
| 模板波长系 | air / vacuum（按模板实际选择） |
| 观测谱波长系 | air / vacuum（默认 **vacuum**） |
| FWHM | `λ/R` 或常量 Å，打开文件时自动推断，可手改 |
| 红移 z | `auto`（模板+发射线互相关）或手动 |
| 拟合波段 | 观测系 Å，默认全谱 |
| 速度网格 dlam | 默认 **跟随观测谱采样**（`obs`），也可选模板采样或自定义 |
| moments | 2 = (V, σ)，4 = (+h3, h4) |
| degree | 多项式阶数 |
| 发射线 | 19 条模板（Hδ…Hα、[OII]/[OIII]/[NII]/[SII]…），可 tie Balmer / limit doublets |
| 噪声 | 用文件误差，或按相邻像素 MAD 自动估计 |
| 正则化 | pPXF `regul`，用模板 (age, metal) 网格作 `reg_dim` |

---

## 3. 用服务器的算力（可选）

适用于：大 cube、服务器上有数据、想跑得快。**部署本身不需要你懂 Python / Docker**：
页面会生成一条一键脚本，在服务器上跑那一次就装好了；你要做的只是把命令复制过去、
并保持一条 SSH 隧道开着。

点左上角 **SEWweb** → 设置面板 →「连接算力服务器（部署向导）」：

| 步骤 | 你要做的 | 页面自动完成的 |
|---|---|---|
| ① 服务器登录 | 填 `user@host`、端口（默认 8899） | 生成部署命令 |
| ② 部署区 | 填代码目录 `~/SEW_web`、工作区 `~/sew-ws` | 生成隧道命令 |
| ③ 完成部署 | 复制下面两条命令执行 → 点「连接并使用服务器」 | 检测接口、切换算力、记住设置 |

```bash
# ① 在服务器上跑一次（页面里可一键复制；Docker 优先，没有 Docker 自动回退 Python）
curl -fsSL https://jefferslu95.github.io/SEW_web/tools/install_remote.sh \
  | bash -s -- --host 127.0.0.1 --port 8899 --dir ~/SEW_web --root ~/sew-ws

# ② 在你自己电脑上跑（Windows 自带 OpenSSH），保持不关
ssh -N -L 8899:127.0.0.1:8899 user@host
```

之后**地址栏仍然是 GitHub Pages**，只是页面通过 `http://127.0.0.1:8899` 与服务器的
`run.py` 通信；状态栏会显示 `算力：服务器 · 工作区：/path/on/server`。

- 服务器上的 `run.py` 只监听回环地址，配合 SSH 隧道使用，**不对公网暴露**
- 想换工作区：设置 → 输入路径或点「浏览…」（列的是**服务器**目录树）
- 想切回本机算力：设置 → **「切回本机（浏览器算力）」**

> 服务器端不需要 Python 环境：有 Docker 就用镜像（内置全部依赖），没有则用已有的
> anaconda/venv 解释器。详细参数与排障见 [docs/BRIDGE.md](docs/BRIDGE.md)。

---

## 4. 支持的输入

- **一维单光谱**：flux 在 HDU0 或任意扩展；波长来自 `WAVE/LAMBDA/LOGWAVE` 扩展、
  头卡 `CRVAL1/CDELT1/CRPIX1`、SDSS `COEFF0/COEFF1`，或手动 start/step
- **BINTABLE 光谱**（如 LAMOST `COADD`）：`FLUX / IVAR / WAVELENGTH / ORMASK` 按列指定，可指定行号
- **二维多光谱**：每行一条谱，可指定行号
- **三维数据立方**：谱轴可在 numpy 轴 0/1/2，内部统一为 `(wave, y, x)`
- **波长类型**：线性 Å、`log10(λ)`（MUSE/WiFeS 常见）、频率轴、CUNIT 换算（nm/μm/m）
- **误差**：`VAR/VARIANCE`、`ERR/ERROR/SIGMA/NOISE`、`IVAR`（自动换算）
- **Mask**：`MASK/DQ/BAD/FLAGS`，`非零=坏` / `非零=好` 可切换
- 扩展名缺失时按“形状相同 + 非负 + 量级远小”推断 variance，并在界面提示

**模板**：`ppxf_sew` 自带 emiles / fsps / galaxev / xsl 四套；也可在「模板库」里
从 URL 下载或上传 `.npz`（需含 `templates, lam, ages, metals, fwhm`）。

---

## 5. 常见问题

**「浏览」只能看到工作区？**
静态网页只能访问你授权过的目录（浏览器安全模型，与操作系统无关）。要看别处，用
「从本机选择文件…」（系统文件对话框，任意路径）；要目录树，连服务器。

**点「连接并使用服务器」没反应 / 连不上？**
按顺序查：① 服务器上 `run.py` 是否在跑（`curl 127.0.0.1:8899/api/ws/info`）；
② 本机 SSH 隧道是否开着；③ 服务器启动时 `--cors` 是否包含 `https://jefferslu95.github.io`。
（页面会自动重试 3 次，避免隧道刚建立时的瞬断。）

**「把运行依赖保存到工作区」失败：permission / not allowed？**
浏览器没给该目录写权限。重新「选择工作目录…」并允许写入，或改用「浏览器私有目录」
（OPFS），或连服务器把依赖存到服务器。

**页面更新后按钮没反应？**
资源带 `?v=<commit>` 版本号，正常刷新即会更新；偶发旧缓存用 `Ctrl+Shift+R`。

**拟合结果和服务器上直接跑不一致？**
两边共用 `backend/` 同一套算法；差异通常来自模板、FWHM、波段或误差来源不同，看「日志」页。

**大 cube 怎么办？** 用服务器模式 + 挂载目录（路径打开），不要网页上传。
预览图会对波段抽样（≤240 层）以控制内存。

---

## 6. 给开发者

```bash
git clone https://github.com/jefferslu95/SEW_web.git
cd SEW_web
python run.py --port 8765 --open          # 本地服务器模式
python scripts/build_webapp.py            # 组装纯静态站（webapp/）
python -m http.server -d webapp 8000      # 本地跑静态版（浏览器算力）
```

- 依赖：`flask numpy scipy matplotlib astropy plotly(<6) ppxf==9.5.0 ppxf-sew==0.8.2 powerbin`
- **页面发布**：推送到 `main` → `.github/workflows/pages.yml` 自动构建并发布到 GitHub Pages
- **服务器部署**：`tools/install_remote.sh`（页面上的向导就是它；Docker 优先，
  参数有 `--docker/--no-docker/--image/--rebuild/--pip-index/--root/--token/--cors`）
- **本地桥（可选）**：`tools/sew_bridge.py` —— 不想手工敲 `ssh -L` 时用

```
backend/     Flask 路由、FITS 识别、分 bin、拟合封装、模板管理
static/      前端源码（app.js / style.css / i18n/）
webapp/      构建产物（由 scripts/build_webapp.py 生成）
tools/       install_remote.sh（服务器部署）、sew_bridge.py（可选桥）
scripts/     build_webapp / fetch_pyodide / sync_templates / check_i18n
docs/        BRIDGE.md（连服务器）、GITHUB_PAGES.md、DEPLOY.md
```

**HTTP 接口**（服务器模式）：`/api/health`、`/api/browse`、`/api/upload`、`/api/open`、
`/api/meta`、`/api/image`、`/api/spectrum`、`/api/fwhm`、`/api/redshift`、
`/api/templates*`、`/api/fit`、`/api/fit/status/<id>`、`/api/fit/export/<id>`、
`/api/ws/*`（工作区：info/browse/list/get/put/delete/root/file）。

---

## 7. 实现要点 / 已知限制

- 拟合前会 `log_rebin` 到 `dlam=1e-4`（≈69 km/s），只按模板覆盖范围裁剪两端，
  中间坏点用 `goodpixels`/`mask` 排除
- pPXF 内部除以 `mean_gal`，结果数组已乘回，交互图与输入同量纲；`p.plot()` 原图仍是归一单位
- 模板分辨率比数据粗时（如 emiles FWHM≈3 Å vs R=7000），σ 会被模板分辨率限制，日志会提示但不去卷积
- 拟合任务在后台线程串行执行；进度条反映阶段而非迭代次数
