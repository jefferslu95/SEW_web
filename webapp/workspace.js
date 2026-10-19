/* ------------------------------------------------------------------
   SEW web — 工作区（浏览器端）

   首次打开时引导用户选一个本地目录作为工作区：
       <workdir>/templates   模板 npz
       <workdir>/results     拟合结果（json + png）
       <workdir>/data        导入的 FITS
       <workdir>/cache       运行依赖副本（vendor/**）
   选好后：
     * 目录句柄存 IndexedDB，下次自动恢复（只需一次授权）；
     * 运行依赖写入 CacheStorage 并镜像到 <workdir>/cache/vendor；
     * 模板 / 数据 / 结果同时写 IndexedDB（索引）与工作区目录（真实文件）。
   不支持 File System Access API 的浏览器会退回「仅浏览器存储」。
   ------------------------------------------------------------------ */
(function () {
  const $ = (id) => document.getElementById(id);
  const T = (k, v) => (window.t ? window.t(k, v) : k);
  const IDB_NAME = 'sew-workspace';
  const DIR_STORE = 'meta';
  const DIR_KEY = 'dirHandle';
  const SUBDIRS = ['templates', 'results', 'data', 'cache'];

  const FS_SUPPORTED = typeof window.showDirectoryPicker === 'function';
  // 本页若由静态站点提供（无后端），算力与工作区都在浏览器内，File System
  // Access 只能选到“运行浏览器的那台机器”的目录；想用 SSH 服务器上的目录与
  // 算力，必须打开服务器上 run.py 提供的页面（那里有 api/ws/browse）。

  const SEWWS = {
    supported: FS_SUPPORTED,
    dir: null,           // FileSystemDirectoryHandle（本地目录 / OPFS）
    dirName: '',
    server: null,        // {root, writable, compute, ...} 服务器工作区
    backend: 'browser',  // 'server' | 'local' | 'browser'
    compute: 'browser',  // 'server' = 服务器算力（不启动 Pyodide）
    _token: '',
    ready: null,         // Promise
    autoSaveVendor: true,

    /* API 地址：永远与页面同源（页面在哪台服务器，算力/工作区就在那台） */
    apiUrl(p) {
      return String(p || '').replace(/^\/+/, '');
    },

    /* 是否走服务器算力（服务器工作区 + 后端声明 compute=server） */
    serverCompute() {
      return this.backend === 'server'
        && ((this.server && this.server.compute) || 'browser') === 'server';
    },

    _hdr(extra) {
      return Object.assign({ 'X-SEW-Token': this._token || '' }, extra || {});
    },

    /* ---------------- 服务器工作区 ---------------- */
    async detectServer() {
      const url = this.apiUrl('api/ws/info');
      try {
        let r = await fetch(url, { cache: 'no-store', headers: this._hdr() });
        if (r.status === 401) {
          const tok = localStorage.getItem('sew.ws.token') || '';
          if (tok) {
            this._token = tok;
            r = await fetch(url, { cache: 'no-store', headers: this._hdr() });
          }
          if (r.status === 401) { this._needsToken = true; return null; }
        }
        if (!r.ok) return null;
        const j = await r.json();
        if (!j.ok) return null;
        this.server = j;
        return j;
      } catch (e) { return null; }
    },

    useServer(info) {
      this.server = info || this.server;
      if (!this.server) throw new Error('没有可用的服务器工作区');
      this.backend = 'server';
      this.compute = this.server.compute || 'browser';
      this.dir = null;
      this.dirName = this.server.root || '';
      return this.server;
    },

    async setPref(v) {
      try { await this._metaPut('backend', v); } catch (e) { /* ignore */ }
    },

    /* 切换服务器工作区目录（服务端重启后仍有效） */
    async setServerRoot(path) {
      const r = await fetch(this.apiUrl('api/ws/root'), {
        method: 'POST',
        headers: this._hdr({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ root: path }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false) {
        throw new Error(j.error || `HTTP ${r.status}`);
      }
      const info = await this.detectServer();
      this.useServer(info || { root: j.root, writable: true });
      await this.setPref('server');
      return this.server;
    },

    async _srvPut(dir, name, blob) {
      const q = new URLSearchParams({ dir, name });
      const r = await fetch(this.apiUrl('api/ws/put?' + q), {
        method: 'POST', headers: this._hdr({ 'Content-Type': 'application/octet-stream' }),
        body: blob,
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
      return true;
    },
    async _srvList(dir) {
      const r = await fetch(this.apiUrl('api/ws/list?' + new URLSearchParams({ dir })),
        { cache: 'no-store', headers: this._hdr() });
      const j = await r.json();
      return (j.files || []).map((f) => f.name);
    },
    async _srvGet(dir, name) {
      const r = await fetch(this.apiUrl('api/ws/get?' + new URLSearchParams({ dir, name })),
        { headers: this._hdr() });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.blob();
    },
    async _srvDel(dir, name) {
      const r = await fetch(this.apiUrl('api/ws/delete?' + new URLSearchParams({ dir, name })),
        { method: 'DELETE', headers: this._hdr() });
      return r.ok;
    },

    /* ---------------- IndexedDB 小工具 ---------------- */
    async _db() {
      if (this.__db) return this.__db;
      this.__db = await new Promise((res, rej) => {
        const rq = indexedDB.open(IDB_NAME, 2);
        rq.onupgradeneeded = () => {
          const db = rq.result;
          if (!db.objectStoreNames.contains('files')) db.createObjectStore('files', { keyPath: 'path' });
          if (!db.objectStoreNames.contains('results')) db.createObjectStore('results', { keyPath: 'id' });
          if (!db.objectStoreNames.contains(DIR_STORE)) db.createObjectStore(DIR_STORE);
        };
        rq.onsuccess = () => res(rq.result);
        rq.onerror = () => rej(rq.error);
      });
      return this.__db;
    },
    async _metaGet(name) {
      const db = await this._db();
      return new Promise((res, rej) => {
        const rq = db.transaction(DIR_STORE).objectStore(DIR_STORE).get(name);
        rq.onsuccess = () => res(rq.result);
        rq.onerror = () => rej(rq.error);
      });
    },
    async _metaPut(name, val) {
      const db = await this._db();
      return new Promise((res, rej) => {
        const rq = db.transaction(DIR_STORE, 'readwrite')
          .objectStore(DIR_STORE).put(val, name);
        rq.onsuccess = () => res();
        rq.onerror = () => rej(rq.error);
      });
    },

    /* ---------------- 目录选择 / 恢复 ---------------- */
    async pickDir(silent) {
      if (!FS_SUPPORTED) throw new Error(T('wiz.unsupported'));
      const handle = await window.showDirectoryPicker({ mode: 'readwrite' });
      this.dir = handle;
      this.dirName = handle.name;
      await this._metaPut(DIR_KEY, handle);
      for (const sub of SUBDIRS) await handle.getDirectoryHandle(sub, { create: true });
      await this.writeManifest();
      return handle;
    },

    /* OPFS：与目录句柄同一套接口。用于自动测试，或在系统文件选择被拦截时兜底 */
    async useOPFS() {
      if (!navigator.storage || !navigator.storage.getDirectory) {
        throw new Error('浏览器不支持 OPFS');
      }
      const handle = await navigator.storage.getDirectory();
      this.dir = handle;
      this.dirName = 'OPFS（浏览器私有）';
      this.isOPFS = true;
      for (const sub of SUBDIRS) {
        await handle.getDirectoryHandle(sub, { create: true });
      }
      await this.writeManifest();
      return handle;
    },

    async _perm(handle, request) {
      if (!handle) return false;
      const q = await handle.queryPermission({ mode: 'readwrite' });
      if (q === 'granted') return true;
      if (!request) return false;
      return (await handle.requestPermission({ mode: 'readwrite' })) === 'granted';
    },

    async restoreDir(silent) {
      if (!FS_SUPPORTED) return false;
      let handle = null;
      try { handle = await this._metaGet(DIR_KEY); } catch (e) { /* ignore */ }
      if (!handle) return false;
      if (!(await this._perm(handle, !silent))) return false;
      this.dir = handle;
      this.dirName = handle.name;
      for (const sub of SUBDIRS) await handle.getDirectoryHandle(sub, { create: true });
      return true;
    },

    async _dir(sub, create) {
      if (!this.dir) throw new Error(T('ws.noDir'));
      try {
        return await this.dir.getDirectoryHandle(sub, { create: create !== false });
      } catch (e) {
        throw this._mapErr(e);
      }
    },

    /* 把浏览器原生的权限类报错换成可操作的中文/英文提示 */
    _mapErr(e) {
      if (e && (e.name === 'NotAllowedError' || e.name === 'SecurityError')) {
        return new Error(T('ws.permDenied'));
      }
      return (e instanceof Error) ? e : new Error(String(e));
    },

    /* 写目录前确认权限：granted 直接用；prompt 则尝试申请（需用户手势） */
    async _ensureWritable() {
      if (!this.dir) throw new Error(T('ws.noDir'));
      let q = 'prompt';
      try { q = await this.dir.queryPermission({ mode: 'readwrite' }); } catch (e) { /* ignore */ }
      if (q === 'granted') return true;
      if (q === 'prompt') {
        try {
          const r = await this.dir.requestPermission({ mode: 'readwrite' });
          if (r === 'granted') return true;
        } catch (e) { /* 无用户手势时浏览器会拒绝 */ }
      }
      throw new Error(T('ws.permDenied'));
    },

    /* ---------------- 文件读写 ---------------- */
    async writeFile(sub, name, blob) {
      if (this.backend === 'server') return this._srvPut(sub, name, blob);
      if (!this.dir) return false;
      await this._ensureWritable();
      const d = (sub === '.' || !sub) ? this.dir : await this._dir(sub);
      try {
        const fh = await d.getFileHandle(name, { create: true });
        const w = await fh.createWritable();
        await w.write(blob);
        await w.close();
      } catch (e) {
        throw this._mapErr(e);
      }
      return true;
    },

    async deleteFile(sub, name) {
      if (this.backend === 'server') return this._srvDel(sub, name);
      if (!this.dir) return false;
      try {
        const d = await this._dir(sub);
        await d.removeEntry(name);
        return true;
      } catch (e) { return false; }
    },

    async listDir(sub) {
      if (this.backend === 'server') return this._srvList(sub);
      if (!this.dir) return [];
      const out = [];
      try {
        const d = await this._dir(sub);
        for await (const [name, h] of d.entries()) {
          if (h.kind === 'file') out.push(name);
        }
      } catch (e) { /* ignore */ }
      return out.sort();
    },

    async readDirFile(sub, name) {
      if (this.backend === 'server') return this._srvGet(sub, name);
      const d = await this._dir(sub);
      const fh = await d.getFileHandle(name);
      return fh.getFile();
    },

    /* ---------------- 依赖缓存 ---------------- */
    async _vendorList() {
      try {
        const r = await fetch('vendor/manifest.json', { cache: 'no-store' });
        if (!r.ok) return null;
        const j = await r.json();
        return (j.files || []).map((f) => new URL('vendor/' + f, location.href).href);
      } catch (e) { return null; }
    },

    async primeVendorCache() {
      const urls = await this._vendorList();
      if (!urls) return { ok: false, reason: 'no-manifest' };
      const cache = await caches.open('sew-vendor-v1');
      let hit = 0, added = 0;
      for (const u of urls) {
        if (await cache.match(u)) { hit++; continue; }
        try {
          const r = await fetch(u);
          if (r.ok) { await cache.put(u, r); added++; }
        } catch (e) { /* ignore */ }
      }
      return { ok: true, total: urls.length, hit, added };
    },

    /* 把缓存里的依赖镜像到 <workdir>/cache/vendor/**（可跨浏览器长期保存） */
    async mirrorVendorToDir(onProgress) {
      if (!this.dir) return { ok: false, reason: 'no-dir', error: T('ws.noDir') };
      try {
        await this._ensureWritable();
      } catch (e) {
        return { ok: false, reason: 'no-perm', error: String(e.message || e) };
      }
      const urls = await this._vendorList();
      if (!urls) return { ok: false, reason: 'no-manifest' };
      const cache = await caches.open('sew-vendor-v1');
      let n = 0;
      try {
        for (const u of urls) {
          const res = await cache.match(u);
          if (!res) continue;
          const rel = decodeURIComponent(new URL(u).pathname)
            .replace(/^.*?\/vendor\//, '');
          const parts = rel.split('/');
          let dir = await this._dir('cache');
          for (const p of parts.slice(0, -1)) {
            dir = await dir.getDirectoryHandle(p, { create: true });
          }
          const fh = await dir.getFileHandle(parts[parts.length - 1], { create: true });
          const w = await fh.createWritable();
          await w.write(await res.blob());
          await w.close();
          n++;
          if (onProgress && n % 5 === 0) onProgress(n, urls.length);
        }
      } catch (e) {
        return { ok: false, reason: 'write-failed', error: String(this._mapErr(e).message) };
      }
      return { ok: true, written: n, total: urls.length };
    },

    /* 缓存被人清掉时，从工作区目录恢复 */
    async restoreVendorFromDir() {
      if (this.backend === 'server' || !this.dir) return 0;
      let root;
      try {
        root = await this.dir.getDirectoryHandle('cache', { create: false });
        root = await root.getDirectoryHandle('vendor', { create: false });
      } catch (e) { return 0; }
      const cache = await caches.open('sew-vendor-v1');
      let restored = 0;
      const walk = async (dir, prefix) => {
        for await (const [name, h] of dir.entries()) {
          if (h.kind === 'directory') {
            await walk(h, prefix + name + '/');
            continue;
          }
          const url = new URL('vendor/' + prefix + name, location.href).href;
          if (await cache.match(url)) continue;
          const f = await h.getFile();
          await cache.put(url, new Response(await f.arrayBuffer()));
          restored++;
        }
      };
      await walk(root, '');
      return restored;
    },

    async writeManifest() {
      if (!this.dir) return;
      const info = {
        app: 'SEW web',
        created: new Date().toISOString(),
        structure: { templates: '模板 npz', results: '拟合结果 (json+png)', data: 'FITS 文件', cache: '运行依赖副本' },
      };
      await this.writeFile('.', 'workspace.json',
        new Blob([JSON.stringify(info, null, 2)], { type: 'application/json' }));
    },
  };

  /* ================= 启动前：SW + 目录恢复 ================= */
  async function preInit() {
    // Service Worker：缓存 vendor/** 请求（不阻塞启动）
    if ('serviceWorker' in navigator) {
      try {
        const reg = navigator.serviceWorker.register('sw.js', { scope: './' });
        await Promise.race([
          reg,
          new Promise((r) => setTimeout(r, 4000)),
        ]);
      } catch (e) { console.warn('SW 注册失败', e); }
    }
    // 1) 页面由带后端的服务器提供（同源有 /api/ws/*）：算力与工作区就在这台服务器。
    //    这是唯一的“服务器模式”判定——不连接其他机器，也不让算力/工作区跑到访问者本机。
    const srv = await SEWWS.detectServer();
    if (srv) {
      SEWWS.useServer(srv);
      return { restored: true, server: true, needDir: !srv.root };
    }
    // 2) 纯静态页面（无后端）：算力与工作区都在当前浏览器（同源本地）
    // 3) 上次选过的本地目录（未授权则等用户点按钮）
    let restored = false;
    try { restored = await SEWWS.restoreDir(true); } catch (e) { /* ignore */ }
    if (restored) {
      SEWWS.backend = SEWWS.isOPFS ? 'browser' : 'local';
      const n = await SEWWS.restoreVendorFromDir().catch(() => 0);
      if (n) console.log(`从工作区恢复了 ${n} 个依赖文件到缓存`);
    }
    return { restored, server: false };
  }
  /* ================= 首次引导 ================= */
  function buildWizard() {
    if ($('wsWizard')) return $('wsWizard');
    const wrap = document.createElement('div');
    wrap.id = 'wsWizard';
    wrap.className = 'wizard';
    wrap.innerHTML = `
      <div class="wizard-box">
        <h2 data-i18n="wiz.title"></h2>
        <p class="small" data-i18n="wiz.desc"></p>
        <ul class="small wiz-list">
          <li data-i18n="wiz.li1"></li>
          <li data-i18n="wiz.li2"></li>
          <li data-i18n="wiz.li3"></li>
        </ul>
        <div class="wiz-pick">
          <button id="wizPick" class="primary" data-i18n="wiz.pick"></button>
          <button id="wizServer" class="ghost" data-i18n="wiz.server" style="display:none"></button>
          <button id="wizOpfs" class="ghost" data-i18n="wiz.opfs"></button>
          <button id="wizSkip" class="ghost" data-i18n="wiz.skip"></button>
        </div>
        <div id="wizHost" class="small" style="color:#b45309;margin-top:6px"></div>
        <div id="wizInfo" class="small muted"></div>
        <label class="chk small"><input id="wizMirror" type="checkbox" checked/>
          <span data-i18n="wiz.mirror"></span></label>
        <div class="wiz-foot">
          <button id="wizGo" class="primary" data-i18n="wiz.go"></button>
        </div>
      </div>`;
    document.body.appendChild(wrap);
    if (window.applyI18N) window.applyI18N(wrap);
    return wrap;
  }

  function wizardFirstRun() {
    return new Promise((resolve) => {
      const w = buildWizard();
      const info = () => $('wizInfo');
      if (!SEWWS.serverCompute() && $('wizHost')) {
        $('wizHost').textContent = T('wiz.staticOnly');
      }
      $('wizPick').onclick = async () => {
        try {
          const h = await SEWWS.pickDir();
          SEWWS.backend = 'local';
          await SEWWS.setPref('local');
          info().textContent = T('wiz.chosen', { name: h.name });
        } catch (e) {
          info().textContent = (e && e.name === 'AbortError')
            ? T('wiz.pickAbort')
            : String(e.message || e);
        }
      };
      $('wizSkip').onclick = async () => {
        SEWWS.dir = null;
        SEWWS.backend = 'browser';
        await SEWWS.setPref('browser');
        info().textContent = T('wiz.browserOnly');
      };
      if ($('wizOpfs')) {
        $('wizOpfs').onclick = async () => {
          try {
            const h = await SEWWS.useOPFS();
            SEWWS.backend = 'browser';
            await SEWWS.setPref('browser');
            info().textContent = T('wiz.chosen', { name: h.name || 'OPFS' });
          } catch (e) {
            info().textContent = String(e.message || e);
          }
        };
      }
      if ($('wizServer')) {
        const btnS = $('wizServer');
        btnS.style.display = (SEWWS.server || SEWWS._needsToken) ? '' : 'none';
        btnS.onclick = async () => {
          try {
            let srv = SEWWS.server;
            if (!srv) {
              const tok = window.prompt(T('wiz.tokenAsk'), '');
              if (!tok) return;
              SEWWS._token = tok;
              try { localStorage.setItem('sew.ws.token', tok); } catch (e) { /* ignore */ }
              srv = await SEWWS.detectServer();
              if (!srv) throw new Error(T('wiz.tokenBad'));
            }
            SEWWS.useServer(srv);
            info().textContent = T('wiz.serverChosen', { root: srv.root });
          } catch (e) {
            info().textContent = String(e.message || e);
          }
        };
      }
      $('wizGo').onclick = async () => {
        const mirror = $('wizMirror').checked;
        w.remove();
        if (!SEWWS.dir) {
          try { await SEWWS._metaPut('skipWizard', true); } catch (e) { /* ignore */ }
        }
        resolve({ mirror, dir: SEWWS.dir });
      };
      if (!FS_SUPPORTED) {
        $('wizPick').disabled = true;
        info().textContent = T('wiz.unsupported');
      }
    });
  }

  /* 初始化完成后的“保存依赖到工作区” */
  async function afterBoot() {
    const btn = $('wsCacheBtn');
    const status = $('wsDirStatus');
    const refreshStatus = () => {
      if (!status) return;
      if (SEWWS.backend === 'server') {
        status.textContent = T('ws.srvAt', { root: SEWWS.dirName });
      } else {
        status.textContent = SEWWS.dir
          ? T('ws.dirAt', { name: SEWWS.dirName })
          : T('ws.dirNone');
      }
    };
    refreshStatus();
    const hint = $('wsHint');
    if (hint) {
      hint.textContent = SEWWS.backend === 'server'
        ? T('ws.hint.srv', { root: SEWWS.dirName })
        : T('ws.hint');
    }
    // 服务器工作区不需要把依赖镜像到目录（浏览器缓存已经够用）
    if (btn && SEWWS.backend === 'server') {
      btn.style.display = 'none';
    }

    if (btn) {
      btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = T('ws.caching');
        try {
          if (!SEWWS.dir) await SEWWS.pickDir(false).then(refreshStatus);
          const p = await SEWWS.primeVendorCache();
          const m = await SEWWS.mirrorVendorToDir((n, total) => {
            btn.textContent = `${n}/${total}`;
          });
          if (!m.ok) {
            btn.textContent = T('ws.cacheFail');
            if (window.toast) window.toast(m.error || T('ws.cacheFail'), 'err');
            return;
          }
          btn.textContent = T('ws.cached');
          if (window.toast) {
            window.toast(T('ws.cacheDone', { n: m.written || (p && p.added) || 0 }));
          }
        } catch (e) {
          btn.textContent = T('ws.cacheFail');
          if (window.toast) window.toast(String(e.message || e), 'err');
        } finally {
          btn.disabled = false;
          refreshStatus();
        }
      };
    }
  }

  /* ================= 工作区设置面板（点左上角 SEWweb） ================= */
  function currentLabel() {
    if (SEWWS.backend === 'server') {
      return SEWWS.dirName ? T('ws.srvAt', { root: SEWWS.dirName })
        : T('ws.unset');
    }
    if (SEWWS.backend === 'local') return T('ws.dirAt', { name: SEWWS.dirName });
    return T('ws.dirNone');
  }

  /* 顶部状态栏：算力 · 工作区（i18n 就绪后再设，避免出现 key） */
  function applyHostStatus() {
    if (typeof window.setStatus !== 'function') return;
    if (SEWWS.serverCompute()) {
      window.setStatus(T('hdr.compute.srv',
        { root: SEWWS.dirName || T('ws.unsetShort') }));
    } else if (SEWWS.dirName) {
      window.setStatus(T('hdr.compute.loc', { root: SEWWS.dirName }));
    }
  }

  let srvWhenOpened = null;   // 打开设置面板时的算力归属（用于判断是否需要重载）
  async function refreshAfterSwitch(msg) {
    // 算力归属变了（服务器 ↔ 浏览器）：Pyodide 内核启停状态不同，整页重载最干净
    if (srvWhenOpened !== null && SEWWS.serverCompute() !== srvWhenOpened) {
      location.reload();
      return;
    }
    try { if (window.sewRescanWorkspace) await window.sewRescanWorkspace(); }
    catch (e) { console.warn(e); }
    try { if (typeof afterBoot === 'function') await afterBoot(); }
    catch (e) { console.warn(e); }
    // 状态栏跟着工作区走
    applyHostStatus();
    if (window.toast) window.toast(msg || T('set.changed'));
  }

  /* ---------------- 服务器目录浏览（选工作区用） ---------------- */
  async function browseDirs(path) {
    const r = await fetch(SEWWS.apiUrl('api/ws/browse?' +
      new URLSearchParams({ path: path || '' })),
      { cache: 'no-store', headers: SEWWS._hdr() });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  }

  function openDirPicker(start, onPick) {
    const old = $('wsDirPicker');
    if (old) old.remove();
    const el = document.createElement('div');
    el.id = 'wsDirPicker';
    el.className = 'modal';
    el.innerHTML = `
      <div class="modal-box">
        <div class="modal-head">
          <h3>${T('set.pickDir')}</h3>
          <button id="dpClose" class="ghost" data-i18n="modal.close"></button>
        </div>
        <div class="row">
          <input id="dpPath" type="text" spellcheck="false"/>
          <button id="dpGo" class="ghost" data-i18n="modal.go"></button>
          <button id="dpUp" class="ghost" data-i18n="modal.up"></button>
        </div>
        <div id="dpMsg" class="small" style="color:#b45309;margin-top:2px"></div>
        <div class="browselist"><div id="dpDirs"></div></div>
        <div class="wiz-foot" style="display:flex;gap:8px;margin-top:8px">
          <button id="dpPick" class="primary" data-i18n="set.useThis"></button>
          <button id="dpCancel" class="ghost" data-i18n="modal.close"></button>
        </div>
      </div>`;
    document.body.appendChild(el);
    if (window.applyI18N) window.applyI18N(el);

    const shown = { path: '' };
    const close = () => el.remove();
    $('dpClose').onclick = close;
    $('dpCancel').onclick = close;
    el.addEventListener('click', (e) => { if (e.target === el) close(); });

    const render = async (path) => {
      $('dpMsg').textContent = '';
      try {
        const j = await browseDirs(path);
        shown.path = j.path;
        $('dpPath').value = j.path;
        const box = $('dpDirs');
        box.innerHTML = '';
        if (!j.dirs.length) {
          box.innerHTML = `<div class="item muted">${T('ws.empty')}</div>`;
        }
        j.dirs.forEach((d) => {
          const it = document.createElement('div');
          it.className = 'item dir';
          it.textContent = '\u{1F4C1} ' + d.name;
          it.onclick = () => render(d.path);
          box.appendChild(it);
        });
      } catch (e) {
        $('dpMsg').textContent = String(e.message || e);
      }
    };
    $('dpGo').onclick = () => render($('dpPath').value.trim());
    $('dpPath').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') render($('dpPath').value.trim());
    });
    $('dpUp').onclick = async () => {
      if (!shown.path) return;
      const parent = shown.path.replace(/\/+$/, '').replace(/\/[^/]+$/, '') || '/';
      render(parent);
    };
    $('dpPick').onclick = () => {
      const p = ($('dpPath').value || shown.path || '').trim();
      if (!p) return;
      close();
      onPick(p);
    };
    render(start || '');
  }

  async function openSettings() {
    srvWhenOpened = SEWWS.serverCompute();
    const old = $('wsSettings');
    if (old) old.remove();
    const el = document.createElement('div');
    el.id = 'wsSettings';
    el.className = 'wizard';
    const onServer = SEWWS.backend === 'server';
    // 只展示“当前宿主的工作区”：页面由服务器提供时就是服务器目录，
    // 纯静态页面时才是浏览器/本地目录（避免在 SSH 页面里误选本机目录）。
    const sec = onServer
      ? `
        <div class="set-sec">
          <div class="set-h" data-i18n="set.workdir"></div>
          <div class="small muted" data-i18n="set.workdirHint"></div>
          <div class="row" style="margin-top:6px">
            <input id="setSrvPath" type="text" style="flex:1;min-width:0"/>
            <button id="setSrvBrowse" class="ghost" data-i18n="set.browse"></button>
            <button id="setSrvApply" class="primary" data-i18n="set.apply"></button>
          </div>
          <div id="setSrvWhere" class="small muted" style="margin-top:4px"></div>
        </div>`
      : `
        <div class="set-sec">
          <div class="set-h" data-i18n="set.local"></div>
          <div class="small muted" id="setStaticHint"></div>
          <div class="row">
            <button id="setLocal" class="ghost" data-i18n="wiz.pick"></button>
            <button id="setOpfs" class="ghost" data-i18n="wiz.opfs"></button>
            <button id="setBrowser" class="ghost" data-i18n="wiz.skip"></button>
          </div>
        </div>`;
    el.innerHTML = `
      <div class="wizard-box">
        <h2 data-i18n="set.title"></h2>
        <p class="small"><span data-i18n="set.current"></span>
          <b id="setCur"></b></p>
        ${sec}
        <div class="wiz-foot">
          <button id="setClose" class="ghost" data-i18n="modal.close"></button>
        </div>
      </div>`;
    document.body.appendChild(el);
    if (window.applyI18N) window.applyI18N(el);

    const cur = $('setCur');
    const paint = () => { cur.textContent = currentLabel(); };
    paint();
    const sh = $('setStaticHint');
    if (sh) {
      sh.textContent = T('set.staticHint', { origin: location.origin })
        + ' ' + T('wiz.staticOnly');
    }

    const close = () => el.remove();
    $('setClose').onclick = close;
    el.addEventListener('click', (e) => { if (e.target === el) close(); });

    if (onServer) {
      const pathInp = $('setSrvPath');
      if (pathInp) pathInp.value = (SEWWS.dirName)
        || (SEWWS.server && SEWWS.server.root) || '';
      const where = $('setSrvWhere');
      if (where) {
        where.textContent = T('set.serverAt', { url: location.origin });
      }

      $('setSrvBrowse').onclick = () => {
        const start = ($('setSrvPath').value || '').trim();
        openDirPicker(start, (p) => {
          const inp = $('setSrvPath');
          if (inp) inp.value = p;
        });
      };

      $('setSrvApply').onclick = async () => {
        const path = ($('setSrvPath').value || '').trim();
        if (!path) return;
        const btn = $('setSrvApply');
        btn.disabled = true;
        try {
          await SEWWS.setServerRoot(path);
          paint();
          await refreshAfterSwitch();
        } catch (e) {
          if (window.toast) window.toast(T('set.badRoot', { msg: e.message || e }), 'err');
        } finally { btn.disabled = false; }
      };
      return;
    }
    $('setLocal').onclick = async () => {
      try {
        const h = await SEWWS.pickDir();
        SEWWS.backend = 'local';
        SEWWS.dirName = h.name;
        await SEWWS.setPref('local');
        paint();
        await refreshAfterSwitch();
      } catch (e) { /* 用户取消 */ }
    };

    $('setOpfs').onclick = async () => {
      try {
        await SEWWS.useOPFS();
        SEWWS.backend = 'browser';
        await SEWWS.setPref('browser');
        paint();
        await refreshAfterSwitch();
      } catch (e) {
        if (window.toast) window.toast(String(e.message || e), 'err');
      }
    };

    $('setBrowser').onclick = async () => {
      SEWWS.dir = null;
      SEWWS.backend = 'browser';
      SEWWS.dirName = '';
      await SEWWS.setPref('browser');
      paint();
      await refreshAfterSwitch();
    };
  }
  window.sewOpenSettings = openSettings;

  const preInitP = preInit();
  window.__sewPreInit = preInitP;   // 只包含 SW 注册 + 工作区探测（不等内核）

  SEWWS.ready = preInitP.then(async (st) => {
    const restored = !!(st && st.restored);
    const serverMode = !!(st && st.server);
    const needDir = !!(st && st.needDir);
    // 等 i18n 加载完再写状态栏
    setTimeout(applyHostStatus, 60);
    setTimeout(applyHostStatus, 1500);
    if (needDir) {
      // 服务器模式但没有工作区目录：引导用户设置（不自动创建任何目录）
      setTimeout(applyHostStatus, 60);
      setTimeout(() => { openSettings().catch(() => {}); }, 300);
    }
    // 第一次进入（没有服务器工作区、没有已授权目录、且没选过“仅浏览器存储”）→ 弹引导
    let wantMirror = false;
    let skipped = false;
    try { skipped = !!(await SEWWS._metaGet('skipWizard')); } catch (e) { /* ignore */ }
    if (!restored && !skipped) {
      const r = await wizardFirstRun();
      wantMirror = r.mirror && (SEWWS.backend === 'local');
    } else if (restored) {
      wantMirror = SEWWS.backend === 'local';
    }
    if (SEWWS.dir || SEWWS.backend === 'server') {
      // 刚选定工作区：把面板状态与已有文件同步过来
      try {
        if (window.sewRescanWorkspace) await window.sewRescanWorkspace();
        if (typeof afterBoot === 'function') await afterBoot();
      } catch (e) { console.warn(e); }
    }
    if (wantMirror && SEWWS.dir) {
      setTimeout(async () => {
        try {
          // 无用户手势：只在权限已授权时自动镜像，否则等用户主动点按钮
          if (!(await SEWWS._perm(SEWWS.dir, false))) return;
          await SEWWS.primeVendorCache();
          await SEWWS.mirrorVendorToDir();
        } catch (e) { console.warn(e); }
      }, 3000);
    }
    return SEWWS.dir || SEWWS.server;
  });

  window.SEWWS = SEWWS;
  document.addEventListener('DOMContentLoaded', () => {
    const brand = document.getElementById('brandBtn');
    if (brand) {
      brand.addEventListener('click', () => openSettings());
      brand.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openSettings(); }
      });
    }
    SEWWS.ready.then(afterBoot).catch((e) => console.warn(e));
  });
})();
