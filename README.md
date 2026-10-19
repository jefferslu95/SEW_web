# SEW web — FITS 光谱 / 数据立方 查看与拟合

网页版交互工具：**打开 → 自动识别 → 看图 → 点像素 → pPXF / ppxf-sew 拟合 → 出图/导出**。

同一套算法有两种跑法：

| 模式 | 算力 | 适合 | 入口 |
|---|---|---|---|
| **浏览器端（纯静态）** | 访问者自己的 CPU（Pyodide / WebAssembly） | 不想装环境、要分享链接、数据不能出本机 | `webapp/`（见 [webapp/README.md](webapp/README.md)） |
| **服务器端（Flask）** | 服务器 CPU | 大 cube、多入共享、需要快 | `python run.py` |

两种模式共用 `backend/` 里同一套算法代码，结果口径一致。

技术栈：astropy + numpy/scipy + matplotlib + ppxf / ppxf-sew + Plotly（本地提供，无需 CDN）。

---

## 1. 快速开始

### 本地运行

```bash
cd /home/jflu/SEW_web
./start.sh                          # 默认 http://127.0.0.1:8765/
# 或
/home/jflu/anaconda3/bin/python run.py --port 8765 --open
```

远程机器上可让端口转发，或：

```bash
python run.py --host 0.0.0.0 --port 8765
```

> 依赖：`flask numpy scipy matplotlib astropy plotly(<6) ppxf==9.5.0 ppxf-sew==0.8.2
> powerbin`（见 `requirements.txt`；`powerbin` 提供 PowerBin 分 bin，自带 `plotbin`）。

### Docker（完全封装 Python 环境）

```bash
docker build -t sew-web .
docker run --rm -p 8765:8765 \
           -v /path/to/fits:/data \
           -v /path/to/templates:/app/data/templates \
           sew-web
```

镜像说明：

- 两阶段构建（builder 装依赖 → 运行镜像只带 `libgomp1`），容器内以非 root 用户
  `sew` 运行；`HEALTHCHECK` 轮询 `/api/health`。
- `ppxf_sew` 自带 emiles / fsps / galaxev / xsl 模板（`.../ppxf_sew/example/sps_models`），
  不挂载模板目录也能直接拟合；额外模板可挂到 `/app/data/templates`（即环境变量
  `SEW_TEMPLATE_DIR`），或在页面「拟合设置 → 模板库」里下载/上传。
- 上传的 FITS 与下载的模板都写在 `data/` 下；如需持久化，挂载
  `-v sew_data:/app/data`。

打开 `http://127.0.0.1:8765/`。FITS 文件既可以通过页面「上传」，也可以把宿主机目录挂进来后
在「浏览」里选择（例如挂到 `/data`，路径栏填 `/data/xxx.fits`）。

### 当成「在线工具」用（计算在本机，浏览器零环境）

服务跑在有算力的机器上，使用者只需浏览器：

```bash
# 服务器侧（对外暴露时务必带认证 + 目录白名单）
SEW_AUTH='user:强密码' SEW_DATA_ROOTS='/data:/home/user/fits' \
  python run.py --host 127.0.0.1 --port 8765
```

```bash
# 自己电脑侧（最安全：SSH 转发，不暴露公网）
ssh -N -L 8765:127.0.0.1:8765 user@server
# 然后浏览器打开 http://127.0.0.1:8765/
```

- `SEW_AUTH`：HTTP Basic 认证（覆盖所有路由，含图片/下载）；不设则无认证。
- `SEW_DATA_ROOTS`：只允许打开/浏览这些目录下的 FITS（`:` 分隔），上传目录始终允许。

完整方案（Cloudflare Tunnel / nginx + TLS / Tailscale、GitHub 上传、常见问题）见
**[docs/DEPLOY.md](docs/DEPLOY.md)**。

### 1.4 部署到 GitHub Pages（纯浏览器版，推荐给分享/试用）

浏览器端版本是纯静态站点（`webapp/`），可以直接托管在 GitHub Pages：

1. 推送仓库到 GitHub；
2. 仓库 **Settings → Pages → Source: GitHub Actions**；
3. 之后每次推送 `main`，`.github/workflows/pages.yml` 自动构建并发布
   （组装 `webapp/` → 下载 Pyodide 运行时 → 发布），地址形如
   `https://<账号>.github.io/SEW_web/`。

详细步骤与模板说明见 **[docs/GITHUB_PAGES.md](docs/GITHUB_PAGES.md)**。

> ⚠️ 不要直接用 `--host 0.0.0.0` 裸奔到公网：`/api/browse` 能列目录、`/api/open`
> 能打开任意路径，等于把服务器文件系统开放出去。

---

## 2. 功能

### 2.1 数据识别（自动 + 手动覆盖）

支持：

* **一维单光谱**：flux 在 HDU0 或任意扩展；波长来自 `WAVE/LAMBDA/LOGWAVE` 扩展、
  头卡 `CRVAL1/CDELT1/CRPIX1`、SDSS `COEFF0/COEFF1`（log10 λ），或手动 `start/step`。
* **BINTABLE 光谱**（LAMOST `COADD` 等）：`FLUX / IVAR / WAVELENGTH / ORMASK`
  分别按列指定；光谱可按行号选择（`nrows>1`）。
* **二维多光谱**：每行一条谱（自动选最长轴为谱轴，可指定行号）。
* **三维数据立方**：谱轴可位于 numpy 轴 0/1/2，内部统一为 `(wave, y, x)`。
* **波长类型**：线性 Å、`log10(λ)`（WiFeS/MUSE 常见 `CRVAL3≈3.5, CDELT3≈3e-5`）、
  频率轴（转 Å）、CUNIT 换算（nm/μm/m）。
* **误差**：`VAR/VARIANCE`、`ERR/ERROR/SIGMA/NOISE`、`IVAR`（自动识别并换算）。
* **Mask**：`MASK/DQ/BAD/FLAGS`；`非零=坏` / `非零=好` 可切换。
* 无名扩展兜底：很多 cube 只有 `[flux, variance]` 两层，扩展名为空；会按
  “形状相同 + 非负 + 量级远小于 flux”识别为 variance，并在界面提示。

识别失败/不理想时，展开左侧 **「识别结果 / 手动覆盖」**：可指定
flux / variance / mask / WAVE 的 HDU 与 **BINTABLE 列**、谱轴、行号、波长来源与
手动 start/step。

> 加载失败时前端会调用 `/api/inspect` 读取**这个文件**的结构并自动填好建议映射
> （如 LAMOST: `[1] COADD : FLUX / IVAR / ORMASK / WAVELENGTH`），直接点
> 「应用覆盖并重新识别」即可；不会继续停留在上一个文件。

### 2.2 查看

* 数据立方：白光照 / S/N 图，可调波段、颜色范围；**点击图像任意像素**提取该点光谱
  （可设置圆形孔径半径，按孔径求和并传播方差）。
* 单光谱：直接显示整条谱。
* 光谱图（Plotly）：缩放/悬停；可切换 **观测系 / 静止系**、标出 mask 坏点、叠加 1σ 误差带。

### 2.3 拟合

引擎：

* **ppxf-sew**（默认）：连续谱平滑 + 发射线窗口 + EW（`ppxf_sew.ewfit`），
  可打开 `mask_sync`（峰窗模型仲裁解掩膜）。
* **pPXF**（标准）：`ppxf.ppxf`。

参数：

| 参数 | 说明 |
|---|---|
| 模板 | emiles / fsps / galaxev / xsl（或用户上传的 npz） |
| 模板波长系 | air / vacuum（XSL 用户请按模板实际波长系选择） |
| FWHM | `λ/R` 或常量 Å；打开文件时自动从头卡/仪器名推断，可手改 |
| 红移 z | `auto`（模板 + 发射线互相关）或手动 |
| 拟合波段 | 观测系 Å，默认全谱（会自动裁到模板覆盖范围） |
| moments | 2 = (V, σ)，4 = (+h3, h4) |
| degree | 多项式阶数，默认 -1 |
| 发射线模板 | 19 条（Hδ…Hα、[OII]/[OIII]/[NII]/[SII] 等）；可选 tie Balmer / limit doublets |
| V / σ 范围 | 参数边界 |
| 噪声 | 使用文件误差 / 自动估计（相邻像素 MAD） |
| 正则化 | pPXF `regul`，使用模板的 (age, metal) 网格作为 reg_dim |

拟合结果：

* **拟合曲线直接叠加在主光谱图上**（同一个 Plotly 图：flux / best fit / stellar /
  gas，不另开子图）；拟合完成后自动切到「光谱」标签。
* 底部通栏结果面板：
  * 拟合摘要：χ²/dof、dof、RMS、S/N、红移、通量标度、耗时
  * 「ppxf plot()」：**直接调用 pPXF 自带 `p.plot()`** 渲染的 PNG
  * 「模板权重」：**直接调用模板类 `temp.plot(weights)`** 的 age–metal 权重图
* 「参数 / 气体线」：逐成分 V/σ/h3/h4；逐线 flux ± error、EW、V、σ
    * 「参数 / 气体线」：逐成分 V/σ/h3/h4；逐线 flux ± error、EW、V、σ
  * 「日志」：红移方法、模板信息、模板覆盖范围等
  * 导出 JSON / FITS（含数组与 GOOD 掩膜）

---

## 3. 目录结构

```
SEW_web/
├── run.py                 # 唯一启动入口
├── start.sh               # 本地封装启动
├── requirements.txt
├── Dockerfile
├── .dockerignore
├── backend/
│   ├── app.py             # Flask 路由 + 异步拟合任务
│   ├── fits_io.py         # FITS 类型/轴/波长/误差/mask 自动识别
│   ├── binning.py         # PowerBin 分 bin（powerbin）
│   ├── fitting.py         # 红移、FWHM、pPXF/ppxf-sew 封装、出图
│   └── templates.py       # 模板库扫描 / 下载 / 上传
├── static/
│   ├── index.html
│   ├── style.css
│   ├── app.js
│   ├── i18n.js            # 多语言运行时
│   └── i18n/              # zh.json / en.json 语言文件
└── data/
    ├── uploads/           # 网页上传的 FITS
    └── templates/         # 下载/上传的模板 npz
```

模板搜索顺序（同名取先找到者）：

1. `SEW_web/data/templates/`
2. `$SEW_TEMPLATE_DIR`
3. `~/.ppxf_sps_models/`
4. `ppxf_sew/example/sps_models/`（pip 自带 emiles/fsps/galaxev/xsl）

模板格式：`.npz`，至少包含 `templates (nwave,nage,nmetal), lam, ages, metals, fwhm`。
缺少模板时，可在「模板库 → 管理模板」里从 URL 下载或直接上传 npz。

---

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 版本信息 |
| GET | `/api/browse?path=` | 目录 / FITS 列表 |
| POST | `/api/upload` | 上传 FITS（multipart `file`） |
| POST | `/api/open` | `{path, override}` 打开并自动识别 |
| GET | `/api/meta` | 当前数据集元信息 |
| GET | `/api/image?mode=white\|snr&wave_min=&wave_max=&max_size=` | 2D 图像数组 |
| GET | `/api/spectrum?x=&y=&row=&aperture=` | 单谱 / 像素谱（wave, flux, err, mask） |
| GET | `/api/fwhm` | 自动识别的 FWHM 配置 |
| POST | `/api/redshift` | `{x?,y?,row?,aperture?,template,z_range}` 红移估计 |
| GET | `/api/templates` | 模板列表 |
| POST | `/api/templates/download` | `{name,url}` |
| POST | `/api/templates/upload` | multipart `file` + `name` |
| POST | `/api/fit` | 启动异步拟合，返回 `job_id` |
| GET | `/api/fit/status/<id>` | 进度 / 结果 |
| GET | `/api/fit/export/<id>?fmt=json\|fits` | 导出 |

`override` 可用字段：

```json
{
  "kind": "cube|spectrum",
  "flux_hdu": 0, "var_hdu": 1, "mask_hdu": null,
  "var_kind": "var|sigma|ivar",
  "mask_semantics": "bad_nonzero|good_nonzero",
  "spec_axis": 0, "row": 0,
  "wave_mode": "auto|hdu|wcs|custom",
  "wave_hdu": 1, "wave_start": 3500.0, "wave_step": 1.0, "wave_log": false
}
```

---

## 5. 实现要点 / 已知限制

* 拟合输入会先 `log_rebin` 到 `dlam=1e-4`（≈69 km/s）的均匀速度网格；只在两端按模板
  覆盖范围**连续裁剪**，中间坏点通过 `goodpixels`/`mask` 排除（否则 pPXF 的
  `velscale = c·Δlnλ` 断言会失败）。
* pPXF 内部会把谱除以 `mean_gal`。结果数组会乘回 `mean_gal`，交互图与输入谱同量纲；
  `p.plot()` 原图仍是 pPXF 归一单位。
* 模板分辨率比数据更粗时（如 emiles FWHM≈3 Å vs WiFeS R=7000），σ 会被模板分辨率限制；
  工具会在日志中打印模板波长范围和 age/metal 网格，但不会自动“去卷积”。
* 大 cube（>10 GB）建议用挂载目录 + 服务端路径打开，而不是网页上传。
* 单个大 cube 的一次点像素光谱提取很快（只读该列），但整幅预览图会对波段做抽样平均
  （最多 240 个波长层）以控制内存与响应时间。
* 拟合任务用后台线程串行执行；进度条只反映阶段，不反映迭代次数。

---

## 6. 示例文件

* 数据立方：`/home/jflu/DGIS/data/ESO154-G023_DATACUBE.fits`
* 单光谱：`/home/jflu/CCC/ssy_muse/spaxel_1-604053_y63_x27.fits`

打开页面后这两条路径会自动出现在输入框/示例提示里（存在时）。
