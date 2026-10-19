/* ------------------------------------------------------------------
   SEW web — 浏览器端（Pyodide）桥接层

   作用：给 static/app.js 提供和服务端完全一样的 `api()` 语义，
   但所有请求都被路由到 Web Worker 里的 Python（或本地 IndexedDB 工作区）。
   同时负责「工作区」：模板、数据文件、拟合结果的本地存储与列表界面。
   ------------------------------------------------------------------ */
(function () {
  const $ = (id) => document.getElementById(id);
  const WS_DB = 'sew-workspace';
  const WS_VER = 1;

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
  const worker = new Worker('worker.js');
  worker.onerror = (e) => {
    console.error('worker error', e.message, e.filename, e.lineno);
    setBootMsg('计算内核启动失败：' + e.message, true);
  };
  worker.onmessageerror = (e) => console.error('worker messageerror', e);
  worker.postMessage({ type: 'init' });
  let seq = 0;
  const waiting = new Map();
  let readyResolve;
  const ready = new Promise((r) => { readyResolve = r; });
  let booted = false;

  function setBootMsg(msg, err) {
    const el = $('topStatus');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'status' + (err ? ' err' : '');
  }

  worker.onmessage = (ev) => {
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
  };

  function rpc(method, path, query, body) {
    return ready.then(() => new Promise((res, rej) => {
      const id = ++seq;
      waiting.set(id, { res, rej });
      worker.postMessage({
        type: 'request', id, method, path,
        query: query || {}, body: body || {},
      });
    })).then((json) => JSON.parse(json));
  }

  function fsWrite(path, bytes) {
    return ready.then(() => new Promise((res, rej) => {
      const id = ++seq;
      waiting.set(id, { res, rej });
      worker.postMessage({ type: 'write', id, path, bytes });
    }));
  }

  function fsDelete(path) {
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
    const base = kind === KIND_TPL ? TPL_DIR
      : UP_DIR + Math.random().toString(16).slice(2, 10) + '_';
    const path = base + clean;
    const rec = { path, name: clean, kind, size: bytes.byteLength || bytes.length,
                  mtime: Date.now() };
    await WS.put('files', Object.assign({}, rec, { bytes }));
    await fsWrite(path, bytes);
    return rec;
  }

  async function removeFile(rec) {
    await WS.del('files', rec.path);
    try { await fsDelete(rec.path); } catch (e) { /* ignore */ }
  }

  async function syncWorkspace() {
    const items = await WS.all('files');
    for (const it of items) {
      try { await fsWrite(it.path, it.bytes); } catch (e) { console.warn(e); }
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
      await WS.put('results', rec);
      renderWorkspace();
    } catch (e) { console.warn('保存结果失败', e); }
  }

  async function openResult(rec) {
    try {
      await rpc('POST', '/api/_restore', {}, { id: rec.id, result: rec.json });
    } catch (e) { /* 忽略：内核可能较旧 */ }
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
  window.sewApi = async function (url, opts) {
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

    // 工作区（浏览器本地文件系统替身）
    if (path === '/api/browse') return browseWorkspace();

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
    const j = await rpc(method, path, query, body);
    if (!j || j.ok === false) {
      throw new Error((j && j.error) || `HTTP ${j && j.code || 500}`);
    }
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

  async function renderWorkspace() {
    const details = $('wsDetails');
    if (!details) return;
    details.classList.remove('hidden');
    const [files, results] = await Promise.all([WS.all('files'),
                                                WS.all('results')]);
    const tpls = files.filter((x) => x.kind === KIND_TPL);
    const fits = files.filter((x) => x.kind === KIND_FITS);

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
              const rec = await downloadTemplate(e.name, 'templates/' + e.file);
              if (window.toast) window.toast(t('ws.downloaded', { name: rec.name }));
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
    if (st) st.textContent = window.t ? t('ws.saved', { n: files.length + results.length }) : '';
  }

  /* ================= 事件绑定 ================= */
  async function bindWorkspaceUI() {
    await WS.open();
    await ready;
    await syncWorkspace();
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
          const rec = await downloadTemplate(name, url);
          if (window.toast) window.toast(t('ws.downloaded', { name: rec.name }));
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

  document.addEventListener('DOMContentLoaded', () => {
    // 浏览器端没有「服务器路径」概念，改提示
    const p = $('pathInput');
    if (p && window.t) p.placeholder = t('hdr.path.ph.wasm');
    bindWorkspaceUI().catch((e) => {
      console.error('workspace init failed', e);
      setBootMsg('本地工作区不可用：' + e.message, true);
    });
  });
})();
