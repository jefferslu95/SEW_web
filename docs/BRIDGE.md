# 本地 SSH 桥：让 GitHub Pages 使用任意算力服务器

目标：**GitHub Pages 永远是唯一界面**；算力和文件放在**任意** SSH 可达的算力服务器上。
服务器不需要公网 IP、不开放端口、不需要对外部署 Web 服务。

```
浏览器  https://jefferslu95.github.io/SEW_web/
   │  HTTP（跨源，带 CORS / Private-Network-Access 头）
   ▼
本机    127.0.0.1:8977          tools/sew_bridge.py
   │  反向代理
   ▼
本机    127.0.0.1:8899          ssh -N -L 建立的本地转发
   │  SSH（你自己已有的密钥/免密）
   ▼
服务器  127.0.0.1:8899          run.py（只监听回环）
```

桥做三件事：

1. 必要时通过 SSH 在服务器上拉起 `run.py --host 127.0.0.1 --port 8899`（不对外）；
2. 建立 `ssh -N -L 127.0.0.1:8899:127.0.0.1:8899` 隧道；
3. 在 `127.0.0.1:8977` 做反向代理，并注入 `Access-Control-Allow-Origin`
   与 `Access-Control-Allow-Private-Network: true`，让 HTTPS 页面能跨源访问本地端口。

---

## 用法 0（最简单）：部署 + SSH 转发，直接打开服务器页面

不需要桥，也不需要 CORS。

1. **部署到服务器**（Docker 优先，服务器无需 Python 环境）：

   ```bash
   curl -fsSL https://jefferslu95.github.io/SEW_web/tools/install_remote.sh \
     | bash -s -- --host 127.0.0.1 --port 8899 --root ~/sew-ws
   ```

   脚本会自动：检测 Docker → 构建/复用 `sew-web:latest` 镜像 → 挂载工作区
   → 以 `--restart unless-stopped` 常驻。没有 Docker 时自动回退到 Python 模式。

2. **本机建隧道**（Windows 自带 OpenSSH）：

   ```bash
   ssh -N -L 8899:127.0.0.1:8899 user@host
   ```

3. **浏览器打开** <http://127.0.0.1:8899>

页面内容和 GitHub Pages 版**完全相同**（同一份代码），但算力、工作区、文件都在服务器；
地址栏是 `127.0.0.1:8899`，属于同源访问，不需要桥、不需要跨域头。

> 页面向导里「打开服务器页面（最简单）」按钮就是做第 3 步。

---

## 用法 A（需要页面地址仍是 github.io）
```bash
git clone https://github.com/jefferslu95/SEW_web.git
cd SEW_web
python tools/sew_bridge.py --ssh jflu@CSST1
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--ssh jflu@CSST1` | **必填**，SSH 目标（建议先配好免密：`ssh-copy-id`） |
| `--port 8977` | 本地代理端口（浏览器访问它） |
| `--remote-port 8899` | 远端 `run.py` 端口（回环） |
| `--remote-dir ~/SEW_web` | 远端代码目录 |
| `--remote-root /path/ws` | 远端工作区根目录（也可稍后在页面上选） |
| `--origin URL` | 额外放行的来源，可重复 |
| `--no-autostart` | 不自动在远端启动，只复用已有实例 |

启动后终端会打印：

```
桥已启动：http://127.0.0.1:8977
  https://jefferslu95.github.io/SEW_web/#bridge=http://127.0.0.1:8977
```

打开页面 → 点左上角 **SEWweb** 进设置 → 「算力服务器（本地 SSH 桥）」填
`http://127.0.0.1:8977` → **连接**。之后状态栏显示
`算力：服务器 · 工作区：<远端路径>`，「浏览…」列出的是**服务器**的目录。

## 用法 B（本机没有 Python）

远端（只需一次）：

```bash
cd ~/SEW_web
python run.py --host 127.0.0.1 --port 8800 \
  --cors https://jefferslu95.github.io --root /path/to/workspace
```

本机（Windows 自带 OpenSSH）：

```powershell
ssh -N -L 8800:127.0.0.1:8800 jflu@CSST1
```

页面里把桥地址填成 `http://127.0.0.1:8800`（这里没有桥，CORS 由 `--cors` 提供）。

---

## 常见问题

**浏览器提示 Private Network Access / 跨源被拒？**
用用法 A（桥会补 `Access-Control-Allow-Private-Network`）。
若用用法 B，远端必须带 `--cors https://jefferslu95.github.io`。

**页面依旧走本地算力？**
打开浏览器控制台看 `SEWWS.apiBase` 是否为你填的地址；
`/api/ws/info` 打不开时页面会退回浏览器模式。

**想加口令？**
远端启动时加 `--token XXX`，页面在该提示出现时输入一次即可
（保存在 localStorage）。

**会不会把服务器暴露到公网？**
不会。`run.py` 只监听服务器回环地址，流量全程走你的 SSH 连接，
桥本身也只绑定 `127.0.0.1`。

**切换/断开？**
设置面板里点「断开」清除保存的桥地址；或清掉 `localStorage['sew.bridge']`。
