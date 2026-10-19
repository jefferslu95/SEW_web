/* ------------------------------------------------------------------
   SEW web — 轻量国际化（i18n）
   语言文件存放在 /static/i18n/<lang>.json，代码里只保留 key，不硬编码文字。
   用法：
     t('hdr.open')                     -> 当前语言文本
     t('status.loaded', {name, kind})  -> 占位符替换
     HTML: data-i18n="key" / data-i18n-html="key"
           data-i18n-ph="key"（placeholder）/ data-i18n-title="key"（title）
   切换语言：I18N.setLang('en')
   ------------------------------------------------------------------ */
(function () {
  const STORAGE_KEY = 'sew.lang';
  const FALLBACK_LANG = 'zh';

  const I18N = {
    lang: FALLBACK_LANG,
    dict: {},
    fallback: {},
    _cache: {},

    async _fetch(lang) {
      if (!this._cache[lang]) {
        const r = await fetch(`/static/i18n/${lang}.json`, { cache: 'no-cache' });
        if (!r.ok) throw new Error(`i18n: cannot load ${lang}.json (HTTP ${r.status})`);
        this._cache[lang] = await r.json();
      }
      return this._cache[lang];
    },

    /* 载入语言并应用到静态 DOM */
    async setLang(lang, opts) {
      if (!lang) lang = FALLBACK_LANG;
      if (!this.fallback || !Object.keys(this.fallback).length) {
        this.fallback = await this._fetch(FALLBACK_LANG);
      }
      this.dict = await this._fetch(lang);
      this.lang = lang;
      try { localStorage.setItem(STORAGE_KEY, lang); } catch (e) { /* ignore */ }
      document.documentElement.lang = lang === 'zh' ? 'zh-CN' : lang;
      applyI18N(document);
      const sel = document.getElementById('langSel');
      if (sel) sel.value = lang;
      if (typeof window.onLangChanged === 'function') {
        try { window.onLangChanged(lang); } catch (e) { console.warn(e); }
      }
      return lang;
    },

    /* 初始化：读取上次选择（默认 zh） */
    async init() {
      let lang = FALLBACK_LANG;
      try { lang = localStorage.getItem(STORAGE_KEY) || FALLBACK_LANG; } catch (e) { /* ignore */ }
      try {
        await this.setLang(lang);
      } catch (e) {
        console.warn(e);
      }
    },

    /* 取文本；缺失时回退到中文，再回退到 key 本身 */
    t(key, vars) {
      let s = this.dict[key];
      if (s == null) s = this.fallback[key];
      if (s == null) s = key;
      if (vars) {
        for (const k in vars) s = s.split(`{${k}}`).join(vars[k]);
      }
      return s;
    }
  };

  function t(key, vars) { return I18N.t(key, vars); }
  window.I18N = I18N;
  window.t = t;

  /* 把 data-i18n* 属性写回 DOM */
  function applyI18N(root) {
    root = root || document;
    root.querySelectorAll('[data-i18n]').forEach(el => {
      el.textContent = t(el.dataset.i18n);
    });
    root.querySelectorAll('[data-i18n-html]').forEach(el => {
      el.innerHTML = t(el.dataset.i18nHtml);
    });
    root.querySelectorAll('[data-i18n-ph]').forEach(el => {
      el.placeholder = t(el.dataset.i18nPh);
    });
    root.querySelectorAll('[data-i18n-title]').forEach(el => {
      el.title = t(el.dataset.i18nTitle);
    });
    document.title = t('doc.title');
  }
  window.applyI18N = applyI18N;

  /* 语言选择框（若页面已包含 #langSel） */
  document.addEventListener('DOMContentLoaded', () => {
    const sel = document.getElementById('langSel');
    if (sel) {
      sel.addEventListener('change', () => I18N.setLang(sel.value));
    }
  });
})();
