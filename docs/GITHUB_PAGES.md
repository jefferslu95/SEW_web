# 部署到 GitHub Pages

SEWweb 的浏览器端版本是**纯静态站点**（`webapp/`）：页面 + Pyodide/WASM 运行时，
计算在访问者的浏览器里，数据不上传。因此可以直接托管在 GitHub Pages 上。

> 注意：GitHub Pages 只托管静态文件。若需要「服务器算力 + 服务器工作区」，
> 请看 `docs/DEPLOY.md` 的 `run.py` 部署方式。

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

## 三、关于模板（重要）

页面的「模板库 → 可下载模板」需要 `webapp/templates/*.npz`（emiles / fsps / galaxev，
共约 48 MB）。它们默认被 `.gitignore` 排除，**不会**随仓库上传，所以有两种做法：

### 做法 A（最简单，推荐）：把模板提交进仓库

```bash
# 让 git 不再忽略模板 npz
sed -i '/webapp\/templates\/\*\.npz/d' .gitignore

# 从本机 ppxf_sew 自带目录补齐并提交
python scripts/sync_templates.py --copy
git add webapp/templates/*.npz .gitignore
git commit -m "templates: 加入可下载模板"
git push
```

这样 Pages 站点里模板可直接下载（每个文件 <50 MB，仓库总量也远小于 1 GB 限制）。

### 做法 B：不提交模板

站点仍可正常打开与使用，但点「下载」会提示 404；用户需要自己上传 `.npz`
（或把模板放到工作区的 `templates/` 目录里）。

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
