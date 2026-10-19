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

  const SEWWS = {
    supported: FS_SUPPORTED,
    dir: null,           // FileSystemDirectoryHandle
    dirName: '',
    ready: null,         // Promise
    autoSaveVendor: true,

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
      if (!FS_SUPPORTED) throw new Error('浏览器不支持目录访问');
      const handle = await window.showDirectoryPicker({ mode: 'readwrite' });
      this.dir = handle;
      this.dirName = handle.name;
      await this._metaPut(DIR_KEY, handle);
      for (const sub of SUBDIRS) await handle.getDirectoryHandle(sub, { create: true });
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
      if (!this.dir) throw new Error('未设置工作区');
      return this.dir.getDirectoryHandle(sub, { create: create !== false });
    },

    /* ---------------- 文件读写 ---------------- */
    async writeFile(sub, name, blob) {
      if (!this.dir) return false;
      const d = await this._dir(sub);
      const fh = await d.getFileHandle(name, { create: true });
      const w = await fh.createWritable();
      await w.write(blob);
      await w.close();
      return true;
    },

    async deleteFile(sub, name) {
      if (!this.dir) return false;
      try {
        const d = await this._dir(sub);
        await d.removeEntry(name);
        return true;
      } catch (e) { return false; }
    },

    async listDir(sub) {
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
      if (!this.dir) return { ok: false, reason: 'no-dir' };
      const urls = await this._vendorList();
      if (!urls) return { ok: false, reason: 'no-manifest' };
      const cache = await caches.open('sew-vendor-v1');
      let n = 0;
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
      return { ok: true, written: n, total: urls.length };
    },

    /* 缓存被人清掉时，从工作区目录恢复 */
    async restoreVendorFromDir() {
      if (!this.dir) return 0;
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
    // 恢复上次的工作区目录（未授权则等用户点按钮）
    let restored = false;
    try { restored = await SEWWS.restoreDir(true); } catch (e) { /* ignore */ }
    if (restored) {
      const n = await SEWWS.restoreVendorFromDir().catch(() => 0);
      if (n) console.log(`从工作区恢复了 ${n} 个依赖文件到缓存`);
    }
    return restored;
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
          <button id="wizSkip" class="ghost" data-i18n="wiz.skip"></button>
        </div>
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
      $('wizPick').onclick = async () => {
        try {
          const h = await SEWWS.pickDir();
          info().textContent = T('wiz.chosen', { name: h.name });
        } catch (e) {
          info().textContent = String(e.message || e);
        }
      };
      $('wizSkip').onclick = () => {
        SEWWS.dir = null;
        info().textContent = T('wiz.browserOnly');
      };
      $('wizGo').onclick = async () => {
        const mirror = $('wizMirror').checked;
        w.remove();
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
      status.textContent = SEWWS.dir
        ? T('ws.dirAt', { name: SEWWS.dirName })
        : T('ws.dirNone');
    };
    refreshStatus();

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

  SEWWS.ready = preInit().then(async (restored) => {
    // 第一次进入（没有已授权目录）→ 弹引导
    let wantMirror = false;
    if (!restored) {
      const r = await wizardFirstRun();
      wantMirror = r.mirror && !!SEWWS.dir;
    } else {
      wantMirror = true;
    }
    if (wantMirror && SEWWS.dir) {
      // 首次/恢复后都确保依赖已缓存并镜像（不阻塞启动）
      setTimeout(async () => {
        try {
          await SEWWS.primeVendorCache();
          await SEWWS.mirrorVendorToDir();
        } catch (e) { console.warn(e); }
      }, 3000);
    }
    return SEWWS.dir;
  });

  window.SEWWS = SEWWS;
  window.__sewPreInit = SEWWS.ready;
  document.addEventListener('DOMContentLoaded', () => {
    SEWWS.ready.then(afterBoot).catch((e) => console.warn(e));
  });
})();
