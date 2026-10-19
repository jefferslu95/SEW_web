/* ------------------------------------------------------------------
   SEW web — 浏览器端（Pyodide）桥接层

   作用：给 static/app.js 提供和服务端完全一样的 `api()` 语义，
   但所有请求都被路由到 Web Worker 里的 Python（或本地 IndexedDB 工作区）。
   同时负责「工作区」：模板、数据文件、拟合结果的本地存储与列表界面。
   ------------------------------------------------------------------ */
(function () {
  const $ = (id) => document.getElementById(id);
  const WS_DB = 'sew-workspace';
  const WS_VER = 2;   // 与 workspace.js 保持一致（版本只能升不能降）

  /* ================= IndexedDB 工作区 ================= */
  const WS = {
    db: null,
    async open() {
      if (this.db) return this.db;
      this.db = await new Promise((res, rej) => {
        const req = indexedDB.open(WS_DB, WS_VER);
        req.onupgradeneeded = () => {
          const db = req.result;
          if (!db.objectStoreNames.contains('files')) {
            db.createObjectStore('files', { keyPath: 'path' });
          }
          if (!db.objectStoreNames.contains('results')) {
            db.createObjectStore('results', { keyPath: 'id' });
          }
          if (!db.objectStoreNames.contains('meta')) {
            db.createObjectStore('meta');
          }
        };
        req.onsuccess = () => res(req.result);
        req.onerror = () => rej(req.error);
      });
      return this.db;
    },
    async _tx(store, mode) {
      const db = await this.open();
      return db.transaction(store, mode).objectStore(store);
    },
    async put(store, obj) {
      const s = await this._tx(store, 'readwrite');
      return new Promise((res, rej) => {
        const r = s.put(obj);
        r.onsuccess = () => res(obj);
        r.onerror = () => rej(r.error);
      });
    },
    async del(store, key) {
      const s = await this._tx(store, 'readwrite');
      return new Promise((res, rej) => {
        const r = s.delete(key);
        r.onsuccess = () => res();
        r.onerror = () => rej(r.error);
      });
    },
    async all(store) {
      const s = await this._tx(store, 'readonly');
      return new Promise((res, rej) => {
        const r = s.getAll();
        r.onsuccess = () => res(r.result || []);
        r.onerror = () => rej(r.error);
      });
    },
    async get(store, key) {
      const s = await this._tx(store, 'readonly');
      return new Promise((res, rej) => {
        const r = s.get(key);
        r.onsuccess = () => res(r.result);
        r.onerror = () => rej(r.error);
      });
    },
  };

  /* ================= Worker 通信 ================= */
  let worker = null;
  let seq = 0;
  const waiting = new Map();
  let readyResolve;
  const ready = new Promise((r) => { readyResolve = r; });
  let booted = false;

  function spawnWorker() {
    worker = new Worker('worker.js');
    worker.onerror = (e) => {
      console.error('worker error', e.message, e.filename, e.lineno);
      setBootMsg('计算内核启动失败：' + e.message, true);
    };
    worker.onmessageerror = (e) => console.error('worker messageerror', e);
    worker.postMessage({ type: 'init' });
    worker.onmessage = onWorkerMessage;
  }

  function setBootMsg(msg, err) {
    const el = $('topStatus');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'status' + (err ? ' err' : '');
  }

  function onWorkerMessage(ev) {
    const m = ev.data || {};
    if (m.type === 'log') {
      console.log('[py]', m.msg);
      if (!booted) setBootMsg(m.msg);
      return;
    }    if (m.type === 'ready') {
      booted = true;
      setBootMsg('');
      readyResolve();
      return;
    }
    if (m.type === 'progress') {
      const bar = $('fitProgress');
      const msg = $('fitMsg');
      if (bar) bar.style.width = Math.max(2, (m.frac || 0) * 100) + '%';
      if (msg && m.msg) {
        const o = m.args || {};
        msg.textContent = (m.key && window.t)
          ? window.t('fit.prog.' + m.key, o) : m.msg;
      }
      return;
    }
    if (m.type === 'response') {
      const w = waiting.get(m.id);
      if (!w) return;
      waiting.delete(m.id);
      if (m.error) w.rej(new Error(m.error));
      else w.res(m.json);
      return;
    }
    if (m.type === 'error') {
      console.error(m.msg);
      setBootMsg('计算内核出错：' + m.msg, true);
      return;
    }
  }

  // 等「工作区预初始化」（SW + 目录恢复）完成后再决定用什么算力；
  // 服务器工作区且后端声明 compute=server 时，完全不启动 Pyodide。
  const preInit = (window.__sewPreInit || Promise.resolve())
    .catch((e) => { console.warn('preInit failed', e); });
  // 算力归属尚未定时，不要启动内核：app.js 引导期会并发请求 /api/health，
  // 若此时强行 rpc() 会把 Pyodide 拉起来（服务器算力模式下完全不需要）。
  let decided = null;
  function whenDecided() {
    if (!decided) {
      decided = preInit.then(() => {
        if (computeOnServer()) {
          booted = true;
          setBootMsg('');
          readyResolve();
        } else {
          spawnWorker();
        }
        return true;
      });
    }
    return decided;
  }
  whenDecided();

  /* 服务器算力模式：算力与文件都在服务器，浏览器只做 UI */
  function computeOnServer() {
    const w = window.SEWWS;
    if (!w) return false;
    if (typeof w.serverCompute === 'function') return w.serverCompute();
    return w.backend === 'server' && !!(w.server && w.server.compute === 'server');
  }

  /* API 地址（页面可能在网页服务器，算力在另一台 SSH 服务器） */
  function apiUrl(p) {
    const w = window.SEWWS;
    return (w && typeof w.apiUrl === 'function') ? w.apiUrl(p) : p;
  }

  /* 直接转发到服务器（Flask），语义与 rpc() 相同（返回已解析的 JSON） */
  async function serverRpc(method, path, query, body) {
    let url = apiUrl(path);
    const qs = new URLSearchParams();
    Object.keys(query || {}).forEach((k) => {
      if (query[k] !== undefined && query[k] !== null) qs.set(k, query[k]);
    });
    if ([...qs.keys()].length) url += (url.includes('?') ? '&' : '?') + qs;
    const w = window.SEWWS;
    const opts = { method, cache: 'no-store',
                   headers: (w && w._hdr) ? w._hdr() : {} };
    if (method !== 'GET' && method !== 'HEAD') {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body || {});
    }
    const r = await fetch(url, opts);
    let j = null;
    try { j = await r.json(); } catch (e) { j = null; }
    if (j === null) throw new Error(`HTTP ${r.status}`);
    if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  }

  let wsSynced = null;
  function ensureWs() {
    if (!wsSynced) {
      wsSynced = syncWorkspace().catch((e) => console.warn('syncWorkspace', e));
    }
    return wsSynced;
  }

  async function rpc(method, path, query, body) {
    await whenDecided();
    if (computeOnServer()) return serverRpc(method, path, query, body);
    if (!worker) spawnWorker();          // 运行时从服务器算力切回本地算力
    return ready.then(ensureWs).then(() => new Promise((res, rej) => {
      const id = ++seq;
      waiting.set(id, { res, rej });
      worker.postMessage({
        type: 'request', id, method, path,
        query: query || {}, body: body || {},
      });
    })).then((json) => JSON.parse(json));
  }

  async function fsWrite(path, bytes) {
    await whenDecided();
    if (computeOnServer()) return;    // 服务器算力没有内存 FS
    return ready.then(() => new Promise((res, rej) => {
      const id = ++seq;
      waiting.set(id, { res, rej });
      worker.postMessage({ type: 'write', id, path, bytes });
    }));
  }

  async function fsDelete(path) {
    await whenDecided();
    if (computeOnServer()) return;
    return ready.then(() => new Promise((res, rej) => {
      const id = ++seq;
      waiting.set(id, { res, rej });
      worker.postMessage({ type: 'delete', id, path });
    }));
  }

  /* ================= 工作区文件 ================= */
  const KIND_TPL = 'template';
  const KIND_FITS = 'fits';
  const TPL_DIR = '/sew/data/templates/';
  const UP_DIR = '/sew/data/uploads/';

  async function addFile(kind, name, bytes) {
    const clean = String(name || 'file').replace(/[^\w.\-+]/g, '_');
    const wsfs = window.SEWWS;
    const sub = kind === KIND_TPL ? 'templates' : 'data';
    const onServer = computeOnServer();
    let path;
    if (onServer) {
      // 服务器算力：直接写到 <root>/<sub>/，不下载、不入浏览器存储
      await wsfs.writeFile(sub, clean, new Blob([bytes]));
      path = String(wsfs.server.root).replace(/\/+$/, '') + '/' + sub + '/' + clean;
    } else {
      const base = kind === KIND_TPL ? TPL_DIR
        : UP_DIR + Math.random().toString(16).slice(2, 10) + '_';
      path = base + clean;
      await fsWrite(path, bytes);
    }
    const rec = { path, name: clean, kind, size: bytes.byteLength || bytes.length,
                  mtime: Date.now() };
    if (onServer) return rec;          // 服务器目录即真相，不存浏览器副本
    await WS.put('files', Object.assign({}, rec, { bytes }));
    // 本地目录 / 静态服务器的服务器工作区：镜像一份
    if (wsfs && (wsfs.dir || wsfs.backend === 'server')) {
      try { await wsfs.writeFile(sub, clean, new Blob([bytes])); }
      catch (e) { console.warn('写入工作区失败', e); }
    }
    return rec;
  }

  async function removeFile(rec) {
    const wsfs = window.SEWWS;
    const sub = rec.kind === KIND_TPL ? 'templates' : 'data';
    if (computeOnServer()) {
      try { await wsfs.deleteFile(sub, rec.name); } catch (e) { /* ignore */ }
      return;
    }
    await WS.del('files', rec.path);
    try { await fsDelete(rec.path); } catch (e) { /* ignore */ }
    if (wsfs && (wsfs.dir || wsfs.backend === 'server')) {
      try { await wsfs.deleteFile(sub, rec.name); } catch (e) { /* ignore */ }
    }
  }

  /* 服务器算力：只对账文件名（不下载内容） */
  async function syncWorkspace() {
    // 服务器算力：列表直接读 <root>/templates|data，不需要把文件拉进内存 FS
    if (computeOnServer()) return [];
    const items = await WS.all('files');
    for (const it of items) {
      try { await fsWrite(it.path, it.bytes); } catch (e) { console.warn(e); }
    }
    // 从工作区目录导入尚未登记的文件（换浏览器 / 手放进去的）
    const wsfs = window.SEWWS;
    if (wsfs && (wsfs.dir || wsfs.backend === 'server')) {
      const known = new Set(items.map((x) => x.name));
      for (const [sub, kind] of [['templates', KIND_TPL], ['data', KIND_FITS]]) {
        let names = [];
        try { names = await wsfs.listDir(sub); } catch (e) { /* ignore */ }
        for (const name of names) {
          const isTpl = /\.npz$/i.test(name);
          if ((kind === KIND_TPL) !== isTpl) continue;
          if (known.has(name)) continue;
          try {
            const blob = await wsfs.readDirFile(sub, name);
            const buf = new Uint8Array(await blob.arrayBuffer());
            await addFile(kind, name, buf);
            items.push({ name, kind });
          } catch (e) { console.warn('导入工作区文件失败', name, e); }
        }
      }
    }
    return items;
  }

  /* ================= 结果存储 ================= */
  async function saveResult(r, jobId, target) {
    if (!r) return;
    const rec = {
      id: jobId || ('r' + Date.now()),
      jobId: jobId || '',
      target: target || '',
      created: Date.now(),
      summary: {
        z: r.z, chi2: r.chi2, dof: r.dof, snr: r.snr,
        template: (r.template || {}).name, engine: r.engine,
        fit_seconds: r.fit_seconds,
      },
      json: r,
      png: r.plot_png || '',
    };
    try {
      // 镜像到工作区 results/：<时间>_<模板>.json / .png
      const wsfs = window.SEWWS;
      const srvMode = !!(wsfs && wsfs.backend === 'server');
      const mirror = !!(wsfs && (wsfs.dir || srvMode));
      let stem = '';
      if (mirror) {
        const d = new Date(rec.created);
        const p2 = (n) => String(n).padStart(2, '0');
        stem = `${d.getFullYear()}${p2(d.getMonth() + 1)}${p2(d.getDate())}-`
          + `${p2(d.getHours())}${p2(d.getMinutes())}${p2(d.getSeconds())}`
          + `_${(rec.summary && rec.summary.template) || 'fit'}`;
        if (srvMode) rec.srvName = stem;
      }
      await WS.put('results', rec);
      if (mirror) {
        try {
          await wsfs.writeFile('results', stem + '.json',
            new Blob([JSON.stringify(r)], { type: 'application/json' }));
          if (r.plot_png) {
            const b64 = String(r.plot_png).replace(/^data:image\/png;base64,/, '');
            await wsfs.writeFile('results', stem + '.png', b64ToBlob(b64, 'image/png'));
          }
        } catch (e) { console.warn('写入工作区 results/ 失败', e); }
      }
      renderWorkspace();
    } catch (e) { console.warn('保存结果失败', e); }
  }

  async function openResult(rec) {
    // 换浏览器（服务器工作区）时本地索引里可能只有文件名，从服务器回读 JSON
    if (!rec.json && rec.srvName) {
      try {
        const wsfs = window.SEWWS;
        const blob = await wsfs.readDirFile('results', rec.srvName + '.json');
        rec.json = JSON.parse(await blob.text());
        rec.summary = Object.assign({}, rec.summary || {}, {
          template: (rec.json.template || {}).name,
          z: rec.json.z, chi2: rec.json.chi2, snr: rec.json.snr,
        });
        await WS.put('results', rec);
      } catch (e) { console.warn('读取服务器结果失败', e); }
    }
    if (!rec.json) {
      if (window.toast) window.toast('结果内容不可用', 'err');
      return;
    }
    try {
      await rpc('POST', '/api/_restore', {}, { id: rec.id, result: rec.json });
    } catch (e) { /* 忽略：内核可能较旧 / 服务器模式无此接口 */ }
    if (typeof window.sewSetJobId === 'function') window.sewSetJobId(rec.id);
    if (typeof window.renderResult === 'function') {
      window.renderResult(rec.json, rec.target || '');
    }
  }

  /* ================= 导出 ================= */
  function b64ToBlob(b64, mime) {
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    return new Blob([arr], { type: mime });
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  window.sewExport = async (jid, fmt) => {
    try {
      const j = await rpc('GET', `/api/fit/export/${jid}`, { fmt }, {});
      if (!j.ok) throw new Error(j.error || 'export failed');
      downloadBlob(b64ToBlob(j.b64, j.mime || 'application/octet-stream'),
                   j.filename || `sew_fit_${jid}.${fmt}`);
    } catch (e) {
      if (window.toast) window.toast(String(e.message || e), 'err');
    }
  };

  /* ================= 文件上传（FormData） ================= */
  function formFile(fd) {
    const f = fd && fd.get && fd.get('file');
    return f;
  }

  async function handleUpload(opts) {
    const fd = opts && opts.body;
    const f = formFile(fd);
    if (!f) throw new Error('没有上传文件');
    const buf = new Uint8Array(await f.arrayBuffer());
    const name = fd.get('name') || f.name || 'upload.fits';
    const isTpl = /\.npz$/i.test(name) || (opts.__tpl === true);
    const rec = await addFile(isTpl ? KIND_TPL : KIND_FITS,
                             isTpl && fd.get('name') ? name + '.npz' : f.name,
                             buf);
    renderWorkspace();
    return { ok: true, path: rec.path, name: rec.name, size: rec.size };
  }

  /* ================= 虚拟文件浏览（工作区） ================= */
  async function browseWorkspace() {
    const items = await WS.all('files');
    const files = items.filter((x) => x.kind === KIND_FITS)
      .sort((a, b) => a.name.localeCompare(b.name))
      .map((x) => ({ name: x.name, path: x.path, size: x.size,
                     mtime: x.mtime / 1000 }));
    return { ok: true, path: '工作区', parent: '工作区', dirs: [], files };
  }

  /* ================= 服务器目录浏览 ================= */
  async function browseServer(path) {
    const q = new URLSearchParams();
    if (path) q.set('path', path);
    const ws = window.SEWWS;
    const r = await fetch(apiUrl('api/ws/browse?' + q.toString()),
      { cache: 'no-store', headers: (ws && ws._hdr()) || {} });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  }

  /* 服务器上的绝对路径 → 先下载到 Pyodide 内存 FS，再交给 Python */
  const memPathCache = new Map();
  function strHash(s) {
    let h = 0x811c9dc5;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return h.toString(16).padStart(8, '0');
  }

  async function ensureMemPath(p) {
    await whenDecided();
    if (computeOnServer()) return p;      // 服务器算力：直接用服务器路径
    if (!p || !String(p).startsWith('/')) return p;
    if (String(p).startsWith('/sew/')) return p;
    if (memPathCache.has(p)) return memPathCache.get(p);
    const ws = window.SEWWS;
    const r = await fetch(apiUrl('api/ws/file?' + new URLSearchParams({ path: p })),
      { headers: (ws && ws._hdr()) || {} });
    if (!r.ok) throw new Error(`无法读取服务器文件（HTTP ${r.status}）`);
    const buf = new Uint8Array(await r.arrayBuffer());
    const base = String(p).split('/').pop() || 'file.fits';
    const clean = base.replace(/[^\w.\-+]/g, '_');
    const dst = '/sew/data/server/' + strHash(String(p)) + '/' + clean;
    await fsWrite(dst, buf);
    memPathCache.set(p, dst);
    return dst;
  }

  /* ================= 模板下载 ================= */
  const MANIFEST = 'templates/manifest.json';

  async function downloadTemplate(name, url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status} · ${url}`);
    const buf = new Uint8Array(await r.arrayBuffer());
    const fn = (String(name || '').trim() || url.split('/').pop() || 'template')
      + (/\.npz$/i.test(name || url) ? '' : '.npz');
    const rec = await addFile(KIND_TPL, /\.npz$/i.test(fn) ? fn : fn + '.npz', buf);
    renderWorkspace();
    return rec;
  }

  /* ================= api shim ================= */
  async function serverPassthru(url, opts) {
    const abs = apiUrl(url);
    const r = await fetch(abs, Object.assign({ cache: 'no-store' }, opts || {}));
    let j = null;
    try { j = await r.json(); } catch (e) { j = null; }
    if (j === null) throw new Error(`HTTP ${r.status}`);
    if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  }

  window.sewApi = async function (url, opts) {
    // 服务器算力：不用 Pyodide，也不用内存 FS，全部直接打后端
    await whenDecided();
    if (computeOnServer()) return serverPassthru(url, opts);
    opts = opts || {};
    const u = new URL(url, location.href);
    const path = u.pathname;
    const query = {};
    u.searchParams.forEach((v, k) => { query[k] = v; });
    const method = (opts.method || 'GET').toUpperCase();

    // 上传（FormData）
    if (path === '/api/upload') return handleUpload(opts);
    if (path === '/api/templates/upload') {
      return handleUpload(Object.assign({}, opts, { __tpl: true }));
    }

    // 工作区（浏览器本地文件系统替身）；服务器模式下直接浏览服务器目录
    if (path === '/api/browse') {
      const ws = window.SEWWS;
      if (ws && ws.backend === 'server') return browseServer(query.path || '');
      return browseWorkspace();
    }

    // 模板下载：交给 JS fetch，落到工作区
    if (path === '/api/templates/download') {
      const body = opts.body && typeof opts.body === 'string'
        ? JSON.parse(opts.body) : (opts.body || {});
      const rec = await downloadTemplate(body.name, body.url);
      return { ok: true, path: rec.path, name: reShort(rec.name) };
    }

    // 其余全部交给 Python
    let body = {};
    if (typeof opts.body === 'string' && opts.body) {
      try { body = JSON.parse(opts.body); } catch (e) { body = {}; }
    }
    // /api/open、/api/inspect 可能传入服务器上的绝对路径：先下载到内存 FS
    let originalPath = null;
    if ((path === '/api/open' || path === '/api/inspect') && body.path) {
      originalPath = body.path;
      body.path = await ensureMemPath(body.path);
    }
    const j = await rpc(method, path, query, body);
    if (!j || j.ok === false) {
      throw new Error((j && j.error) || `HTTP ${j && j.code || 500}`);
    }
    if (originalPath && j && typeof j === 'object') j.path = originalPath;
    return j;
  };

  function reShort(fn) {
    return String(fn).replace(/^spectra_/, '').replace(/_\d+\.\d+.*$/, '')
      .replace(/\.npz$/i, '').toLowerCase();
  }

  /* ================= 模板清单（可下载模板） ================= */
  let manifestCache = null;
  async function loadManifest() {
    if (manifestCache) return manifestCache;
    try {
      const r = await fetch('templates/manifest.json');
      manifestCache = r.ok ? ((await r.json()).templates || []) : [];
    } catch (e) { manifestCache = []; }
    return manifestCache;
  }

  /* ================= 工作区界面 ================= */
  function fmtSize(n) {
    n = Number(n || 0);
    if (n > 1048576) return (n / 1048576).toFixed(1) + ' MB';
    return Math.max(1, Math.round(n / 1024)) + ' KB';
  }

  /* 服务器工作区：直接读目录（服务器就是唯一真相，不靠 IndexedDB 缓存） */
  async function serverList(sub) {
    const ws = window.SEWWS;
    try {
      const r = await fetch(apiUrl('api/ws/list?' + new URLSearchParams({ dir: sub })),
        { cache: 'no-store', headers: (ws && ws._hdr()) || {} });
      const j = await r.json();
      return (j && j.files) || [];
    } catch (e) {
      console.warn('读取服务器工作区失败', sub, e);
      return [];
    }
  }

  function serverPath(sub, name) {
    const ws = window.SEWWS;
    const root = (ws && ws.server && ws.server.root) || '';
    return String(root).replace(/\/+$/, '') + '/' + sub + '/' + name;
  }

  function row(name, size, buttons) {
    const el = document.createElement('div');
    el.className = 'wsrow';
    const nm = document.createElement('span');
    nm.className = 'nm'; nm.textContent = name; nm.title = name;
    el.appendChild(nm);
    if (size != null) {
      const sz = document.createElement('span');
      sz.className = 'sz'; sz.textContent = fmtSize(size);
      el.appendChild(sz);
    }
    (buttons || []).forEach(([label, fn]) => {
      const b = document.createElement('button');
      b.className = 'ghost'; b.textContent = label;
      b.onclick = fn; el.appendChild(b);
    });
    return el;
  }

  function emptyHint(box) {
    if (!box.children.length) {
      const d = document.createElement('div');
      d.className = 'wsrow';
      d.innerHTML = `<span class="nm muted">${window.t ? t('ws.empty') : ''}</span>`;
      box.appendChild(d);
    }
  }

  // 串行化：并发调用（例如刚下载完 + 别处触发）不会把列表渲染两遍
  let renderChain = Promise.resolve();
  function renderWorkspace() {
    renderChain = renderChain
      .then(() => renderWorkspaceNow())
      .catch((e) => console.warn('renderWorkspace', e));
    return renderChain;
  }

  async function renderWorkspaceNow() {
    const details = $('wsDetails');
    if (!details) return;
    let tpls, fits, results;
    if (computeOnServer()) {
      // 服务器算力：默认不预设工作区；未设置时只显示提示
      const ws = window.SEWWS;
      if (!ws || !ws.dirName) {
        ['wsTplList', 'wsAvailList', 'wsFileList', 'wsResList'].forEach((id) => {
          const el = $(id); if (el) el.innerHTML = '';
        });
        const st0 = $('wsStatus');
        if (st0) st0.textContent = t('ws.unsetHint');
        return;
      }
      const [tf, df, rs] = await Promise.all([
        serverList('templates'), serverList('data'), WS.all('results')]);
      tpls = tf.map((f) => ({ name: f.name, size: f.size, kind: KIND_TPL }));
      fits = df.map((f) => ({ name: f.name, size: f.size, kind: KIND_FITS,
                              path: serverPath('data', f.name) }));
      results = rs;
    } else {
      const files = await WS.all('files');
      tpls = files.filter((x) => x.kind === KIND_TPL);
      fits = files.filter((x) => x.kind === KIND_FITS);
      results = await WS.all('results');
    }

    const tl = $('wsTplList'); tl.innerHTML = '';
    tpls.forEach((r) => {
      const short = reShort(r.name);
      tl.appendChild(row(r.name, r.size, [
        [window.t ? t('ws.use') : 'Use', () => {
          const sel = $('templateSel');
          if (sel && [...sel.options].some((o) => o.value === short)) {
            sel.value = short;
            if (window.updateDlamUI) window.updateDlamUI();
          } else {
            window.loadTemplates && window.loadTemplates();
          }
        }],
        [window.t ? t('ws.del') : 'Del', async () => {
          await removeFile(r); renderWorkspace();
        }],
      ]));
    });
    emptyHint(tl);

    const avail = $('wsAvailList');
    if (avail) {
      avail.innerHTML = '';
      const have = new Set(tpls.map((r) => reShort(r.name)));
      const man = await loadManifest();
      man.filter((e) => !have.has(e.name)).forEach((e) => {
        avail.appendChild(row(e.label || e.name, (e.size_mb || 0) * 1048576, [
          [window.t ? t('ws.download') : 'Download', async (ev) => {
            const b = ev.target;
            b.disabled = true;
            b.textContent = window.t ? t('tpl.msg.downloading') : '…';
            try {
              // 默认从 ppxf_data（micappe/ppxf_data）下载；无 url 时用站点内副本
              const srcUrl = e.url || (e.file ? ('templates/' + e.file) : '');
              if (computeOnServer()) {
                const body = e.url ? { name: e.name, url: e.url }
                  : { name: e.name, file: e.file };
                await serverRpc('POST', '/api/templates/download', {}, body);
              } else {
                const rec = await downloadTemplate(e.name, srcUrl);
                if (window.toast) window.toast(t('ws.downloaded', { name: rec.name }));
              }
              if (window.loadTemplates) await window.loadTemplates();
            } catch (err) {
              if (window.toast) window.toast(String(err.message || err), 'err');
            }
            renderWorkspace();
          }],
        ]));
      });
      emptyHint(avail);
    }

    const fl = $('wsFileList'); fl.innerHTML = '';
    fits.forEach((r) => {
      fl.appendChild(row(r.name, r.size, [
        [window.t ? t('ws.open') : 'Open', () => {
          if (window.openPath) window.openPath(r.path);
        }],
        [window.t ? t('ws.del') : 'Del', async () => {
          await removeFile(r); renderWorkspace();
        }],
      ]));
    });
    emptyHint(fl);

    const rl = $('wsResList'); rl.innerHTML = '';
    results.sort((a, b) => b.created - a.created).forEach((rec) => {
      const s = rec.summary || {};
      const label = `${new Date(rec.created).toLocaleString()} · `
        + `${s.template || '?'} · z=${Number(s.z || 0).toFixed(5)}`;
      rl.appendChild(row(label, null, [
        [window.t ? t('ws.open') : 'Open', () => openResult(rec)],
        [window.t ? t('ws.del') : 'Del', async () => {
          await WS.del('results', rec.id); renderWorkspace();
        }],
      ]));
    });
    emptyHint(rl);

    const st = $('wsStatus');
    if (st) st.textContent = window.t
      ? t('ws.saved', { n: tpls.length + fits.length + results.length }) : '';
  }

  /* ================= 事件绑定 ================= */
  async function bindWorkspaceUI() {
    await WS.open();
    await ready;
    await ensureWs();
    await renderWorkspace();

    const dl = $('wsTplDl');
    if (dl) {
      dl.onclick = async () => {
        const url = ($('wsTplUrl').value || '').trim();
        const name = ($('wsTplName') && $('wsTplName').value || '').trim();
        if (!url) {
          if (window.toast) window.toast(t('ws.needUrl'), 'err');
          return;
        }
        dl.disabled = true;
        try {
          if (computeOnServer()) {
            await serverRpc('POST', '/api/templates/download', {},
                             { name: name || 'template', url });
          } else {
            const rec = await downloadTemplate(name, url);
            if (window.toast) window.toast(t('ws.downloaded', { name: rec.name }));
          }
          if (window.loadTemplates) await window.loadTemplates();
        } catch (e) {
          if (window.toast) window.toast(String(e.message || e), 'err');
        } finally { dl.disabled = false; }
      };
    }

    const up = $('wsTplUp');
    if (up) {
      up.onchange = async () => {
        const f = up.files && up.files[0];
        if (!f) return;
        if (computeOnServer()) {
          // 服务器算力：直接 multipart 传给服务器
          const fd = new FormData();
          fd.append('file', f);
          try {
            await serverPassthru('api/templates/upload', { method: 'POST', body: fd });
            if (window.toast) window.toast(t('ws.downloaded', { name: f.name }));
          } catch (e) {
            if (window.toast) window.toast(String(e.message || e), 'err');
          }
          up.value = '';
          await renderWorkspace();
          if (window.loadTemplates) await window.loadTemplates();
          return;
        }
        const buf = new Uint8Array(await f.arrayBuffer());
        const rec = await addFile(KIND_TPL, f.name, buf);
        up.value = '';
        if (window.toast) window.toast(t('ws.downloaded', { name: rec.name }));
        await renderWorkspace();
        if (window.loadTemplates) await window.loadTemplates();
      };
    }

    // 拖拽文件到页面：FITS 直接打开，npz 存成模板
    document.addEventListener('dragover', (e) => e.preventDefault());
    document.addEventListener('drop', async (e) => {
      e.preventDefault();
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (!f) return;
      const buf = new Uint8Array(await f.arrayBuffer());
      if (/\.npz$/i.test(f.name)) {
        await addFile(KIND_TPL, f.name, buf);
        await renderWorkspace();
        if (window.loadTemplates) await window.loadTemplates();
      } else {
        const rec = await addFile(KIND_FITS, f.name, buf);
        await renderWorkspace();
        if (window.openPath) window.openPath(rec.path);
      }
    });
  }

  window.sewOnResult = saveResult;
  // 刚绑定/切换工作区目录后重新扫描（导入目录里已有的模板与 FITS）
  window.sewRescanWorkspace = async () => {
    wsSynced = null;
    await ensureWs();
    await renderWorkspace();
    if (window.loadTemplates) await window.loadTemplates();
  };

  document.addEventListener('DOMContentLoaded', () => {
    const p = $('pathInput');
    bindWorkspaceUI().then(() => {
      if (p && window.t && !computeOnServer()) {
        // 浏览器端没有「服务器路径」概念，改提示
        p.placeholder = t('hdr.path.ph.wasm');
      }
    }).catch((e) => {
      console.error('workspace init failed', e);
      setBootMsg('本地工作区不可用：' + e.message, true);
    });
  });
})();
