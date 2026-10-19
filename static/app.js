/* SEW web front-end — 无外部依赖（Plotly 由后端本地提供） */
'use strict';

const $ = (id) => document.getElementById(id);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

const S = {
  path: null,
  meta: null,
  image: null,
  spec: null,
  target: { x: null, y: null, row: 0, aperture: 0, bin: null },
  bins: null,
  binsIndex: null,
  sky: null,
  templates: [],
  lines: null,           // /api/lines 结果 {lines:[{lam,name,kind}], fit_em:[{lam,name}]}
  plotW: 0,              // 光谱图绘图区实际宽度（第一次渲染后回填）
  fwhm: null,
  jobId: null,
  timer: null,
  result: null,
  view: 'empty',
  mapGeom: null,
  panelH: null,          // 手动拖动后的三栏高度（null = 跟随拟合设置卡片）
  _browsePick: null,     // 浏览弹窗的文件选择回调（用于选 LSF 参考文件）
};

// 浏览器端（Pyodide）恢复历史结果时需要把 jobId 写回当前状态
window.sewSetJobId = (id) => { S.jobId = id; };

/* ------------------------------------------------------------------ */
/* 基础工具                                                            */
/* ------------------------------------------------------------------ */
function toast(msg, kind = '') {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast' + (kind === 'err' ? ' err' : '');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add('hidden'), 4200);
}

function setStatus(s, err = false) {
  const el = $('topStatus');
  el.textContent = s || '';
  el.className = 'status' + (err ? ' err' : '');
}

async function api(url, opts) {
  // 浏览器端（Pyodide）模式：由 rpc.js 提供 window.sewApi，不访问服务器
  if (typeof window.sewApi === 'function') return window.sewApi(url, opts);
  const r = await fetch(url, opts);
  let j;
  try { j = await r.json(); } catch (e) {
    throw new Error(`HTTP ${r.status}`);
  }
  if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

const postJSON = (url, body) => api(url, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
});

const fmt = (v, n = 4) => {
  if (v === null || v === undefined || !isFinite(v)) return '—';
  const a = Math.abs(v);
  if (a !== 0 && (a < 1e-3 || a >= 1e5)) return v.toExponential(2);
  return Number(v).toFixed(n);
};

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ------------------------------------------------------------------ */
/* viridis 颜色表                                                      */
/* ------------------------------------------------------------------ */
const VIRIDIS = [
  [68, 1, 84], [72, 40, 120], [62, 74, 137], [49, 104, 142],
  [38, 130, 142], [31, 158, 137], [53, 183, 121], [109, 205, 89],
  [180, 222, 44], [253, 231, 37],
];
function viridis(t) {
  t = Math.max(0, Math.min(1, t));
  const p = t * (VIRIDIS.length - 1);
  const i = Math.floor(p), f = p - i;
  const a = VIRIDIS[i], b = VIRIDIS[Math.min(i + 1, VIRIDIS.length - 1)];
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f,
          a[2] + (b[2] - a[2]) * f];
}

/* ------------------------------------------------------------------ */
/* 初始化                                                              */
/* ------------------------------------------------------------------ */
document.addEventListener('DOMContentLoaded', async () => {
  await I18N.init();
  bindEvents();
  try {
    const h = await api('/api/health');
    setStatus(`pPXF ${h.ppxf} · ppxf-sew ${h.ppxf_sew}`);
  } catch (e) {
    setStatus(t('status.backend', { msg: e.message }), true);
  }
  await loadTemplates();
  await loadLines();
  try {
    const s = await api('/api/samples');
    if (s.samples && s.samples.length) {
      $('pathInput').placeholder = t('hdr.sample', { path: s.samples[0] });
      // 启动期异步返回：若用户已打开/正在浏览文件，不要覆盖输入框
      if (!S.path && !$('pathInput').value) $('pathInput').value = s.samples[0];
    }
  } catch (e) { /* ignore */ }
  window.addEventListener('resize', () => {
    syncPanelHeight();
    if (S.image) drawMap();
  });
  // 面板高度：上为展示区，下为四列设置区（默认 42%，可拖拽）
  syncPanelHeight();
  // 双保险：DOMContentLoaded 时机可能早于字体/布局稳定
  window.addEventListener('load', syncPanelHeight);
  setTimeout(syncPanelHeight, 60);
});

/* 语言切换后重渲染动态生成的文本（静态部分由 applyI18N 处理） */
window.onLangChanged = () => {
  if (S.meta) renderMeta(S.meta);
  updateDlamUI();
  updateDustUI();
  updateTargetInfo();
  updateApHint();
  loadTemplates();
  if (S.spec) drawSpectrum(S.spec);
  if (S.result) renderResult(S.result, S._fitTarget);
};

function syncPanelHeight() {
  const headerH = document.querySelector('header').getBoundingClientRect().height;
  const avail = Math.max(420, window.innerHeight - headerH - 20);
  let h;
  if (S.panelH != null) {
    h = S.panelH;
  } else {
    // 默认底部四列区 ≈ 42% 可用高度（上：展示区）
    h = Math.round(Math.max(230, Math.min(0.42 * avail, avail - 260)));
  }
  h = Math.round(Math.max(180, Math.min(h, avail - 240)));
  document.documentElement.style.setProperty('--panel-h', h + 'px');
}

function browseStart() {
  if (S.path) return S.path.replace(/[^/]*$/, '');
  const W = window.SEWWS;
  if (W && W.backend === 'server' && W.dirName) return W.dirName;
  return '~';
}

function bindEvents() {
  $('openBtn').onclick = () => openPath($('pathInput').value.trim());
  $('pathInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') openPath($('pathInput').value.trim());
  });
  $('browseBtn').onclick = () => openBrowse(browseStart());
  $('fileInput').onchange = onUploadFile;

  $('applyOverride').onclick = applyOverride;
  $('applyView').onclick = async () => {
    if (S.meta && S.meta.kind === 'cube') {
      await loadImage();
      // 按当前取谱方式：bin 模式才重算 PowerBin
      if (useBin()) await loadBins(false);
    }
    if (S.meta) {
      if (S.meta.kind === 'cube' && S.target.x === null) {
        S.target.x = Math.floor(S.meta.nx / 2);
        S.target.y = Math.floor(S.meta.ny / 2);
      }
      // 孔径/波段可能变化 → 旧拟合不再对应当前谱，清掉叠加
      if (S.result) resetResultPanel();
      if (S.target.x !== null || S.meta.kind === 'spectrum') await loadSpectrum();
    }
  };
  $('binSel').onchange = () => {
    selectBin(parseInt($('binSel').value, 10));
  };
  $('binPrev').onclick = () => stepBin(-1);
  $('binNext').onclick = () => stepBin(1);
  $('useBin').onchange = async () => {
    updateCoAddUI();
    if (!S.meta || S.meta.kind !== 'cube') return;
    if (useBin()) {
      if (S.bins) populateBinSel(S.target.bin);
      else await loadBins(true);
    }
    if (S.result) resetResultPanel();
    drawMap();
    await loadSpectrum(false);
  };
  $('binSN').addEventListener('change', async () => {
    if (!S.meta || S.meta.kind !== 'cube') return;
    await loadBins(true);
    if (S.result) resetResultPanel();
    await loadSpectrum(false);
  });
  $('showBins').onchange = () => drawMap();
  $('fullBand').onclick = () => {
    if (!S.meta || !S.meta.wave) return;
    $('bandMin').value = S.meta.wave.min.toFixed(2);
    $('bandMax').value = S.meta.wave.max.toFixed(2);
    $('cminIn').value = ''; $('cmaxIn').value = '';
    syncSliders();
  };
  $('imgMode').onchange = () => { if (S.meta && S.meta.kind === 'cube') loadImage(); };
  $('stretchSel').onchange = () => drawMap();
  $('fwhmMode').onchange = updateFwhmModeUI;
  $('dlamModeSel').onchange = updateDlamUI;
  $('dlamIn').oninput = updateDlamUI;
  $('dustModeSel').onchange = updateDustUI;
  $('dustOnSel').onchange = updateDustUI;
  $('dustAvMin').oninput = updateDustUI;
  $('dustAvMax').oninput = updateDustUI;
  $('engineSel').onchange = updateDustUI;
  $('templateSel').onchange = updateDlamUI;
  $('fitMin').oninput = updateDlamUI;
  $('fitMax').oninput = updateDlamUI;
  $('obsVacSel').onchange = () => {
    try { localStorage.setItem('sew_obs_vac', $('obsVacSel').value); }
    catch (e) { /* ignore */ }
  };
  $('fwhmLsfBrowse').onclick = () => {
    const cur = $('fwhmLsfPath').value || '';
    openBrowse(cur ? cur.replace(/[^/]*$/, '') : browseStart(),
               (p) => { $('fwhmLsfPath').value = p; });
  };
  $('showMask').onchange = () => { if (S.spec) drawSpectrum(S.spec); };
  $('showErr').onchange = () => { if (S.spec) drawSpectrum(S.spec); };
  $('waveRestChk').onchange = () => { if (S.spec) drawSpectrum(S.spec); };
  $('lineSel').onchange = () => { if (S.spec) drawSpectrum(S.spec); };

  // 展示区 tab
  $$('#displayTabs button').forEach(b => {
    b.onclick = () => showView(b.dataset.view);
  });

  // 拟合结果 tab
  $$('#resultTabsWrap button').forEach(b => {
    b.onclick = () => showResultTab(b.dataset.rtab);
  });

  $('fitBtn').onclick = startFit;
  $('autoZBtn').onclick = autoRedshift;
  initSliders();
  // 孔径改变时立即重画取谱范围，并自动重取光谱（防抖）
  ['aperture', 'apertureR'].forEach(id => {
    const e = $(id);
    if (e) e.addEventListener('input', () => {
      updateApHint();
      if (S.meta && S.meta.kind === 'cube' && S.image && !useBin()) drawMap();
      scheduleApertureUpdate();
    });
  });
  $('apUnit').onchange = () => {
    const old = S.apUnit || 'pix';
    const now = $('apUnit').value;
    const sc = apScale();
    let v = parseFloat($('aperture').value) || 0;
    if (sc) {
      if (old === 'pix' && now === 'arcsec') v = v * sc;
      else if (old === 'arcsec' && now === 'pix') v = v / sc;
    }
    S.apUnit = now;
    setupApSlider();
    $('aperture').step = now === 'pix' ? '0.5' : '0.05';
    $('aperture').value = (Math.round(v * 100) / 100).toString();
    $('apertureR').value = $('aperture').value;
    updateApHint();
    if (S.meta && S.meta.kind === 'cube' && S.image && !useBin()) drawMap();
    scheduleApertureUpdate();
  };

  // 数据 / 显示卡片折叠
  $$('.collapse-btn').forEach(btn => {
    btn.onclick = () => {
      const body = $(btn.dataset.target);
      const nowHidden = body.classList.toggle('hidden');
      btn.textContent = nowHidden ? '▸' : '▾';
    };
  });

  // 刷新按钮悬浮提示（有拟合结果时）
  const refreshBtn = $('applyView'), rtip = $('refreshTip');
  refreshBtn.addEventListener('mouseenter', () => {
    if (!S.result) { rtip.classList.add('hidden'); return; }
    const r = refreshBtn.getBoundingClientRect();
    const half = (rtip.offsetWidth || 170) / 2;
    const cx = Math.max(half + 8,
      Math.min(r.left + r.width / 2, window.innerWidth - half - 8));
    rtip.style.left = cx + 'px';
    rtip.style.top = (r.top - 6) + 'px';
    rtip.classList.remove('hidden');
  });
  refreshBtn.addEventListener('mouseleave', () => rtip.classList.add('hidden'));

  initSplitter();
  updateFwhmModeUI();
  updateDlamUI();
  updateDustUI();
  updateCoAddUI();
  refreshApUI();
  try {
    const ov = localStorage.getItem('sew_obs_vac');
    if (ov && ['air', 'vacuum'].includes(ov)) $('obsVacSel').value = ov;
  } catch (e) { /* ignore */ }
  // 结果图点击在新标签页放大
  ['nativeImg', 'weightsImg'].forEach(id => {
    $(id).onclick = () => {
      const src = $(id).src;
      if (src) window.open(src, '_blank');
    };
  });

  $('tplDownloadBtn').onclick = async () => {
    const url = ($('tplUrlInput').value || '').trim();
    if (!url) {
      setTplMsg(t('tpl.msg.needUrl'));
      return toast(t('tpl.msg.needUrl'), 'err');
    }
    const typed = ($('tplNameInput').value || '').trim();
    const name = typed || ($('templateSel').value || 'template');
    const btn = $('tplDownloadBtn');
    btn.disabled = true;
    btn.textContent = t('tpl.msg.downloading');
    setTplMsg(t('tpl.msg.dling'));
    try {
      const j = await postJSON('/api/templates/download', { name, url });
      const fn = String(j.path || '').split('/').pop();
      setTplMsg(t('tpl.msg.saved', { fn }));
      await loadTemplates();
      const short = guessTplName(fn);
      const sel = $('templateSel');
      if (short && [...sel.options].some(o => o.value === short)) sel.value = short;
      toast(t('tpl.msg.ready', { fn }));
    } catch (e) {
      setTplMsg(t('tpl.msg.dlfail', { msg: e.message }));
      toast(e.message, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = t('tpl.download');
    }
  };
  $('tplRefreshBtn').onclick = async () => {
    await loadTemplates();
    toast(t('tpl.msg.refreshed'));
  };
  $('tplUploadInput').onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const name = ($('tplNameInput').value || '').trim();
    const fd = new FormData();
    fd.append('file', f);
    if (name) fd.append('name', name);
    setTplMsg(t('tpl.msg.uploading'));
    try {
      const j = await api('/api/templates/upload', { method: 'POST', body: fd });
      setTplMsg(t('tpl.msg.uploaded', { fn: String(j.path || '').split('/').pop() }));
      await loadTemplates();
      toast(t('tpl.msg.uploadDone'));
    } catch (err) {
      setTplMsg(t('tpl.msg.uploadFail', { msg: err.message }));
      toast(err.message, 'err');
    } finally {
      e.target.value = '';
    }
  };

  // HDU 变更时刷新对应的 BINTABLE 列选择
  [['ovFlux', 'ovFluxCol'], ['ovVar', 'ovVarCol'],
   ['ovMask', 'ovMaskCol'], ['ovWaveHdu', 'ovWaveCol']].forEach(([h, c]) => {
    $(h).onchange = () => fillColSelect($(c), S.meta, $(h).value, '');
  });

  // 文件浏览
  $('browseClose').onclick = () => {
    S._browsePick = null;
    $('browseModal').classList.add('hidden');
  };
  $('browseGo').onclick = () => openBrowse($('browsePath').value.trim());
  $('browseUp').onclick = () => {
    if (S._browseParent) openBrowse(S._browseParent);
  };
  $('browsePath').addEventListener('keydown', e => {
    if (e.key === 'Enter') openBrowse($('browsePath').value.trim());
  });

  // 地图点击
  $('mapCanvas').onclick = onMapClick;
  $('mapCanvas').addEventListener('mousemove', (e) => {
    if (!S.meta || S.meta.kind !== 'cube' || !S.image) return;
    const c = e.target, r = c.getBoundingClientRect();
    const cx = (e.clientX - r.left) * c.width / r.width;
    const cy = (e.clientY - r.top) * c.height / r.height;
    const x = Math.floor(cx * S.meta.nx / c.width);
    const y = S.meta.ny - 1 - Math.floor(cy * S.meta.ny / c.height);
    if (x < 0 || x >= S.meta.nx || y < 0 || y >= S.meta.ny) return;
    updateSkyReadout(x, y);
  });
  $('mapCanvas').addEventListener('mouseleave', () =>
    updateSkyReadout(S.target.x, S.target.y));
}

/* 速度网格 dlam（dex/px）→ velscale = c·ln10·dlam */
const C_KMS = 299792.458;

function obsDlam() {
  const sp = S.spec;
  if (!sp || !sp.wave || sp.wave.length < 3) return null;
  let lo = parseFloat($('fitMin').value), hi = parseFloat($('fitMax').value);
  if (!isFinite(lo)) lo = -Infinity;
  if (!isFinite(hi)) hi = Infinity;
  const w = [];
  for (let i = 0; i < sp.wave.length; i++) {
    const v = sp.wave[i];
    if (v >= lo && v <= hi && isFinite(v) && v > 0) w.push(v);
  }
  if (w.length < 3) return null;
  const d = [];
  for (let i = 1; i < w.length; i++) {
    if (w[i] > 0 && w[i - 1] > 0) d.push(Math.log(w[i] / w[i - 1]));
  }
  if (!d.length) return null;
  d.sort((a, b) => a - b);
  return d[Math.floor(d.length / 2)] / Math.LN10;
}

// 观测谱是否已经是均匀 log 网格（Δlnλ 恒定）
// 注：后端返回的波长只保留 6 位小数，Δlnλ 的相对散布会被放大到 ~1e-5，
//     所以这里用 1e-4 的阈值（线性网格的散布通常在 0.1 量级）。
function isLogGrid() {
  const sp = S.spec;
  if (!sp || !sp.wave || sp.wave.length < 4) return false;
  let lo = parseFloat($('fitMin').value), hi = parseFloat($('fitMax').value);
  if (!isFinite(lo)) lo = -Infinity;
  if (!isFinite(hi)) hi = Infinity;
  const w = [];
  for (let i = 0; i < sp.wave.length; i++) {
    const v = sp.wave[i];
    if (v >= lo && v <= hi && isFinite(v) && v > 0) w.push(v);
  }
  if (w.length < 4) return false;
  let dmin = Infinity, dmax = -Infinity, dsum = 0, n = 0;
  for (let i = 1; i < w.length; i++) {
    const d = Math.log(w[i] / w[i - 1]);
    if (!(d > 0) || !isFinite(d)) return false;
    dmin = Math.min(dmin, d); dmax = Math.max(dmax, d); dsum += d; n++;
  }
  const med = dsum / n;
  return (dmax - dmin) <= 1e-4 * med;
}

function fmtVel(kmps) {
  if (!isFinite(kmps)) return '—';
  return kmps >= 100 ? kmps.toFixed(0) : kmps.toFixed(1);
}

function updateDlamUI() {
  const mode = $('dlamModeSel').value;
  const row = $('dlamCustomRow');
  if (row) row.style.display = mode === 'custom' ? '' : 'none';
  let d = null, src = '';
  if (mode === 'custom') {
    d = parseFloat($('dlamIn').value);
    src = t('dlam.src.custom');
  } else if (mode === 'obs') {
    d = obsDlam();
    src = t('dlam.src.obs');
  } else {
    const name = $('templateSel').value;
    const tt = (S.templates || []).find(x => x.name === name);
    d = tt && tt.dlam != null ? Number(tt.dlam) : null;
    src = t('dlam.src.tpl');
  }
  let msg;
  if (d != null && isFinite(d) && d > 0) {
    const v = fmtVel(C_KMS * Math.LN10 * d);
    const vars = { dlam: d.toExponential(2), v };
    if (mode === 'obs') msg = t(isLogGrid() ? 'dlam.hint.obsLog' : 'dlam.hint.obs', vars);
    else if (mode === 'template') msg = t(isLogGrid() ? 'dlam.hint.tplLog' : 'dlam.hint.tpl', vars);
    else msg = t('dlam.hint.custom', vars);
  } else {
    msg = t(mode === 'obs' ? 'dlam.hint.needSpec' : 'dlam.hint.default');
  }
  const h = $('dlamHint');
  if (h) h.textContent = msg;
}

/* ---------------- 尘埃消光（仅 pPXF 引擎） ---------------- */
function dustEnabled() {
  return $('engineSel').value !== 'ppxf_sew';
}

function updateDustUI() {
  const on = dustEnabled();
  const block = $('dustBlock');
  if (block) block.style.display = on ? 'contents' : 'none';
  const note = $('dustOffNote');
  if (note) note.style.display = on ? 'none' : '';
  if (!on) return;
  const mode = $('dustModeSel').value;
  const hasDust = mode !== 'none';
  const two = (mode === 'powerlaw' || mode === 'kc13' || mode === 'custom');
  $('dustSlopeRow').style.display = two ? '' : 'none';
  $('dustSlopeLabel').style.display = two ? '' : 'none';
  $('dustExprRow').style.display = mode === 'custom' ? '' : 'none';
  if (mode === 'kc13') {
    $('dustSlopeMin').value = '-1'; $('dustSlopeMax').value = '0.4';
  }
  const desc = {
    none: t('dust.hint.none'),
    calzetti: t('dust.hint.calzetti'),
    powerlaw: t('dust.hint.powerlaw'),
    kc13: t('dust.hint.kc13'),
    custom: t('dust.hint.custom'),
  }[mode] || '';
  const gas = $('dustOnSel').value === 'both'
    ? t('dust.hint.gas') : t('dust.hint.stars');
  const av = `${$('dustAvMin').value}~${$('dustAvMax').value} mag`;
  $('dustHint').textContent = hasDust ? desc + gas + t('dust.hint.av', { av }) : desc;
}

function collectDustCfg() {
  // ppxf-sew 拟合等值宽度谱，消光被 att_curve 吸收 → 不需要（也不发送）
  if (!dustEnabled()) return { mode: 'none' };
  const mode = $('dustModeSel').value || 'none';
  if (mode === 'none') return { mode: 'none' };
  const num = (id, d) => {
    const v = parseFloat($(id).value);
    return isFinite(v) ? v : d;
  };
  return {
    mode,
    on: $('dustOnSel').value || 'stars',
    av_range: [num('dustAvMin', 0), num('dustAvMax', 4)],
    slope_range: [num('dustSlopeMin', -3), num('dustSlopeMax', 1)],
    expr: $('dustExpr').value || 'av*(lam/5500.)**-0.7',
  };
}

function updateFwhmModeUI() {
  const m = $('fwhmMode').value;
  const show = (id, on) => { $(id).style.display = on ? '' : 'none'; };
  show('fwhmFuncLabel', m === 'function');
  show('fwhmFunc', m === 'function');
  show('fwhmLsfLabel', m === 'scatter');
  show('fwhmLsfRow', m === 'scatter');
  const needVal = (m === 'lambda/R' || m === 'constant');
  show('fwhmRLabel', needVal);
  show('fwhmRow', needVal);
  show('fwhmRRng', m === 'lambda/R');
  show('fwhmValRng', m === 'constant');
}

/* ------------------------------------------------------------------ */
/* 数值滑条                                                            */
/* ------------------------------------------------------------------ */
function _decimals(step) {
  const s = String(step);
  const i = s.indexOf('.');
  return i < 0 ? 0 : s.length - i - 1;
}

function _fmtNum(v, step) {
  const d = Math.min(6, _decimals(step || 1));
  return Number(v).toFixed(d);
}

function _decOf(el) {
  const ds = el.dataset && el.dataset.dec;
  if (ds !== undefined && ds !== '') {
    const d = parseInt(ds, 10);
    if (isFinite(d)) return Math.min(6, d);
  }
  return Math.min(6, _decimals(el.step || 1));
}

function _fmtVal(el, v) { return Number(v).toFixed(_decOf(el)); }

function _clampNum(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

/** 更新区间滑条的选中填充条 */
function updatePairFill(rMinId, rMaxId) {
  const a = $(rMinId), b = $(rMaxId);
  const wrap = a && a.parentElement;
  const fill = wrap && wrap.querySelector('.rngfill');
  if (!a || !b || !fill) return;
  const lo = parseFloat(a.min), hi = parseFloat(a.max);
  if (!(hi > lo)) return;
  const p1 = _clampNum((parseFloat(a.value) - lo) / (hi - lo), 0, 1);
  const p2 = _clampNum((parseFloat(b.value) - lo) / (hi - lo), 0, 1);
  fill.style.left = (Math.min(p1, p2) * 100) + '%';
  fill.style.width = (Math.abs(p2 - p1) * 100) + '%';
}

/** 区间（min/max）双滑条 <-> 两个数字输入框 */
function bindRangePair(nMinId, nMaxId, rMinId, rMaxId) {
  const nMin = $(nMinId), nMax = $(nMaxId);
  const rMin = $(rMinId), rMax = $(rMaxId);
  if (!nMin || !nMax || !rMin || !rMax) return;
  const get = (el) => parseFloat(el.value);
  rMin.oninput = () => {
    let v = get(rMin);
    const hi = get(rMax);
    if (isFinite(hi) && v > hi) { v = hi; rMin.value = v; }
    nMin.value = _fmtVal(rMin, v);
    updatePairFill(rMinId, rMaxId);
  };
  rMax.oninput = () => {
    let v = get(rMax);
    const lo = get(rMin);
    if (isFinite(lo) && v < lo) { v = lo; rMax.value = v; }
    nMax.value = _fmtVal(rMax, v);
    updatePairFill(rMinId, rMaxId);
  };
  nMin.addEventListener('input', () => {
    const v = get(nMin);
    if (isFinite(v)) rMin.value = _clampNum(v, +rMin.min, +rMax.max);
    updatePairFill(rMinId, rMaxId);
  });
  nMax.addEventListener('input', () => {
    const v = get(nMax);
    if (isFinite(v)) rMax.value = _clampNum(v, +rMin.min, +rMax.max);
    updatePairFill(rMinId, rMaxId);
  });
  updatePairFill(rMinId, rMaxId);
}

/** 单个线性滑条 <-> 数字框 */
function bindLinear(numId, rngId) {
  const n = $(numId), r = $(rngId);
  if (!n || !r) return;
  r.oninput = () => { n.value = _fmtNum(parseFloat(r.value), r.step); };
  n.addEventListener('input', () => {
    const v = parseFloat(n.value);
    if (isFinite(v)) r.value = _clampNum(v, +r.min, +r.max);
  });
}

/** 对数滑条（用于 R = λ/Δλ） */
function bindLog(numId, rngId, lo, hi, step) {
  const n = $(numId), r = $(rngId);
  if (!n || !r) return;
  const a = Math.log10(lo), b = Math.log10(hi);
  r.min = a; r.max = b; r.step = step || (b - a) / 400;
  r.oninput = () => {
    n.value = _fmtNum(Math.pow(10, parseFloat(r.value)), 1);
  };
  n.addEventListener('input', () => {
    const v = parseFloat(n.value);
    if (isFinite(v) && v > 0) r.value = _clampNum(Math.log10(v), a, b);
  });
}

/** 把当前数字框的值同步到滑条（程序改了数值后调用） */
function syncSliders() {
  [['bandMin', 'bandMinR'], ['bandMax', 'bandMaxR'],
   ['fitMin', 'fitMinR'], ['fitMax', 'fitMaxR'],
   ['vminIn', 'vminR'], ['vmaxIn', 'vmaxR'],
   ['sigMinIn', 'sigMinR'], ['sigMaxIn', 'sigMaxR'],
   ['fwhmVal', 'fwhmValR'], ['zValue', 'zR'], ['aperture', 'apertureR']].forEach(([n, r]) => {
    const ne = $(n), re = $(r);
    if (!ne || !re) return;
    const v = parseFloat(ne.value);
    if (isFinite(v)) re.value = _clampNum(v, +re.min, +re.max);
  });
  [['bandMinR', 'bandMaxR'], ['fitMinR', 'fitMaxR'],
   ['vminR', 'vmaxR'], ['sigMinR', 'sigMaxR']].forEach(([a, b]) => {
    updatePairFill(a, b);
  });
  const nR = $('fwhmR'), sR = $('fwhmRR');
  const v = parseFloat(nR && nR.value);
  if (isFinite(v) && v > 0 && sR) {
    sR.value = _clampNum(Math.log10(v), +sR.min, +sR.max);
  }
}

/** 按当前数据波长范围设置波段滑条边界 */
function setWaveSliderBounds(lo, hi) {
  if (!isFinite(lo) || !isFinite(hi) || hi <= lo) return;
  ['bandMinR', 'bandMaxR', 'fitMinR', 'fitMaxR'].forEach(id => {
    const e = $(id);
    if (!e) return;
    e.min = lo; e.max = hi;
    e.step = 'any';          // 连续取值，避免吸附到 4601.3 这种怪数
    e.dataset.dec = '1';     // 数字框显示一位小数
  });
  syncSliders();
}

function initSliders() {
  bindRangePair('bandMin', 'bandMax', 'bandMinR', 'bandMaxR');
  bindRangePair('fitMin', 'fitMax', 'fitMinR', 'fitMaxR');
  bindRangePair('vminIn', 'vmaxIn', 'vminR', 'vmaxR');
  bindRangePair('sigMinIn', 'sigMaxIn', 'sigMinR', 'sigMaxR');
  bindLog('fwhmR', 'fwhmRR', 100, 20000, 0.005);
  bindLinear('fwhmVal', 'fwhmValR');
  bindLinear('aperture', 'apertureR');
  bindLinear('zValue', 'zR');

  const vR = $('vminR'), vX = $('vmaxR');
  [vR, vX].forEach(e => { if (e) { e.min = -1500; e.max = 1500; e.step = 1; } });
  const sR = $('sigMinR'), sX = $('sigMaxR');
  [sR, sX].forEach(e => { if (e) { e.min = 0; e.max = 1000; e.step = 1; } });
  const fv = $('fwhmValR');
  if (fv) { fv.min = 0.5; fv.max = 20; fv.step = 0.05; }
  const apR = $('apertureR');
  if (apR) { apR.min = 0; apR.max = 10; apR.step = 0.5; }
  S.apUnit = 'pix';
  const zR = $('zR');
  if (zR) {
    zR.min = -0.01; zR.max = 0.5; zR.step = 0.0001;
    zR.addEventListener('input', () => { $('zAuto').checked = false; });
  }
  syncSliders();
}

function initSplitter() {
  const sp = $('panelSplitter');
  if (!sp) return;
  let dragging = false, startY = 0, startH = 0;
  sp.addEventListener('mousedown', e => {
    dragging = true;
    startY = e.clientY;
    const cur = parseFloat(getComputedStyle(document.documentElement)
      .getPropertyValue('--panel-h'));
    startH = isFinite(cur) && cur > 0 ? cur : 320;
    sp.classList.add('dragging');
    document.body.style.cursor = 'ns-resize';
    e.preventDefault();
  });
  window.addEventListener('mousemove', e => {
    if (!dragging) return;
    const headerH = document.querySelector('header').getBoundingClientRect().height;
    const avail = Math.max(420, window.innerHeight - headerH - 20);
    // 向下拖 → 底部四列区变矮（上面展示区变高）
    let h = Math.round(startH - (e.clientY - startY));
    h = Math.max(180, Math.min(h, avail - 240));
    S.panelH = h;
    document.documentElement.style.setProperty('--panel-h', h + 'px');
    if (S.image) requestAnimationFrame(drawMap);
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    sp.classList.remove('dragging');
    document.body.style.cursor = '';
    if (S.image) drawMap();
  });
  sp.addEventListener('dblclick', () => { S.panelH = null; syncPanelHeight(); });
}

/* ------------------------------------------------------------------ */
/* 打开文件 / 识别                                                     */
/* ------------------------------------------------------------------ */
async function openPath(path, override) {
  if (!path) return toast(t('msg.inputPath'), 'err');
  setStatus(t('status.loading'));
  try {
    const j = await postJSON('/api/open', { path, override: override || {} });
    S.path = j.path;
    S.meta = j;
    S.spec = null;
    S.image = null;
    S.result = null;
    S.target = { x: null, y: null, row: j.row || 0, aperture: 0, bin: null };
    S.bins = null; S.binsIndex = null;
    $('pathInput').value = j.path;
    renderMeta(j);
    resetResultPanel();
    if (j.kind === 'cube') {
      showView('map');
      defaultBand();
      await loadImage();
      // 默认选中心像素并显示其光谱（bin 模式先按 PowerBin 分 bin）
      S.target.x = Math.floor(j.nx / 2);
      S.target.y = Math.floor(j.ny / 2);
      if (useBin()) {
        try { await loadBins(true); } catch (e) { /* 失败则退化为单像素/孔径 */ }
      }
      await loadSpectrum(false);
      showView('map');
    } else {
      showView('spec');
      await loadSpectrum();
    }
    setStatus(t('status.loaded', { name: j.basename, kind: j.kind }));
  } catch (e) {
    await handleLoadFailure(path, e.message);
  }
}

async function handleLoadFailure(path, msg) {
  setStatus(t('status.autofail'), true);
  toast(msg, 'err');
  try {
    // 只读文件结构：拿到 HDU 列表 + BINTABLE 列 + 建议映射
    const j = await postJSON('/api/inspect', { path });
    S.path = j.path;
    S.meta = j;
    S.spec = null;
    S.image = null;
    S.result = null;
    S.target = { x: null, y: null, row: 0, aperture: 0, bin: null };
    S.bins = null; S.binsIndex = null;
    $('pathInput').value = j.path;
    renderMeta(j);
    resetResultPanel();
    // 回到空展示区，等用户指定映射后重新加载
    $('mapView').classList.add('hidden');
    $('spectrumView').classList.add('hidden');
    $('emptyView').classList.remove('hidden');
    $('pixelBadge').classList.add('hidden');
    $('warnBox').textContent = t('msg.autofail.detail', { msg });
    $('warnBox').style.display = '';
    $('overrideDetails').open = true;
    setStatus(t('status.manual'), true);
  } catch (e2) {
    setStatus(t('status.readfail', { msg: e2.message }), true);
    toast(e2.message, 'err');
  }
}

function defaultBand() {
  if (!S.meta) return;
  $('bandMin').value = Number(S.meta.wave.min).toFixed(2);
  $('bandMax').value = Number(S.meta.wave.max).toFixed(2);
  $('cminIn').value = '';
  $('cmaxIn').value = '';
  syncSliders();
}

function renderMeta(m) {
  S.meta = m;
  S.sky = m.sky || null;
  S.apUnit = S.apUnit || 'pix';
  const sr = $('skyReadout');
  if (sr) sr.style.display = 'none';
  const kindTxt = m.kind === 'cube' ? t('meta.cube3d') : t('meta.spec12d');
  let shape = t('meta.unknown');
  if (m.shape && m.nwave) {
    shape = m.kind === 'cube'
      ? `${m.nwave} × ${m.ny} × ${m.nx}  (wave,y,x)`
      : `${m.nwave}` + (m.nrows > 1 ? ` × ${m.nrows} rows` : '');
  } else if (m.hdus) {
    const bt = m.hdus.filter(h => h.type === 'BinTableHDU');
    if (bt.length) shape = `BINTABLE: ` + bt.map(h =>
      `[${h.index}] ${h.name || '—'} (${h.nrows || 0} row)`).join(', ');
  }
  $('fileInfo').innerHTML =
    `<b>${esc(m.basename)}</b><br/>` +
    `<span class="muted">${kindTxt} · ${esc(shape)}</span>` +
    (m.objname ? `<br/><span class="muted">OBJECT: ${esc(m.objname)}</span>` : '');

  const roles = m.roles || {};
  const rows = [
    [t('meta.type'), kindTxt],
    [t('meta.shape'), shape],
    [t('meta.wave'), m.wave ? `${fmt(m.wave.min, 2)} – ${fmt(m.wave.max, 2)} Å` : t('meta.notManual')],
    [t('meta.wavesrc'), (m.wave && m.wave.source) || '—'],
    [t('meta.loggrid'), m.wave ? t(m.wave.log ? 'meta.yes' : 'meta.no') : '—'],
    ['Flux HDU', hduText(m, roles.flux) + (roles.flux_col ? ` : ${roles.flux_col}` : '')],
    ['Var HDU', hduText(m, roles.var) + (roles.var_col ? ` : ${roles.var_col}` : '')],
    ['Mask HDU', hduText(m, roles.mask) + (roles.mask_col ? ` : ${roles.mask_col}` : '')],
    [t('meta.varkind'), roles.var_kind || 'none'],
  ];
  $('detectBox').innerHTML = rows.map(([k, v]) =>
    `<div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>`).join('');
  $('detectBox').style.display = '';

  const w = (m.warnings || []);
  if (w.length) {
    const codes = m.warning_codes || [];
    const lines = codes.length
      ? codes.map(c => t('warn.' + c.code, c.params || {}))
      : w;
    $('warnBox').textContent = t('warn.prefix') + '\n· ' + lines.join('\n· ');
    $('warnBox').style.display = '';
  } else {
    $('warnBox').style.display = 'none';
  }

  // ---- 覆盖控件 ----
  $('ovKind').value = '';
  fillHduSelect($('ovFlux'), m.hdus, true);
  fillHduSelect($('ovVar'), m.hdus, true);
  fillHduSelect($('ovMask'), m.hdus, true);
  fillHduSelect($('ovWaveHdu'), m.hdus, true);
  fillAxisSelect(m);
  setSel($('ovFlux'), roles.flux);
  setSel($('ovVar'), roles.var);
  setSel($('ovMask'), roles.mask);
  setSel($('ovWaveHdu'), roles.wave);
  fillColSelect($('ovFluxCol'), m, roles.flux, roles.flux_col);
  fillColSelect($('ovVarCol'), m, roles.var, roles.var_col);
  fillColSelect($('ovMaskCol'), m, roles.mask, roles.mask_col);
  fillColSelect($('ovWaveCol'), m, roles.wave, roles.wave_col);
  $('ovVarKind').value = !roles.var_kind || roles.var_kind === 'none' ? '' : roles.var_kind;
  $('ovMaskSem').value = '';
  $('ovAxis').value = m.spec_axis == null ? '' : String(m.spec_axis);
  $('ovRow').value = m.row || 0;
  $('ovWaveMode').value = '';
  $('ovWaveStart').value = '';
  $('ovWaveStep').value = '';
  $('ovWaveLog').checked = false;

  // ---- 拟合默认波段 ----
  if (m.wave) {
    $('fitMin').value = Number(m.wave.min).toFixed(1);
    $('fitMax').value = Number(m.wave.max).toFixed(1);
    setWaveSliderBounds(Number(m.wave.min), Number(m.wave.max));
  }

  // ---- FWHM ----
  // 用户显式选择「函数 / 散点 LSF」时不要被自动识别重置
  const f = m.fwhm || {};
  S.fwhm = f;
  const keepMode = ['function', 'scatter'].includes($('fwhmMode').value);
  if (!keepMode) {
    if (f.mode === 'constant') {
      $('fwhmMode').value = 'constant';
      $('fwhmVal').value = f.value;
    } else {
      $('fwhmMode').value = 'lambda/R';
      $('fwhmR').value = Math.round(Number(f.R || 2000) * 10) / 10;
    }
  }
  if (f.source) $('fwhmR').title = f.source;
  updateFwhmModeUI();
  updateCoAddUI();
  refreshApUI();
  syncSliders();

  $('pixelBadge').classList.add('hidden');
  $('overrideDetails').open = false;
}

function fillHduSelect(sel, hdus, allowEmpty) {
  sel.innerHTML = '';
  if (allowEmpty) {
    const o = document.createElement('option');
    o.value = ''; o.textContent = t('meta.autoNone');
    sel.appendChild(o);
  }
  (hdus || []).forEach(h => {
    if (h.type === 'BinTableHDU') {
      const o = document.createElement('option');
      o.value = String(h.index);
      o.textContent = `[${h.index}] ${h.name || '—'} · TABLE ` +
        `${h.nrows || 0}R × ${(h.columns || []).length}C`;
      sel.appendChild(o);
      return;
    }
    if (!h.shape) return;
    const o = document.createElement('option');
    o.value = String(h.index);
    o.textContent = `[${h.index}] ${h.name || '—'}  ${h.shape.join('×')}`;
    sel.appendChild(o);
  });
}

function fillColSelect(sel, m, hduIdx, preferred) {
  sel.innerHTML = '';
  const auto = document.createElement('option');
  auto.value = ''; auto.textContent = t('meta.autoNotTable');
  sel.appendChild(auto);
  const h = (m && m.hdus || []).find(x => String(x.index) === String(hduIdx));
  if (!h || h.type !== 'BinTableHDU') {
    sel.disabled = true;
    return;
  }
  (h.columns || []).forEach(c => {
    const rs = (c.row_shape || c.shape || []).join('×');
    const o = document.createElement('option');
    o.value = c.name;
    o.textContent = `${c.name} (${rs})`;
    sel.appendChild(o);
  });
  sel.disabled = false;
  if (preferred && [...sel.options].some(o => o.value === preferred)) {
    sel.value = preferred;
  }
}

function fillAxisSelect(m) {
  const sel = $('ovAxis');
  sel.innerHTML = '';
  const o = document.createElement('option');
  o.value = ''; o.textContent = t('meta.auto');
  sel.appendChild(o);
  if (m.kind !== 'cube' || !m.shape) return;
  const dims = m.shape.slice(); // nwave,ny,nx (canonical)
  for (let i = 0; i < 3; i++) {
    const opt = document.createElement('option');
    opt.value = String(i);
    opt.textContent = t('meta.npAxis', { i, n: dims[i] });
    sel.appendChild(opt);
  }
}

function setSel(sel, val) {
  if (val === null || val === undefined) { sel.value = ''; return; }
  if (![...sel.options].some(o => o.value === String(val))) {
    const o = document.createElement('option');
    o.value = String(val); o.textContent = String(val);
    sel.appendChild(o);
  }
  sel.value = String(val);
}

function hduText(m, idx) {
  if (idx === null || idx === undefined) return t('meta.unused');
  const h = (m.hdus || []).find(x => x.index === idx);
  return h ? `[${idx}] ${h.name || '—'}` : String(idx);
}

async function applyOverride() {
  if (!S.meta) return;
  const ov = {};
  if ($('ovKind').value) ov.kind = $('ovKind').value;
  if ($('ovFlux').value !== '') ov.flux_hdu = Number($('ovFlux').value);
  if ($('ovVar').value !== '') ov.var_hdu = Number($('ovVar').value);
  if ($('ovMask').value !== '') ov.mask_hdu = Number($('ovMask').value);
  if ($('ovFluxCol').value) ov.flux_col = $('ovFluxCol').value;
  if ($('ovVarCol').value) ov.var_col = $('ovVarCol').value;
  if ($('ovMaskCol').value) ov.mask_col = $('ovMaskCol').value;
  if ($('ovWaveCol').value) ov.wave_col = $('ovWaveCol').value;
  if ($('ovVarKind').value) ov.var_kind = $('ovVarKind').value;
  if ($('ovMaskSem').value) ov.mask_semantics = $('ovMaskSem').value;
  if ($('ovAxis').value !== '') ov.spec_axis = Number($('ovAxis').value);
  if ($('ovRow').value !== '') ov.row = Number($('ovRow').value);
  if ($('ovWaveMode').value) ov.wave_mode = $('ovWaveMode').value;
  if ($('ovWaveHdu').value !== '') ov.wave_hdu = Number($('ovWaveHdu').value);
  if ($('ovWaveStart').value !== '') ov.wave_start = Number($('ovWaveStart').value);
  if ($('ovWaveStep').value !== '') ov.wave_step = Number($('ovWaveStep').value);
  if ($('ovWaveLog').checked) ov.wave_log = true;
  await openPath(S.path, ov);
}

async function onUploadFile(e) {
  const f = e.target.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append('file', f);
  setStatus(t('status.uploading'));
  try {
    const j = await api('/api/upload', { method: 'POST', body: fd });
    await openPath(j.path);
  } catch (err) {
    setStatus(t('status.uploadfail', { msg: err.message }), true);
    toast(err.message, 'err');
  }
  e.target.value = '';
}

/* ------------------------------------------------------------------ */
/* 文件浏览                                                            */
/* ------------------------------------------------------------------ */
async function openBrowse(path, pick) {
  S._browsePick = pick || null;
  try {
    const j = await api('/api/browse?path=' + encodeURIComponent(path));
    S._browseParent = j.parent;
    $('browseModal').classList.remove('hidden');
    $('browsePath').value = j.path;
    const dirs = $('browseDirs'), files = $('browseFiles');
    dirs.innerHTML = ''; files.innerHTML = '';
    j.dirs.forEach(d => {
      const el = document.createElement('div');
      el.className = 'item dir';
      el.textContent = '📁 ' + d.name;
      el.onclick = () => openBrowse(d.path);
      dirs.appendChild(el);
    });
    j.files.forEach(f => {
      const el = document.createElement('div');
      el.className = 'item';
      el.innerHTML = `<span>🗎 ${esc(f.name)}</span>` +
        `<span class="sz">${(f.size / 1048576).toFixed(1)} MB</span>`;
      el.onclick = async () => {
        $('browseModal').classList.add('hidden');
        const cb = S._browsePick;
        S._browsePick = null;
        if (cb) { cb(f.path); return; }
        await openPath(f.path);
      };
      files.appendChild(el);
    });
    if (!j.dirs.length && !j.files.length) {
      files.innerHTML = `<div class="muted small">${t('modal.empty')}</div>`;
    }
  } catch (e) { toast(e.message, 'err'); }
}

/* ------------------------------------------------------------------ */
/* 图像显示                                                            */
/* ------------------------------------------------------------------ */
async function loadImage() {
  if (!S.meta || S.meta.kind !== 'cube') return;
  const q = new URLSearchParams({
    mode: $('imgMode').value,
    max_size: '420',
  });
  const lo = parseFloat($('bandMin').value);
  const hi = parseFloat($('bandMax').value);
  if (isFinite(lo)) q.set('wave_min', lo);
  if (isFinite(hi)) q.set('wave_max', hi);
  try {
    S.image = await api('/api/image?' + q.toString());
    drawMap();
    $('mapHint').classList.add('hidden');
    $('colorbar').classList.remove('hidden');
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------------- bin / 孔径 切换 ---------------- */
function useBin() {
  const e = $('useBin');
  return !!(e && e.checked);
}

/* ---------------- 孔径半径单位（pix / 角秒） ---------------- */
function apScale() {
  const s = S.sky;
  if (!s || !s.has_wcs) return null;
  const det = s.mat[0][0] * s.mat[1][1] - s.mat[0][1] * s.mat[1][0];
  const v = 3600 * Math.sqrt(Math.abs(det));      // 角秒 / 像素
  return (isFinite(v) && v > 0) ? v : null;
}

function apUnit() { return S.apUnit || 'pix'; }

/** 当前输入框的孔径半径 → 像素（后端只接受像素） */
function apPix() {
  const v = parseFloat($('aperture').value) || 0;
  if (apUnit() === 'arcsec') {
    const sc = apScale();
    if (sc) return v / sc;
  }
  return v;
}

/** 把像素半径按当前单位显示 */
function fmtAp(px) {
  if (px == null || !isFinite(px)) return '—';
  const sc = apScale();
  if (apUnit() === 'arcsec' && sc) return `${(px * sc).toFixed(2)}″`;
  return `${(Math.round(px * 10) / 10)} px`;
}

function setupApSlider() {
  const r = $('apertureR');
  if (!r) return;
  const sc = apScale();
  const arc = apUnit() === 'arcsec' && sc;
  r.min = 0;
  r.max = arc ? +(10 * sc).toFixed(2) : 10;
  r.step = arc ? Math.max(0.01, +(sc / 2).toFixed(3)) : 0.5;
}

let _apTimer = null;

/** 孔径改变后自动重取光谱（防抖 ~350ms；不必再手动点「刷新」） */
function scheduleApertureUpdate() {
  if (!S.meta || S.meta.kind !== 'cube' || useBin()) return;
  const ap = apPix();
  if (S.spec && Math.abs((S.spec.aperture || 0) - ap) < 1e-6) return;
  clearTimeout(_apTimer);
  _apTimer = setTimeout(async () => {
    if (!S.meta || S.meta.kind !== 'cube' || useBin()) return;
    if (S.result) resetResultPanel();
    try { await loadSpectrum(false); } catch (e) { /* ignore */ }
  }, 350);
}

function updateApHint() {
  const el = $('apHint');
  if (!el) return;
  const sc = apScale();
  if (apUnit() === 'arcsec' && !sc) {
    el.textContent = t('msg.noWCS');
    return;
  }
  if (!sc) { el.textContent = ''; return; }
  const v = parseFloat($('aperture').value) || 0;
  el.textContent = apUnit() === 'arcsec'
    ? t('ap.hint.pix', { px: apPix().toFixed(2), sc: sc.toFixed(3) })
    : t('ap.hint.arcsec', { as: (v * sc).toFixed(2), sc: sc.toFixed(3) });
}

/** 打开新文件后刷新孔径单位可用性/滑条/提示 */
function refreshApUI() {
  const sc = apScale();
  const sel = $('apUnit');
  if (sel) {
    const o = sel.querySelector('option[value="arcsec"]');
    if (o) {
      o.disabled = !sc;
      if (!sc && sel.value === 'arcsec') {
        sel.value = 'pix';
        S.apUnit = 'pix';
      }
    }
  }
  setupApSlider();
  syncSliders();
  updateApHint();
}

function updateCoAddUI() {
  const isCube = !S.meta || S.meta.kind === 'cube';
  const box = $('coaddBox');
  if (box) box.style.display = isCube ? 'contents' : 'none';
  if (!isCube) return;
  const on = useBin();
  const show = (id, v) => { const e = $(id); if (e) e.style.display = v ? '' : 'none'; };
  show('binSNLabel', on); show('binSN', on);
  show('binSelLabel', on); show('binSelRow', on);
  show('showBinsRow', on);
  show('apLabel', !on); show('apRow', !on); show('apSliderRow', !on);
  show('apHint', !on);
  const sn = $('binSN');
  if (sn) sn.disabled = !on;
}

/* ---------------- PowerBin 分 bin ---------------- */
function binAt(x, y) {
  const b = S.bins;
  if (!b || !b.map || x === null || y === null) return null;
  const row = b.map[y];
  if (!row || x < 0 || x >= row.length) return null;
  const v = row[x];
  return (v === undefined || v < 0) ? null : v;
}

function binLabel(k) {
  const b = S.bins;
  const np = b && b.npix ? b.npix[k] : null;
  const sn = b && b.sn ? b.sn[k] : null;
  return `#${k}` + (np != null ? ` · ${np} px` : '') +
    (sn != null ? ` · S/N≈${sn >= 10 ? sn.toFixed(0) : sn.toFixed(1)}` : '');
}

function populateBinSel(prefer) {
  const sel = $('binSel');
  const b = S.bins;
  sel.innerHTML = '';
  if (!b) { sel.disabled = true; return; }
  for (let k = 0; k < b.nbin; k++) {
    const o = document.createElement('option');
    o.value = String(k);
    o.textContent = binLabel(k);
    sel.appendChild(o);
  }
  sel.disabled = false;
  let k = (prefer == null ? null : prefer);
  if (k == null || !(k >= 0 && k < b.nbin)) k = binAt(S.target.x, S.target.y);
  if (k == null) k = 0;
  S.target.bin = k;
  sel.value = String(k);
}

async function loadBins(force) {
  if (!S.meta || S.meta.kind !== 'cube') return;
  const sn = parseFloat($('binSN').value) || 10;
  const key = String(sn);
  if (!force && S.bins && S.binsKey === key) {
    populateBinSel(S.target.bin);
    drawMap();
    return;
  }
  // S/N 估算波段固定 5300–5700 Å（与 cube.ipynb 的 sn_band 一致，
  //   不随显示波段变化；cube 不覆盖时后端自动退回全谱）
  const q = new URLSearchParams({ target_sn: String(sn) });
  setStatus(t('status.bins', { sn }));
  $('binSel').disabled = true;
  try {
    const j = await api('/api/bins?' + q.toString());
    S.bins = j;
    S.binsKey = key;
    populateBinSel(S.target.bin);
    drawMap();
    const sns = (j.sn || []).slice().sort((a, b) => a - b);
    const med = sns.length ? sns[Math.floor(sns.length / 2)] : null;
    setStatus(t('status.bins.ok', {
      n: j.nbin, sn,
      med: med != null ? t('status.bins.med', { med: med.toFixed(0) }) : '',
    }));
  } catch (e) {
    S.bins = null;
    populateBinSel(null);
    toast(t('msg.binFail', { msg: e.message }), 'err');
  }
}

async function selectBin(k) {
  if (!S.bins) return;
  k = Math.max(0, Math.min(S.bins.nbin - 1, k | 0));
  S.target.bin = k;
  $('binSel').value = String(k);
  if (S.result) resetResultPanel();
  drawMap();
  await loadSpectrum(false);
}

function stepBin(d) {
  if (!S.bins) return;
  selectBin((S.target.bin == null ? 0 : S.target.bin) + d);
}

/* ---------------- 天体测量坐标 ---------------- */
function skyFromPix(x, y) {
  const s = S.sky;
  if (!s || !s.has_wcs) return null;
  const dx = x - s.crpix[0], dy = y - s.crpix[1];
  let ra = s.crval[0] + s.mat[0][0] * dx + s.mat[0][1] * dy;
  const dec = s.crval[1] + s.mat[1][0] * dx + s.mat[1][1] * dy;
  ra = ((ra % 360) + 360) % 360;
  return [ra, dec];
}

function fmtRA(ra) {
  const h = ra / 15;
  let H = Math.floor(h);
  let m = (h - H) * 60, M = Math.floor(m), S = (m - M) * 60;
  if (S >= 59.95) { S = 0; M += 1; }
  if (M >= 60) { M = 0; H = (H + 1) % 24; }
  return `${String(H).padStart(2, '0')}h${String(M).padStart(2, '0')}m` +
    `${S.toFixed(1).padStart(4, '0')}s`;
}

function fmtDec(dec) {
  const sign = dec < 0 ? '-' : '+';
  let d = Math.abs(dec);
  let D = Math.floor(d), m = (d - D) * 60, M = Math.floor(m), S = (m - M) * 60;
  if (S >= 59.95) { S = 0; M += 1; }
  if (M >= 60) { M = 0; D += 1; }
  return `${sign}${String(D).padStart(2, '0')}°${String(M).padStart(2, '0')}′` +
    `${S.toFixed(1).padStart(4, '0')}″`;
}

function _niceStep(span, n) {
  if (!(span > 0)) return 1;
  const raw = span / Math.max(1, n);
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}

function updateSkyReadout(px, py) {
  const el = $('skyReadout');
  if (!el) return;
  const s = S.sky;
  if (!s || !s.has_wcs || px == null || py == null) {
    el.style.display = 'none';
    return;
  }
  const w = skyFromPix(px, py);
  if (!w) { el.style.display = 'none'; return; }
  el.style.display = 'block';
  el.textContent = `RA ${fmtRA(w[0])}  Dec ${fmtDec(w[1])}  (x=${px}, y=${py})`;
}

/** 在图像上画 RA/Dec 网格与边缘刻度（IFU 小视场，线性近似足够）。 */
function drawSkyGrid(ctx, canvas, dpr) {
  const s = S.sky;
  if (!s || !s.has_wcs || !S.meta) return;
  const nx = S.meta.nx, ny = S.meta.ny;
  const pxX = canvas.width / nx, pxY = canvas.height / ny;
  const m00 = s.mat[0][0], m01 = s.mat[0][1];
  const m10 = s.mat[1][0], m11 = s.mat[1][1];
  const cx0 = s.crpix[0], cy0 = s.crpix[1];
  const world = (x, y) => [
    s.crval[0] + m00 * (x - cx0) + m01 * (y - cy0),
    s.crval[1] + m10 * (x - cx0) + m11 * (y - cy0)];
  // 四角 → 范围（RA 相对中心展开，避免 0/360 跨越）
  const raC = s.crval[0];
  const corners = [[0, 0], [nx - 1, 0], [0, ny - 1], [nx - 1, ny - 1]]
    .map(([x, y]) => {
      const [ra, dec] = world(x, y);
      return [((ra - raC + 540) % 360) - 180, dec];
    });
  const draMin = Math.min(...corners.map(c => c[0]));
  const draMax = Math.max(...corners.map(c => c[0]));
  const decMin = Math.min(...corners.map(c => c[1]));
  const decMax = Math.max(...corners.map(c => c[1]));
  const raStep = _niceStep((draMax - draMin) * 240, 5) / 240;   // 秒（时间）取整
  const decStep = _niceStep((decMax - decMin) * 3600, 5) / 3600; // 角秒取整

  // 像素坐标 → canvas 坐标（y 轴翻转，取像元中心）
  const toCv = (x, y) => [x * pxX, (ny - 0.5 - y) * pxY];
  const T = 1e5;   // 参数化直线远端（会被 clip 裁掉）

  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, canvas.width, canvas.height);
  ctx.clip();
  ctx.strokeStyle = 'rgba(255,255,255,.55)';
  ctx.lineWidth = Math.max(1, 0.7 * dpr);
  ctx.setLineDash([4 * dpr, 4 * dpr]);

  // 带白底的刻度标签（避免压在深色图像上看不清）
  const font = `${10 * dpr}px Segoe UI, system-ui, sans-serif`;
  const drawTag = (txt, x, y, align, baseline) => {
    ctx.save();
    ctx.setLineDash([]);
    ctx.font = font;
    ctx.textAlign = align; ctx.textBaseline = baseline;
    const w = ctx.measureText(txt).width;
    const padX = 2.5 * dpr, hgt = 13 * dpr;
    let bx = x;
    if (align === 'left') bx = x - padX;
    else if (align === 'center') bx = x - w / 2 - padX;
    else bx = x - w - padX;
    let by = y - hgt / 2;
    if (baseline === 'bottom') by = y - hgt;
    else if (baseline === 'top') by = y;
    ctx.fillStyle = 'rgba(255,255,255,.88)';
    ctx.fillRect(bx, by, w + padX * 2, hgt);
    ctx.fillStyle = '#1f2b42';
    ctx.fillText(txt, x, y);
    ctx.restore();
  };

  // 等 Dec 线：沿 (dx,dy) = (-m11, m10) 方向
  if (Math.abs(m11) > 1e-12) {
    const dLen = Math.hypot(m11, m10) || 1;
    const dxu = -m11 / dLen, dyu = m10 / dLen;
    const k0 = Math.ceil((decMin) / decStep);
    const k1 = Math.floor((decMax) / decStep);
    let lastY = -1e3;
    for (let k = k0; k <= k1; k++) {
      const decT = k * decStep;
      const y = cy0 + (decT - s.crval[1]) / m11;
      const p1 = toCv(cx0 - dxu * T, y - dyu * T);
      const p2 = toCv(cx0 + dxu * T, y + dyu * T);
      ctx.beginPath(); ctx.moveTo(p1[0], p1[1]); ctx.lineTo(p2[0], p2[1]);
      ctx.stroke();
      // 左侧刻度（太密的跳过，防重叠）
      const yl = cy0 + (decT - s.crval[1] - m10 * (0 - cx0)) / m11;
      if (yl >= -0.5 && yl <= ny - 0.5) {
        const c = toCv(0, yl);
        if (Math.abs(c[1] - lastY) > 22 * dpr) {
          drawTag(fmtDec(decT), 3 * dpr, c[1], 'left', 'middle');
          lastY = c[1];
        }
      }
    }
  }

  // 等 RA 线：沿 (dx,dy) = (-m01, m00) 方向
  if (Math.abs(m00) > 1e-12) {
    const dLen = Math.hypot(m01, m00) || 1;
    const dxu = -m01 / dLen, dyu = m00 / dLen;
    const raStepSec = raStep * 240;
    const k0 = Math.ceil(((raC + draMin) * 240) / raStepSec);
    const k1 = Math.floor(((raC + draMax) * 240) / raStepSec);
    let lastX = -1e3;
    for (let k = k0; k <= k1; k++) {
      const raAbs = (k * raStepSec) / 240;              // 绝对 RA（度）
      const draT = ((raAbs - raC + 540) % 360) - 180;   // 相对中心
      const x = cx0 + draT / m00;
      const p1 = toCv(x - dxu * T, cy0 - dyu * T);
      const p2 = toCv(x + dxu * T, cy0 + dyu * T);
      ctx.beginPath(); ctx.moveTo(p1[0], p1[1]); ctx.lineTo(p2[0], p2[1]);
      ctx.stroke();
      // 底部刻度
      const xb = cx0 + (draT - m01 * (0 - cy0)) / m00;
      if (xb >= -0.5 && xb <= nx - 0.5) {
        const c = toCv(xb, 0);
        if (Math.abs(c[0] - lastX) > 70 * dpr) {
          const txt = fmtRA(((raAbs % 360) + 360) % 360);
          drawTag(txt, c[0], canvas.height - 3 * dpr, 'center', 'bottom');
          lastX = c[0];
        }
      }
    }
  }
  ctx.restore();
}

function drawMap() {
  const img = S.image;
  if (!img) return;
  const canvas = $('mapCanvas');
  const wrap = canvas.parentElement;
  const [h, w] = img.shape;
  const pad = 8;
  const availW = Math.max(80, wrap.clientWidth - pad * 2);
  const availH = Math.max(80, wrap.clientHeight - pad * 2);
  const aspect = w / h;
  let cw = availW, ch = cw / aspect;
  if (ch > availH) { ch = availH; cw = ch * aspect; }
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(cw * dpr));
  canvas.height = Math.max(1, Math.round(ch * dpr));
  canvas.style.width = cw + 'px';
  canvas.style.height = ch + 'px';

  const ctx = canvas.getContext('2d');
  const off = document.createElement('canvas');
  off.width = w; off.height = h;
  const octx = off.getContext('2d');
  const idata = octx.createImageData(w, h);

  const vmin = parseFloat($('cminIn').value);
  const vmax = parseFloat($('cmaxIn').value);
  const lo = isFinite(vmin) ? vmin : img.vmin;
  const hi = isFinite(vmax) ? vmax : img.vmax;
  const span = (hi - lo) || 1;
  const stretch = $('stretchSel').value;
  for (let y = 0; y < h; y++) {
    const row = img.map[y] || [];
    for (let x = 0; x < w; x++) {
      const v = row[x];
      const k = ((h - 1 - y) * w + x) * 4;
      if (v === null || v === undefined || !isFinite(v)) {
        idata.data[k + 3] = 0;
        continue;
      }
      let t = (v - lo) / span;
      t = Math.max(0, Math.min(1, t));
      if (stretch === 'sqrt') t = Math.sqrt(t);
      else if (stretch === 'log') t = Math.log10(1 + 9 * t);
      const c = viridis(t);
      idata.data[k] = c[0]; idata.data[k + 1] = c[1];
      idata.data[k + 2] = c[2]; idata.data[k + 3] = 255;
    }
  }
  octx.putImageData(idata, 0, 0);

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(off, 0, 0, canvas.width, canvas.height);

  S.mapGeom = { nx: S.meta.nx, ny: S.meta.ny, cw: canvas.width, ch: canvas.height };

  // ---- bin 边界 + 选中 bin 高亮 ----
  const bins = S.bins;
  if (bins && bins.map && useBin() && $('showBins').checked) {
    const ny = bins.map.length;
    const nx = ny ? bins.map[0].length : 0;
    const sx = canvas.width / nx, sy = canvas.height / ny;
    ctx.save();
    if (S.target.bin != null && S.target.bin >= 0) {
      ctx.fillStyle = 'rgba(255,45,85,.28)';
      for (let y = 0; y < ny; y++) {
        const row = bins.map[y];
        for (let x = 0; x < nx; x++) {
          if (row[x] !== S.target.bin) continue;
          ctx.fillRect(x * sx, (ny - 1 - y) * sy,
                       Math.ceil(sx) + 0.5, Math.ceil(sy) + 0.5);
        }
      }
    }
    ctx.strokeStyle = 'rgba(255,255,255,.75)';
    ctx.lineWidth = Math.max(1, 0.7 * dpr);
    ctx.beginPath();
    for (let y = 0; y < ny; y++) {
      const row = bins.map[y];
      for (let x = 0; x < nx; x++) {
        const v = row[x];
        if (x + 1 < nx && row[x + 1] !== v) {
          const px = (x + 1) * sx;
          ctx.moveTo(px, (ny - 1 - y) * sy);
          ctx.lineTo(px, (ny - y) * sy);
        }
        if (y + 1 < ny && bins.map[y + 1][x] !== v) {
          const py = (ny - 1 - y) * sy;
          ctx.moveTo(x * sx, py);
          ctx.lineTo((x + 1) * sx, py);
        }
      }
    }
    ctx.stroke();
    // 选中 bin 的轮廓单独用亮色描一遍（填充色在深色图上不够醒目）
    if (S.target.bin != null && S.target.bin >= 0) {
      ctx.strokeStyle = '#ff2d55';
      ctx.lineWidth = Math.max(1.6, 1.3 * dpr);
      ctx.beginPath();
      for (let y = 0; y < ny; y++) {
        const row = bins.map[y];
        for (let x = 0; x < nx; x++) {
          if (row[x] !== S.target.bin) continue;
          const top = (ny - 1 - y) * sy, bot = (ny - y) * sy;
          const lef = x * sx, rig = (x + 1) * sx;
          if (x === 0 || row[x - 1] !== S.target.bin) {
            ctx.moveTo(lef, top); ctx.lineTo(lef, bot);
          }
          if (x + 1 >= nx || row[x + 1] !== S.target.bin) {
            ctx.moveTo(rig, top); ctx.lineTo(rig, bot);
          }
          if (y === 0 || bins.map[y - 1][x] !== S.target.bin) {
            ctx.moveTo(lef, top); ctx.lineTo(rig, top);
          }
          if (y + 1 >= ny || bins.map[y + 1][x] !== S.target.bin) {
            ctx.moveTo(lef, bot); ctx.lineTo(rig, bot);
          }
        }
      }
      ctx.stroke();
    }
    ctx.restore();
  }

  // ---- 选中标记：孔径模式画出【精确像元格】选区，准星随孔径缩放 ----
  if (S.target.x !== null && S.target.y !== null) {
    const nx = S.meta.nx, ny = S.meta.ny;
    const pxX = canvas.width / nx, pxY = canvas.height / ny;
    const cx = (S.target.x + 0.5) * pxX;
    const cy = (ny - S.target.y - 0.5) * pxY;
    const ap = apPix();
    const binMode = useBin() && S.bins && S.bins.map;
    const r2 = ap * ap;
    ctx.save();
    ctx.strokeStyle = '#ff2d55';
    ctx.fillStyle = 'rgba(255,45,85,.28)';
    ctx.lineWidth = 1.6 * dpr;
    if (!binMode) {
      // 与后端完全一致的判据：(x-x0)² + (y-y0)² ≤ r² 的 spaxel
      const x0 = S.target.x, y0 = S.target.y;
      const inSel = (xx, yy) => ((xx - x0) ** 2 + (yy - y0) ** 2) <= r2;
      const ylo = Math.max(0, y0 - Math.ceil(ap)), yhi = Math.min(ny - 1, y0 + Math.ceil(ap));
      const xlo = Math.max(0, x0 - Math.ceil(ap)), xhi = Math.min(nx - 1, x0 + Math.ceil(ap));
      for (let yy = ylo; yy <= yhi; yy++) {
        for (let xx = xlo; xx <= xhi; xx++) {
          if (!inSel(xx, yy)) continue;
          ctx.fillRect(xx * pxX, (ny - 1 - yy) * pxY,
                       Math.ceil(pxX) + 0.5, Math.ceil(pxY) + 0.5);
        }
      }
      // 选区轮廓：只描"选区外边缘"（与 bin 高亮同一画法）
      ctx.beginPath();
      for (let yy = ylo; yy <= yhi; yy++) {
        for (let xx = xlo; xx <= xhi; xx++) {
          if (!inSel(xx, yy)) continue;
          const top = (ny - 1 - yy) * pxY, bot = (ny - yy) * pxY;
          const lef = xx * pxX, rig = (xx + 1) * pxX;
          if (xx === 0 || !inSel(xx - 1, yy)) { ctx.moveTo(lef, top); ctx.lineTo(lef, bot); }
          if (xx + 1 >= nx || !inSel(xx + 1, yy)) { ctx.moveTo(rig, top); ctx.lineTo(rig, bot); }
          if (yy === 0 || !inSel(xx, yy - 1)) { ctx.moveTo(lef, top); ctx.lineTo(rig, top); }
          if (yy + 1 >= ny || !inSel(xx, yy + 1)) { ctx.moveTo(lef, bot); ctx.lineTo(rig, bot); }
        }
      }
      ctx.lineWidth = Math.max(1.6, 1.2 * dpr);
      ctx.stroke();
      ctx.lineWidth = 1.6 * dpr;
    }
    // 准星：bin 模式固定尺寸；孔径模式随孔径半径放大（臂略伸出选区）
    const extent = Math.max((ap + 0.5) * pxX, (ap + 0.5) * pxY);
    const arm = binMode ? 11 * dpr : Math.max(11 * dpr, extent + 7 * dpr);
    const rad = 7 * dpr;
    ctx.beginPath();
    ctx.arc(cx, cy, rad, 0, Math.PI * 2);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(cx - arm, cy); ctx.lineTo(cx + arm, cy);
    ctx.moveTo(cx, cy - arm); ctx.lineTo(cx, cy + arm);
    ctx.stroke();
    ctx.restore();
  }

  // 天体测量：RA/Dec 网格 + 边缘刻度 + 当前位置读数
  drawSkyGrid(ctx, canvas, dpr);
  updateSkyReadout(S.target.x, S.target.y);

  $('cbarMin').textContent = fmt(lo, 4);
  $('cbarMax').textContent = fmt(hi, 4);
}

function onMapClick(e) {
  if (!S.meta || S.meta.kind !== 'cube' || !S.image) return;
  const canvas = $('mapCanvas');
  const r = canvas.getBoundingClientRect();
  const cx = (e.clientX - r.left) * canvas.width / r.width;
  const cy = (e.clientY - r.top) * canvas.height / r.height;
  const x = Math.floor(cx * S.meta.nx / canvas.width);
  const y = S.meta.ny - 1 - Math.floor(cy * S.meta.ny / canvas.height);
  if (x < 0 || x >= S.meta.nx || y < 0 || y >= S.meta.ny) return;
  S.target.x = x;
  S.target.y = y;
  S.target.row = 0;
  if (useBin()) {
    const k = binAt(x, y);
    S.target.bin = k;
    if (S.bins && k != null) $('binSel').value = String(k);
  }
  drawMap();
  // 换像素 → 旧拟合作废，清掉叠加
  if (S.result) resetResultPanel();
  // 保持在图像视图：光谱在「光谱」标签里查看（避免每点一下都跳走）
  loadSpectrum(false);
}

/* ------------------------------------------------------------------ */
/* 光谱                                                                */
/* ------------------------------------------------------------------ */
async function loadSpectrum(switchView = true) {
  if (!S.meta) return;
  const q = new URLSearchParams();
  if (S.meta.kind === 'cube') {
    if (S.target.x === null) {
      S.target.x = Math.floor(S.meta.nx / 2);
      S.target.y = Math.floor(S.meta.ny / 2);
    }
    q.set('x', S.target.x);
    q.set('y', S.target.y);
    let useB = false;
    if (useBin() && S.bins && S.bins.nbin > 0) {
      let k = S.target.bin;
      if (k == null || !(k >= 0 && k < S.bins.nbin)) k = binAt(S.target.x, S.target.y);
      if (k == null) k = 0;
      S.target.bin = k;
      q.set('bin', k);
      useB = true;
    }
    if (!useB) {
      const ap = apPix();
      if (ap > 0) q.set('aperture', ap);
    }
  } else {
    q.set('row', S.target.row || 0);
  }
  try {
    const seq = (S.specSeq = (S.specSeq || 0) + 1);
    const _sp = await api('/api/spectrum?' + q.toString());
    if (seq !== S.specSeq) return;   // 连续切 bin 时丢弃过期响应
    S.spec = _sp;
    S.target.aperture = S.spec.aperture || 0;
    S.target.bin = (S.spec.bin == null ? null : S.spec.bin);
    if (S.spec.x != null) { S.target.x = S.spec.x; S.target.y = S.spec.y; }
    // ★ bin/孔径的质心由后端给：同步把准星画到新目标上
    //   （否则切 bin 时准星还停在上一个 bin 的位置，直到下一次重绘）
    if (S.meta.kind === 'cube' && S.image) drawMap();
    $('pixelBadge').textContent =
      S.meta.kind === 'cube'
        ? (S.spec.bin != null ? `bin #${S.spec.bin}` : `x=${S.spec.x}, y=${S.spec.y}`) +
          (S.spec.aperture ? `, r=${fmtAp(S.spec.aperture)}` : '') +
          (S.spec.npix > 1 ? ` · ${S.spec.npix}px` : '') +
          snrTag(S.spec.snr, ', ')
        : (S.spec.snr != null ? t('target.snr', { v: fmtSNR(S.spec.snr) }) : t('tab.spec'));
    $('pixelBadge').classList.remove('hidden');
    updateTargetInfo();
    drawSpectrum(S.spec);
    if (switchView) showView('spec');
    // 拟合波段默认全谱（若仍是旧文件的全谱值就更新）
    if (!$('fitMin').value || parseFloat($('fitMin').value) < S.spec.wave_min) {
      $('fitMin').value = Number(S.spec.wave_min).toFixed(1);
    }
    if (!$('fitMax').value || parseFloat($('fitMax').value) > S.spec.wave_max) {
      $('fitMax').value = Number(S.spec.wave_max).toFixed(1);
    }
    updateDlamUI();
  } catch (e) { toast(e.message, 'err'); }
}

// S/N 显示：观测 5500 Å 波段（5300–5700 Å，与 PowerBin 目标 S/N 同一定义）
function fmtSNR(v) {
  const x = Number(v);
  if (!isFinite(x)) return '—';
  return x >= 10 ? x.toFixed(0) : x.toFixed(1);
}
function snrTag(v, sep) {
  const x = Number(v);
  if (v == null || !isFinite(x)) return '';
  return (sep || ', ') + t('target.snr', { v: fmtSNR(x) });
}

function updateTargetInfo() {
  if (!S.meta) { $('targetInfo').textContent = t('target.none'); return; }
  if (S.meta.kind === 'cube') {
    $('targetInfo').innerHTML = (S.spec && S.spec.bin != null)
      ? t('target.bin', { bin: S.spec.bin, npix: S.spec.npix }) +
        snrTag(S.spec.snr, ', ')
      : t('target.pixel', { x: S.target.x, y: S.target.y }) +
        (S.spec && S.spec.aperture ? t('target.ap', { ap: fmtAp(S.spec.aperture) }) : '') +
        (S.spec ? t('target.npix', { npix: S.spec.npix }) : '') +
        snrTag(S.spec && S.spec.snr, ', ');
  } else {
    $('targetInfo').innerHTML =
      t('target.spec') + (S.meta.nrows > 1 ? t('target.row', { row: S.target.row }) : '');
  }
}

function axisZ() {
  const auto = $('zAuto').checked;
  const z = parseFloat($('zValue').value);
  return auto || !isFinite(z) ? 0 : z;
}

function drawSpectrum(sp) {
  if (!sp) return;
  const useRest = $('waveRestChk').checked;
  const z = axisZ();
  const n = sp.wave.length;
  const x = new Array(n);
  for (let i = 0; i < n; i++) {
    x[i] = useRest ? sp.wave[i] / (1 + z) : sp.wave[i];
  }
  const flux = sp.flux;
  const err = sp.err;
  const mask = sp.mask;

  const up = new Array(n), dn = new Array(n);
  for (let i = 0; i < n; i++) {
    if (flux[i] === null || err[i] === null) { up[i] = null; dn[i] = null; continue; }
    up[i] = flux[i] + err[i];
    dn[i] = flux[i] - err[i];
  }
  const traces = [];
  if ($('showErr').checked) {
    traces.push({
      x, y: up, type: 'scatter', mode: 'lines', line: { width: 0 },
      hoverinfo: 'skip', showlegend: false,
    });
    traces.push({
      x, y: dn, type: 'scatter', mode: 'lines', line: { width: 0 },
      fill: 'tonexty', fillcolor: 'rgba(70,120,220,.14)',
      hoverinfo: 'skip', name: '1σ',
    });
  }
  traces.push({
    x, y: flux, type: 'scatter', mode: 'lines',
    line: { color: '#101827', width: 1.1 }, name: 'flux',
    hovertemplate: 'λ=%{x:.2f} Å<br>flux=%{y:.4g}<extra></extra>',
  });
  if ($('showMask').checked) {
    const mx = [], my = [];
    for (let i = 0; i < n; i++) {
      if (mask[i]) { mx.push(x[i]); my.push(flux[i]); }
    }
    if (mx.length) {
      traces.push({
        x: mx, y: my, type: 'scatter', mode: 'markers',
        marker: { color: '#e5484d', size: 3.2, symbol: 'x' },
        name: `mask (${mx.length})`,
        hovertemplate: 'masked λ=%{x:.2f}<extra></extra>',
      });
    }
  }

  // ---- 拟合结果直接叠加在同一张光谱图上（不再另开子图） ----
  const r = S.result;
  const hasFit = r && r.wave_rest && r.wave_rest.length && r.bestfit;
  if (hasFit) {
    const xf = useRest ? r.wave_rest
      : r.wave_rest.map(v => (v === null ? null : v * (1 + z)));
    const push = (y, name, color, dash, width) => {
      if (!y || !y.length) return;
      traces.push({
        x: xf, y, type: 'scatter', mode: 'lines', name,
        line: { color, width: width || 1.4, dash: dash || 'solid' },
        hovertemplate: `${name}: %{y:.4g}<extra></extra>`,
      });
    };
    const gasMax = (r.gas_model || []).reduce(
      (m, v) => (v !== null && isFinite(v) ? Math.max(m, Math.abs(v)) : m), 0);
    push(r.bestfit, 'best fit', '#e07b00', 'solid', 1.8);
    push(r.stellar, 'stellar', '#d64545', 'dash', 1.2);
    if (gasMax > 0) push(r.gas_model, 'gas', '#8e44ad', 'solid', 1.0);
  }

  // ---- 谱线标注（下拉框选择）----
  // 观测系标注用拟合/测量得到的 z；没有结果时退回手动 z（auto 时为 0）
  const lineZ = (S.result && isFinite(S.result.z)) ? Number(S.result.z) : z;
  let xlo = Infinity, xhi = -Infinity;
  for (let i = 0; i < n; i++) {
    const v = x[i];
    if (v === null || !isFinite(v)) continue;
    if (v < xlo) xlo = v;
    if (v > xhi) xhi = v;
  }
  const la = (isFinite(xlo) && isFinite(xhi) && xhi > xlo)
    ? lineAnnotations(xlo, xhi, useRest, lineZ)
    : { shapes: [], annotations: [] };

  const layout = {
    margin: { l: 58, r: 16, t: 30, b: 44 },
    paper_bgcolor: '#fff', plot_bgcolor: '#fff',
    font: { size: 11, family: 'Segoe UI, system-ui, sans-serif' },
    title: {
      text: t(useRest ? 'spec.title.rest' : 'spec.title.obs') + ' ' +
        (sp.kind === 'cube'
          ? (sp.bin != null
              ? t('spec.title.bin', { bin: sp.bin, npix: sp.npix })
              : t('spec.title.pix', { x: sp.x, y: sp.y }) +
                (sp.aperture ? ` · r=${fmtAp(sp.aperture)} (${sp.npix} px)` : ''))
          : t('tab.spec')),
      font: { size: 12 }, x: 0.01,
    },
    xaxis: { title: t(useRest ? 'spec.title.axisRest' : 'spec.title.axisObs'),
             gridcolor: '#eef1f6', zeroline: false },
    yaxis: { title: t('spec.title.flux'), gridcolor: '#eef1f6', zeroline: false },
    legend: { orientation: 'h', y: 1.08, x: 1, xanchor: 'right', font: { size: 10 } },
    hovermode: 'closest',
    shapes: la.shapes,
    annotations: la.annotations,
    uirevision: (S.path || '') + '|' + (useRest ? 'r' : 'o') + '|' +
      (sp.bin == null ? '' : 'b' + sp.bin) + '|' +
      (sp.aperture == null ? '' : 'a' + Number(sp.aperture).toFixed(3)),
  };
  Promise.resolve(Plotly.react('spectrumPlot', traces, layout,
    { responsive: true, displaylogo: false,
      modeBarButtonsToRemove: ['lasso2d', 'select2d', 'autoScale2d'] }))
    .then(() => {
      // 渲染后拿真实绘图区宽度；与估算差太多就重排一次标签（避免车道/间距算错）
      const el = $('spectrumPlot');
      const fl = el && el._fullLayout;
      if (!fl) return;
      const w = fl.width - fl.margin.l - fl.margin.r;
      if (!(w > 200)) return;
      const prev = S.plotW;
      S.plotW = w;
      if (Math.abs(w - prev) > 40 && la.annotations.length) {
        const la2 = lineAnnotations(xlo, xhi, useRest, lineZ);
        Plotly.relayout('spectrumPlot',
          { shapes: la2.shapes, annotations: la2.annotations });
      }
      // 缩放后按当前可视范围重新排布谱线标签（密集区域放大就能全看清）
      el._lineCtx = { useRest, z: lineZ };
      if (!el._lineEvBound) {
        el._lineEvBound = true;
        el.on('plotly_relayout', ev => {
          const keys = Object.keys(ev || {});
          if (!keys.some(k => k.indexOf('xaxis.range') === 0 || k === 'xaxis.autorange')) return;
          clearTimeout(el._lineT);
          el._lineT = setTimeout(() => {
            const f = el._fullLayout;
            if (!f || !f.xaxis || !f.xaxis.range) return;
            const r = f.xaxis.range.map(Number);
            if (!isFinite(r[0]) || !isFinite(r[1]) || r[0] === r[1]) return;
            const c = el._lineCtx || {};
            const l2 = lineAnnotations(Math.min(r[0], r[1]), Math.max(r[0], r[1]),
                                       !!c.useRest, Number(c.z) || 0);
            Plotly.relayout(el, { shapes: l2.shapes, annotations: l2.annotations });
          }, 200);
        });
      }
    })
    .catch(() => {});
}

/* ------------------------------------------------------------------ */
/* 视图切换                                                            */
/* ------------------------------------------------------------------ */
function showView(v) {
  if (!v || !S.meta) return;
  S.view = v;
  $('mapView').classList.toggle('hidden', v !== 'map');
  $('spectrumView').classList.toggle('hidden', v !== 'spec');
  $('resultView').classList.toggle('hidden', v !== 'result');
  $('emptyView').classList.toggle('hidden', !!(S.meta));
  $$('#displayTabs button').forEach(b =>
    b.classList.toggle('active', b.dataset.view === v));
  const ls = $('lineSel');
  if (ls) ls.style.display = (v === 'spec' && S.lines) ? '' : 'none';
  if (v === 'map' && S.image) requestAnimationFrame(drawMap);
  if (v === 'spec' && S.spec) requestAnimationFrame(() => {
    if (!window.Plotly) return;
    Plotly.Plots.resize($('spectrumPlot'));
    // 切到光谱视图后绘图区宽度可能变了，需要按真实宽度重排谱线标签
    const el = $('spectrumPlot');
    const fl = el && el._fullLayout;
    if (!fl) return;
    const w = fl.width - fl.margin.l - fl.margin.r;
    if (w > 200 && Math.abs(w - S.plotW) > 40) {
      S.plotW = w;
      drawSpectrum(S.spec);
    }
  });
}

/* ------------------------------------------------------------------ */
/* 模板                                                                */
/* ------------------------------------------------------------------ */
function setTplMsg(msg) {
  const e = $('tplDlMsg');
  if (e) e.textContent = msg || '';
}

// spectra_emiles_9.0.npz -> emiles（与后端 _short_name 一致）
function guessTplName(filename) {
  const base = String(filename || '').split('/').pop();
  let s = base.replace(/\.npz$/i, '');
  if (s.startsWith('spectra_')) {
    s = s.slice('spectra_'.length);
    const parts = s.split('_');
    if (parts.length >= 2 && /^\d+(\.\d+)*$/.test(parts[parts.length - 1])) {
      parts.pop();
    }
    s = parts.join('_');
  }
  return s;
}

async function loadTemplates() {
  try {
    const j = await api('/api/templates');
    S.templates = j.templates || [];
    const sel = $('templateSel');
    const old = sel.value;
    sel.innerHTML = '';
    S.templates.forEach(tp => {
      const o = document.createElement('option');
      o.value = tp.name;
      o.textContent = `${tp.label} (${tp.shape[1]}×${tp.shape[2]})` +
        (tp.builtin ? '' : ' • local');
      sel.appendChild(o);
    });
    if (old && [...sel.options].some(o => o.value === old)) sel.value = old;
    else if (S.templates.some(tp => tp.name === 'emiles')) sel.value = 'emiles';
    $('tplStatus').innerHTML = S.templates.length
      ? t('tpl.status.count', {
          n: S.templates.length,
          names: S.templates.map(tp => esc(tp.name)).join(', '),
        })
      : `<span style="color:#b45309">${t('tpl.status.none')}</span>`;
    updateDlamUI();
  } catch (e) {
    $('tplStatus').textContent = t('tpl.status.fail', { msg: e.message });
  }
}

/* ------------------------------------------------------------------ */
/* 谱线标注（line_use.dat，静止系真空波长）                            */
/* ------------------------------------------------------------------ */
async function loadLines() {
  try {
    const j = await api('/api/lines');
    S.lines = j;
    const ls = $('lineSel');
    if (ls) ls.style.display = S.view === 'spec' ? '' : 'none';
  } catch (e) {
    S.lines = null;
    const ls = $('lineSel');
    if (ls) ls.style.display = 'none';
  }
}

// 常用吸收线（拟合波段内常见的恒星吸收特征）；排除 [Fe II] 之类发射线
const COMMON_ABS_RE = /^(H[αβγδεζ]|H(8|9|10|11|12|eps|delta|gamma|beta|alpha)|Ca\s*II|Mg\s*I|Na\s*I|G-?band)/i;

// 显示名（把 line_use.dat / ppxf_sew 里的名字压缩成图上能看清的写法）
function lineLabel(name) {
  let s = String(name || '').trim();
  const map = {
    Halpha: 'Hα', Hbeta: 'Hβ', Hgamma: 'Hγ', Hdelta: 'Hδ',
    Heps: 'Hε', H10: 'H10', H9: 'H9', H8: 'H8',
  };
  if (map[s]) return map[s];
  // 双线组名 xxx_d -> 去掉后缀，波长决定具体标哪一条
  s = s.replace(/_d$/, '');
  // [OIII]5007 -> [O III] 5007
  s = s.replace(/^\[([A-Za-z]+)\s*(III|II|I|IV)\s*\]?\s*(\d+)$/i,
    (m, el, ion, lam) =>
      `[${el[0].toUpperCase()}${el.slice(1).toLowerCase()} ${ion.toUpperCase()}] ${lam}`);
  // Ca II K / Mg I b 5167 / Na I D 5890 适度缩写
  s = s.replace(/^Ca II (K|H)$/, 'Ca $1')
       .replace(/^Mg I b (\d+)$/, 'Mg b $1')
       .replace(/^Na I D (\d+)$/, 'Na D $1')
       .replace(/^G-band$/, 'G band');
  return s;
}

// 当前下拉选择对应的谱线列表（已按波长排序、去重）
function selectedLines() {
  const j = S.lines;
  if (!j || !j.lines || !j.lines.length) return [];
  const mode = $('lineSel') ? $('lineSel').value : '';
  if (!mode) return [];
  const fitEm = (j.fit_em || []).map(v => Number(v.lam));
  // 波长差 < 2 Å 视为同一条（两边波长来源略有差异）
  const has = l => fitEm.some(f => Math.abs(f - Number(l.lam)) < 2.0);
  const out = [];
  for (const l of j.lines) {
    const nm = String(l.name || '');
    const kind = String(l.kind || '').toLowerCase();
    const isEm = kind.includes('em'), isAbs = kind.includes('abs');
    const commonAbs = !nm.startsWith('[') && COMMON_ABS_RE.test(nm);
    let ok = false;
    if (mode === 'fit') ok = has(l);
    else if (mode === 'fitabs') ok = has(l) || commonAbs;
    else if (mode === 'abs') ok = commonAbs;
    else if (mode === 'em') ok = isEm;
    else if (mode === 'allabs') ok = isAbs;
    else if (mode === 'all') ok = true;
    if (!ok) continue;
    out.push({
      lam: Number(l.lam), name: String(l.name || ''),
      em: isEm || has(l),
      fit: has(l),
      common: commonAbs,
    });
  }
  out.sort((a, b) => a.lam - b.lam);
  // 相邻 < 2 Å 视为同一条（双线组/重复条目）
  const dedup = [];
  for (const l of out) {
    const p = dedup[dedup.length - 1];
    if (p && Math.abs(p.lam - l.lam) < 2.0) {
      if (l.fit && !p.fit) { p.fit = true; p.em = p.em || l.em; }
      if (l.common) p.common = true;
      if (!p.name || p.name === l.name) continue;
      if (lineLabel(l.name).length < lineLabel(p.name).length) p.name = l.name;
      continue;
    }
    dedup.push(l);
  }
  return dedup;
}

// 构造谱线的 layout.shapes / annotations
function lineAnnotations(xlo, xhi, useRest, z) {
  const lines = selectedLines();
  if (!lines.length) return { shapes: [], annotations: [] };
  const shapes = [], annotations = [];
  const toX = lam => (useRest ? lam : lam * (1 + z));
  const span = xhi - xlo;
  // 1) 所有落在可视范围的谱线都画竖线
  const inView = [];
  for (const l of lines) {
    const xv = toX(l.lam);
    if (!(xv >= xlo && xv <= xhi)) continue;
    inView.push({ ...l, xv });
    shapes.push({
      type: 'line', xref: 'x', yref: 'paper', x0: xv, x1: xv, y0: 0, y1: 1,
      line: {
        color: l.em ? 'rgba(214,69,69,.55)' : 'rgba(47,111,208,.5)',
        width: l.fit ? 1.2 : 0.8,
        dash: l.em ? 'dot' : 'dash',
      },
      layer: 'below',
    });
  }

  // 2) 每条线都标注：按像素间距分若干"车道"竖向错开，避免挤在一起
  const labelOf = l => lineLabel(l.name);
  const score = l => {
    const t = labelOf(l);
    if (l.fit) return /^H[αβ]$/.test(t) ? 4 : (/^H[γδ]$/.test(t) ? 3 : 2.5);
    return l.common ? 2 : 1;
  };
  const el0 = $('spectrumPlot');
  const plotW = (S.plotW > 200 ? S.plotW
    : (el0 && el0.clientWidth > 200 ? el0.clientWidth - 80 : 900));
  const pxPerA = plotW / span;
  const gapX = 13 / pxPerA;            // 相邻标签至少相隔 ~13 px
  // 线多的时候多开几条"车道"，按 paper 坐标等分高度
  const nLane = inView.length > 90 ? 5 : (inView.length > 45 ? 4 : 3);
  const laneH = Math.min(0.34, 1.02 / nLane);
  const LANE_Y = Array.from({ length: nLane }, (_, k) => 1.0 - k * laneH);
  const lanes = LANE_Y.map(() => []);
  // 重要的先占位（先占上层车道），保证 Hα/Hβ 等不会被挤掉
  const cand = inView.map(l => ({ l, pri: score(l) }))
    .sort((a, b) => (b.pri - a.pri) || (a.l.xv - b.l.xv));
  const placed = [];
  for (const c of cand) {
    let best = -1, bestDist = -Infinity;
    for (let k = 0; k < lanes.length; k++) {
      const d = lanes[k].length
        ? Math.min(...lanes[k].map(x => Math.abs(x - c.l.xv))) : Infinity;
      if (d >= gapX) { best = k; break; }
      if (d > bestDist) { bestDist = d; best = k; }
    }
    lanes[best].push(c.l.xv);
    placed.push({ ...c, lane: best });
  }
  placed.sort((a, b) => (a.lane - b.lane) || (a.l.xv - b.l.xv));
  for (const { l, pri, lane } of placed) {
    annotations.push({
      x: l.xv, y: LANE_Y[lane], xref: 'x', yref: 'paper',
      text: labelOf(l),
      showarrow: false, xanchor: 'left', yanchor: 'top',
      textangle: -90,
      font: {
        size: nLane >= 5 ? 7.5 : (pri >= 2 ? 9.5 : 8),
        color: l.em ? 'rgba(178,44,44,.95)' : 'rgba(38,88,170,.9)',
      },
    });
  }
  return { shapes, annotations };
}

/* ------------------------------------------------------------------ */
/* 红移                                                                */
/* ------------------------------------------------------------------ */
async function autoRedshift() {
  if (!S.spec) return toast(t('msg.pickSpectrum'), 'err');
  $('autoZBtn').disabled = true;
  $('autoZBtn').textContent = t('status.autoz');
  try {
    const j = await postJSON('/api/redshift', {
      x: S.target.x, y: S.target.y, row: S.target.row,
      bin: useBin() ? S.target.bin : null,
      aperture: useBin() ? 0 : apPix(),
      template: $('templateSel').value,
      obs_vacuum: $('obsVacSel') ? $('obsVacSel').value : 'air',
      template_vacuum: $('tplVacSel').value,
      z_range: [-0.01, 0.2],
    });
    $('zAuto').checked = false;
    $('zValue').value = Number(j.z).toFixed(6);
    syncSliders();
    toast(`z = ${Number(j.z).toFixed(6)} (${j.method}, SNR=${Number(j.snr).toFixed(1)})`);
    drawSpectrum(S.spec);
  } catch (e) { toast(e.message, 'err'); }
  $('autoZBtn').disabled = false;
  $('autoZBtn').textContent = t('fit.autoZ');
}

/* ------------------------------------------------------------------ */
/* 拟合                                                                */
/* ------------------------------------------------------------------ */
function collectCfg() {
  const fwhmMode = $('fwhmMode').value;
  const fwhm = { mode: fwhmMode };
  if (fwhmMode === 'lambda/R') {
    fwhm.R = parseFloat($('fwhmR').value) || 2000;
  } else if (fwhmMode === 'constant') {
    fwhm.value = parseFloat($('fwhmVal').value) || 2.5;
  } else if (fwhmMode === 'function') {
    fwhm.expr = ($('fwhmFunc').value || 'lam/7000').trim();
  } else if (fwhmMode === 'scatter') {
    fwhm.path = ($('fwhmLsfPath').value || '').trim();
    fwhm.hdu = 'LSF';
  }
  const zAuto = $('zAuto').checked;
  const z = parseFloat($('zValue').value);
  return {
    template: $('templateSel').value,
    engine: $('engineSel').value,
    obs_vacuum: $('obsVacSel') ? $('obsVacSel').value : 'air',
    fwhm,
    z: zAuto ? 'auto' : (isFinite(z) ? z : 0),
    z_range: [-0.01, 0.2],
    moments: parseInt($('momentsSel').value, 10) || 2,
    degree: parseInt($('degreeIn').value, 10),
    fit_min: parseFloat($('fitMin').value),
    fit_max: parseFloat($('fitMax').value),
    vmin: parseFloat($('vminIn').value),
    vmax: parseFloat($('vmaxIn').value),
    sig_min: parseFloat($('sigMinIn').value),
    sig_max: parseFloat($('sigMaxIn').value),
    gas: $('gasChk').checked,
    tie_balmer: $('tieBalmerChk').checked,
    limit_doublets: $('limitDoubletsChk').checked,
    mask_sync: $('maskSyncChk').checked,
    noise: $('noiseSel').value,
    regul: parseFloat($('regulIn').value) || 0,
    dlam: parseFloat($('dlamIn').value) || 1e-4,
    dlam_mode: $('dlamModeSel').value,
    dust: collectDustCfg(),
    template_vacuum: $('tplVacSel').value,
  };
}

async function startFit() {
  if (!S.spec) return toast(t('msg.pickSpectrum'), 'err');
  const cfg = collectCfg();
  const ap = apPix();
  const target = {
    x: S.target.x, y: S.target.y, row: S.target.row,
    bin: useBin() ? S.target.bin : null,
    aperture: useBin() ? 0 : ap,
  };
  // ★ 叠加图用的是 S.spec：若取谱方式/bin/孔径改过但未取谱，拟合用的是新谱，
  //   而图上还是旧谱。先重新取一次谱。
  try {
    if (S.meta && S.meta.kind === 'cube') {
      const stale = useBin()
        ? ((S.spec.bin == null ? null : S.spec.bin) !== target.bin)
        : (Math.abs((S.spec.aperture || 0) - ap) > 1e-9);
      if (stale) {
        $('fitMsg').textContent = t('fit.msg.updateSpec');
        await loadSpectrum(false);
      }
    }
  } catch (e) { /* 取谱失败不阻断拟合 */ }
  $('fitBtn').disabled = true;
  $('fitError').classList.add('hidden');
  $('fitProgressWrap').classList.remove('hidden');
  $('fitProgress').style.width = '2%';
  $('fitMsg').textContent = t('fit.msg.submit');
  try {
    const j = await postJSON('/api/fit', { target, cfg });
    S.jobId = j.job_id;
    pollFit();
  } catch (e) {
    $('fitBtn').disabled = false;
    showFitError(e.message);
  }
}

function pollFit() {
  clearTimeout(S.timer);
  const tick = async () => {
    try {
      const j = await api('/api/fit/status/' + S.jobId);
      $('fitProgress').style.width = Math.max(2, (j.progress || 0) * 100) + '%';
      $('fitMsg').textContent = j.pkey
        ? t('fit.prog.' + j.pkey, j.pargs || {})
        : (j.message || j.status);
      if (j.status === 'done') {
        $('fitBtn').disabled = false;
        $('fitProgress').style.width = '100%';
        S.result = j.result;
        renderResult(j.result, j.target);
        return;
      }
      if (j.status === 'error') {
        $('fitBtn').disabled = false;
        showFitError(j.error || j.message || t('msg.fitFail'));
        return;
      }
      S.timer = setTimeout(tick, 800);
    } catch (e) {
      $('fitBtn').disabled = false;
      showFitError(e.message);
    }
  };
  tick();
}

function showFitError(msg) {
  $('fitError').textContent = msg;
  $('fitError').classList.remove('hidden');
  $('fitMsg').textContent = t('fit.msg.failed');
}

function resetResultPanel() {
  $('resultEmpty').classList.remove('hidden');
  $('resultSummary').classList.add('hidden');
  $('resultTabsWrap').classList.add('hidden');
  $$('#resultCard .rview').forEach(e => e.classList.add('hidden'));
  $('exportRow').style.display = 'none';
  $('fitProgressWrap').classList.add('hidden');
  $('fitError').classList.add('hidden');
  $('refreshTip').classList.add('hidden');
  S.result = null;
  requestAnimationFrame(syncPanelHeight);
}

function renderResult(r, target) {
  S._fitTarget = target;
  $('resultEmpty').classList.add('hidden');
  $('resultSummary').classList.remove('hidden');
  $('resultTabsWrap').classList.remove('hidden');

  const kv = [
    [t('r.target'), target || '—'],
    [t('r.template'), `${r.template.name}`],
    [t('r.engine'), r.engine],
    [t('r.z'), fmt(r.z, 6) +
      (r.redshift ? ` (${r.redshift.method})` : '')],
    ['FWHM', r.fwhm.text],
    [t('r.wavesys'), (() => {
      const ov = $('obsVacSel') ? $('obsVacSel').value : 'air';
      return t('r.wavesys.v', { obs: t(ov === 'vacuum' ? 'data.vacuum' : 'data.air') });
    })()],
    [t('r.velscale'), r.velscale != null
      ? (() => {
          const mode = String(r.dlam_mode || '').toLowerCase();
          const srcKey = mode.startsWith('obs') ? 'dlam.src.obs'
            : (mode.startsWith('custom') || mode.startsWith('manual')) ? 'dlam.src.custom'
            : 'dlam.src.tpl';
          return t(srcKey) + ' · ' + t('r.velscale.v', {
            v: Number(r.velscale).toFixed(1),
            dlam: Number(r.dlam).toExponential(2),
          });
        })()
      : '—'],
    (r.dust && r.dust.length) ? [t('r.dust'), r.dust.map(d => {
          const v = (d.names || []).map((n, i) =>
            `${n}=${fmt((d.values || [])[i], 3)}`).join(', ');
          return `${d.tag}: ${v || '—'}`;
        }).join('; ')] : null,
    ['χ²/dof', fmt(r.chi2, 3)],
    ['dof', r.dof],
    ['RMS', fmt(r.rms, 4)],
    [t('r.snr'), fmt(r.snr, 2)],
    [t('r.npix'), `${r.n_good} / ${r.n_pixels}`],
    [t('r.scale'), fmt(r.mean_gal, 4)],
    [t('r.secs'), `${r.fit_seconds.toFixed(1)} s`],
  ].filter(Boolean);
  $('resultSummary').innerHTML = kv.map(([k, v]) =>
    `<div class="pair"><span class="k">${esc(k)}</span>` +
    `<span class="v">${esc(v)}</span></div>`).join('');

  renderTables(r);

  // ★ 拟合结果直接叠加到主光谱图（不另开子图）
  S.result = r;
  if (r.z !== null && r.z !== undefined && isFinite(r.z)) {
    $('zAuto').checked = false;
    $('zValue').value = Number(r.z).toFixed(6);
    syncSliders();
  }
  if (S.spec) {
    drawSpectrum(S.spec);
    showView('result');
  }

  $('nativeImg').src = r.plot_png || '';
  $('nativeImg').style.display = r.plot_png ? '' : 'none';
  $('weightsImg').src = r.weight_png || '';
  $('weightsImg').style.display = r.weight_png ? '' : 'none';
  renderStellarStats(r.stellar_stats);
  // 日志：后端每条日志带 [key, args]，按当前语言渲染（无 key 的原样输出）
  const rawLog = r.log || [];
  const logI18n = r.log_i18n || [];
  $('fitLog').textContent = rawLog.map((txt, i) => {
    const e = logI18n[i];
    if (!e || !e[0]) return txt;
    const a = Object.assign({}, e[1] || {});
    if (a.src_kind) {
      const m = String(a.src_kind).toLowerCase();
      const k = m.startsWith('obs') ? 'dlam.src.obs'
        : (m.startsWith('custom') || m.startsWith('manual')) ? 'dlam.src.custom'
        : 'dlam.src.tpl';
      a.src = t(k);
      delete a.src_kind;
    }
    if (e[0] === 'dust' && a.mode) a.mode = t('dust.short.' + a.mode);
    if (e[0] === 'fwhm' && a.mode) {
      const k = 'log.fwhm.' + String(a.mode).replace(/\//g, '_');
      const s = (I18N.dict[k] != null || I18N.fallback[k] != null) ? t(k, a) : null;
      if (s) a.desc = s;
    }
    return t('log.' + e[0], a);
  }).join('\n');

  const jid = S.jobId;
  if (typeof window.sewExport === 'function') {
    $('exportJson').onclick = (e) => { e.preventDefault(); window.sewExport(jid, 'json'); };
    $('exportFits').onclick = (e) => { e.preventDefault(); window.sewExport(jid, 'fits'); };
    $('exportJson').removeAttribute('href');
    $('exportFits').removeAttribute('href');
  } else {
    $('exportJson').href = `/api/fit/export/${jid}?fmt=json`;
    $('exportFits').href = `/api/fit/export/${jid}?fmt=fits`;
  }
  $('exportJson').download = `sew_fit_${jid}.json`;
  $('exportFits').download = `sew_fit_${jid}.fits`;
  $('exportRow').style.display = '';

  showResultTab('native');
  // 浏览器端：把结果存进工作区（IndexedDB）
  if (typeof window.sewOnResult === 'function') {
    try { window.sewOnResult(r, S.jobId, target); } catch (e) { console.warn(e); }
  }
  // 完成后收起进度行（结果面板已有完整信息），避免右栏比面板高出一截
  $('fitProgressWrap').classList.add('hidden');
  requestAnimationFrame(syncPanelHeight);
}

// 模板权重的平均年龄 / 金属丰度（光度加权 & 质量加权，后端算）
function renderStellarStats(st) {
  const el = $('weightsStats');
  if (!el) return;
  if (!st || (!st.light && !st.mass)) {
    el.innerHTML = `<span class="muted">${t('r.noMasses')}</span>`;
    return;
  }
  const ageTxt = d => {
    const g = Math.pow(10, d.log_age - 9);
    return g >= 1 ? g.toFixed(2) : g.toPrecision(2);
  };
  const cell = (d, fn) => (d ? fn(d) : '—');
  const head = `<thead><tr><th></th><th>${t('r.lightW')}</th>` +
    `<th>${t('r.massW')}</th></tr></thead>`;
  const rows = [
    [t('r.logAge'), d => d.log_age.toFixed(3)],
    [`${t('r.age')} [Gyr]`, ageTxt],
    [t('r.metal'), d => d.metal.toFixed(3)],
  ].map(([lab, fn]) => `<tr><td>${esc(lab)}</td>` +
    `<td>${esc(cell(st.light, fn))}</td>` +
    `<td>${esc(cell(st.mass, fn))}</td></tr>`).join('');
  el.innerHTML = `<table>${head}<tbody>${rows}</tbody></table>` +
    `<div class="note">${t('r.logAgeNote')}</div>`;
}

function renderTables(r) {
  let html = `<table><thead><tr><th>${t('r.comp')}</th><th>V [km/s]</th>` +
    '<th>σ [km/s]</th><th>h3</th><th>h4</th></tr></thead><tbody>';
  (r.components || []).forEach(c => {
    html += `<tr><td>${esc(c.name)}</td><td>${fmt(c.V, 2)}</td>` +
      `<td>${fmt(c.sigma, 2)}</td>` +
      `<td>${c.h3 === undefined ? '—' : fmt(c.h3, 3)}</td>` +
      `<td>${c.h4 === undefined ? '—' : fmt(c.h4, 3)}</td></tr>`;
  });
  html += '</tbody></table>';
  $('compTable').innerHTML = html;

  if (!r.gas || !r.gas.length) {
    $('gasTable').innerHTML =
      `<div class="muted small" style="padding:6px">${t('r.gasNone')}</div>`;
    return;
  }
  // 每列一条气体线（行 = 量）：线名横排，放不下就出现左右滚动条
  const gas = r.gas;
  const shortComp = c => String(c == null ? '' : c)
    .replace(/^gas\s*\((.*)\)$/, '$1')
    .replace(/^gas\s*#/, '#')
    .replace(/^stellar$/, 'stellar');
  const rows = [
    ['Flux', g => fmt(g.flux, 4)],
    ['±', g => fmt(g.flux_err, 4)],
    ['EW [Å]', g => fmt(g.ew, 3)],
    ['V [km/s]', g => fmt(g.V, 1)],
    ['σ [km/s]', g => fmt(g.sigma, 1)],
    [t('r.comp'), g => (g.component == null ? '—' : shortComp(g.component))],
  ];
  let gt = `<div class="gas-scroll"><table><thead><tr><th>${t('r.qty')}</th>`;
  gas.forEach(g => { gt += `<th>${esc(g.name)}</th>`; });
  gt += '</tr></thead><tbody>';
  rows.forEach(([label, fn]) => {
    gt += `<tr><td>${esc(label)}</td>`;
    gas.forEach(g => { gt += `<td>${esc(fn(g))}</td>`; });
    gt += '</tr>';
  });
  gt += '</tbody></table></div>';
  $('gasTable').innerHTML = gt;
}

function showResultTab(name) {
  $$('#resultTabsWrap button').forEach(b =>
    b.classList.toggle('active', b.dataset.rtab === name));
  const map = {
    native: 'rNative', weights: 'rWeights',
    params: 'rParams', gas: 'rGas', log: 'rLog',
  };
  Object.entries(map).forEach(([k, id]) =>
    $(id).classList.toggle('hidden', k !== name));
}
