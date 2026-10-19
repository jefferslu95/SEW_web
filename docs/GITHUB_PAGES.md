# 部署到 GitHub Pages

SEWweb 的浏览器端版本是**纯静态站点**（`webapp/`）：页面 + Pyodide/WASM 运行时。
**算力与工作区跟随「打开页面的那台机器」**：

- 在自己电脑的浏览器里打开 → 用本机 CPU，工作区是本机目录/浏览器存储；
- **在算力服务器上的浏览器里打开 → 用的就是那台服务器的 CPU**，工作区也在那台服务器上
  （此时“访问者”就是算力服务器，不需要 `run.py`）。

数据不上传网页服务器，只在打开页面的那台机器上处理。

---

## 一、一次性准备

### 1. 建立仓库并推送

```bash
cd SEW_web
git remote add origin https://github.com/<你的账号>/SEW_web.git
git branch -M main
git push -u origin main
```

（也可以先在 GitHub 网页上新建空仓库，再执行上面的 `remote add` + `push`。）

### 2. 打开 Pages

GitHub 仓库 → **Settings → Pages** → **Build and deployment**
→ Source 选 **GitHub Actions**（不要选 "Deploy from a branch"）。

之后每次推送 `main`，`.github/workflows/pages.yml` 会自动构建并发布，
地址形如 `https://<你的账号>.github.io/SEW_web/`。

---

## 二、构建时自动做的事

`pages.yml` 会：

1. `python scripts/build_webapp.py` —— 由 `static/` + `backend/` 组装出 `webapp/`；
2. `python scripts/fetch_pyodide.py` —— 下载 Pyodide 运行时与纯 Python 轮子到
   `webapp/vendor/`（≈57 MB，已加 Actions 缓存，第二次起很快）；
3. `python scripts/sync_templates.py` —— 检查可下载模板是否齐全；
4. `touch webapp/.nojekyll` —— 关掉 Jekyll；
5. `upload-pages-artifact` + `deploy-pages` 发布 `webapp/`。

---

## 三、关于模板

页面的「模板库 → 可下载模板」的地址指向 **https://github.com/micappe/ppxf_data**
（pPXF 官方数据仓库，含 emiles / fsps / galaxev / xsl 的 npz），每个条目在
`webapp/templates/manifest.json` 里有 `url` 字段。

- **浏览器端**：点「下载」直接从 `raw.githubusercontent.com/micappe/ppxf_data` 拉取；
- **服务器端**（`run.py`）：由服务器直接下载到工作区 `templates/`；
- **构建时**：`pages.yml` 会执行 `python scripts/sync_templates.py --download`，
  把模板一并放进站点（即使仓库里不存 npz，访问者也不用依赖外网）。

如需完全离线：在本机执行 `python scripts/sync_templates.py --download`（或 `--copy`
从 ppxf_sew 自带目录复制），再把 `webapp/templates/*.npz` 提交进仓库即可。

---

## 四、发布后怎么用

打开 `https://<你的账号>.github.io/SEW_web/`：

1. 首次进入会引导设置**工作区**：
   - 「选择工作目录…」= 浏览器文件系统授权（Chrome/Edge，推荐）；
   - 「浏览器私有目录」= OPFS；
   - 「仅用浏览器存储」= IndexedDB（最省事，但清站点数据会丢失）。
2. 从「模板库 → 可下载模板」下载一个模板（或上传自己的 npz）；
3. 载入 FITS（拖拽 / 上传 / 从工作区打开）→ 显示 + 拟合。

算力就是**你打开页面的这台机器**的浏览器（Pyodide），不需要任何服务器。

---

## 五、常见问题

**Q：为什么首次打开比较慢？**
要下载 Pyodide 运行时（≈57 MB）和所选的模板（13–20 MB）。之后由 Service Worker
缓存，第二次打开几乎不下载。

**Q：能不能不提交 vendor/？**
可以，也不应该提交——`pages.yml` 每次构建时下载并缓存，产物不进 git。

**Q：想更新页面怎么办？**
改 `static/` 或 `backend/` 后推送到 `main`，Actions 会自动重新构建发布。

**Q：私有仓库能用 Pages 吗？**
可以（取决于账号套餐）。公开仓库最省事。

**Q：用自定义域名？**
仓库 Settings → Pages → Custom domain，按提示配置 CNAME 即可；
`webapp/` 里所有资源都是相对路径，不需要改代码。

---

## 六、推送失败排障（GitHub 连不上）

### 症状 A：`GnuTLS recv error (-110)`

```
fatal: 无法访问 'https://github.com/<owner>/<repo>.git/'：
GnuTLS recv error (-110): The TLS connection was non-properly terminated.
```

**不是仓库或凭据的问题，是 TCP 层连不上。** 先判断是哪一层：

```bash
getent hosts github.com                 # 看 DNS 解析到哪个 IP
timeout 6 bash -c 'cat < /dev/null > /dev/tcp/github.com/443' && echo 443-OK
```

实测（本机）：DNS 给的是 `20.205.243.166`，该 IP 的 443 被阻断；但
`140.82.112.3` / `140.82.113.3` / `20.27.177.113` 等官方 IP 的 443 正常，
`github.com:22` 与 `ssh.github.com:443` 也可连。**即只有 DNS 指到的那个 IP 坏了。**

### 解法 1（推荐）：改用 SSH

```bash
ls ~/.ssh/*.pub                                   # 没有就先 ssh-keygen -t ed25519
ssh -T git@github.com                             # 应显示 Hi <user>! You've successfully authenticated
git remote set-url origin git@github.com:<owner>/<repo>.git
git push -u origin main
```

若提示 `Permission denied (publickey)`，把 `~/.ssh/id_*.pub` 的内容加到
<https://github.com/settings/ssh/new> 即可。

### 解法 2：仍走 HTTPS，但绕开坏 IP

`scripts/gh_relay.py` 是一个只做 TCP 转发的本地 CONNECT 代理（TLS 仍由 git 完成，
不改 hosts、不需要 root）：

```bash
python3 scripts/gh_relay.py 7899 &
git -c http.proxy=http://127.0.0.1:7899 push
```

脚本会先试 DNS 给的 IP，连不上再依次尝试 `FALLBACK_IPS` 里的官方 IP 并缓存结果。

### 症状 B：`Repository not found`

```bash
git ls-remote origin
```

- 报 `Repository not found` 且仓库确实存在 ⇒ 多半是**私有仓库**，需要认证
  （HTTPS 用 Personal Access Token，别用账号密码）；或账号名/仓库名拼写不符。
- 公开仓库无需认证即可 `ls-remote`。
- 仓库还没建的话，先在 GitHub 上 New repository（**不要**勾选自动生成 README，
  否则首次 push 要先 `git pull --rebase`）。
