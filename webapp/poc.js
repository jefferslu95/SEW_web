/* ------------------------------------------------------------------
   SEW web — 浏览器端拟合 PoC（主线程：UI + 与 worker 的 RPC）
   ------------------------------------------------------------------ */
const $ = (id) => document.getElementById(id);
const logBox = $('log');
const stateTag = $('state');

let worker = null;
let ready = false;

function log(msg) {
  logBox.textContent += msg + '\n';
  logBox.scrollTop = logBox.scrollHeight;
}

function setState(s) { stateTag.textContent = s; }

function boot() {
  worker = new Worker('worker.js');
  worker.onmessage = (ev) => {
    const m = ev.data || {};
    if (m.type === 'log') { log(m.msg); return; }
    if (m.type === 'ready') {
      ready = true;
      $('runBtn').disabled = false;
      setState('就绪');
      return;
    }
    if (m.type === 'error') {
      log('❌ ' + m.msg);
      $('runBtn').disabled = false;
      setState('出错');
      return;
    }
    if (m.type === 'result') { showResult(m.result); }
  };
  worker.onerror = (e) => { log('❌ worker: ' + e.message); setState('出错'); };
  setState('加载依赖…');
  worker.postMessage({ type: 'init', base: './' });
}

function fmt(x, n = 3) {
  return (x === null || x === undefined || Number.isNaN(Number(x)))
    ? '—' : Number(x).toFixed(n);
}

function showResult(r) {
  $('runBtn').disabled = false;
  if (!r.ok) {
    setState('拟合失败');
    log('❌ ' + (r.error || 'unknown'));
    return;
  }
  setState('完成');
  const stats = r.stellar_stats && r.stellar_stats.light;
  $('summary').textContent = [
    `template : ${r.template}`,
    `z        : ${fmt(r.z, 6)}`,
    `velscale : ${fmt(r.velscale, 1)} km/s (dlam=${Number(r.dlam).toExponential(2)})`,
    `chi2/dof : ${fmt(r.chi2)}  (dof=${r.dof})`,
    `RMS      : ${r.rms}`,
    `S/N      : ${fmt(r.snr, 2)}`,
    `time     : ${fmt(r.fit_seconds, 1)} s`,
    stats ? `light    : <logAge>=${stats.log_age.toFixed(3)}  [M/H]=${stats.metal.toFixed(3)}` : '',
    '',
    (r.log || []).slice(0, 12).join('\n'),
  ].filter(Boolean).join('\n');

  if (r.plot_png) {
    const img = $('out');
    img.src = String(r.plot_png).startsWith('data:')
      ? r.plot_png : 'data:image/png;base64,' + r.plot_png;
    img.style.display = 'block';
  }
}

function readFile(input) {
  return new Promise((res, rej) => {
    const f = input.files && input.files[0];
    if (!f) return rej(new Error('未选择文件'));
    f.arrayBuffer().then((b) => res({ name: f.name, bytes: new Uint8Array(b) }))
      .catch(rej);
  });
}

$('runBtn').onclick = async () => {
  if (!ready) return;
  const btn = $('runBtn');
  btn.disabled = true;
  setState('拟合中…');
  try {
    const fits = await readFile($('fitsFile'));
    const tpl = await readFile($('tplFile'));
    log(`→ 拟合 ${fits.name}（${(fits.bytes.length / 1024).toFixed(0)} KB）`
      + ` 用模板 ${tpl.name}（${(tpl.bytes.length / 1048576).toFixed(1)} MB）`);
    worker.postMessage({
      type: 'fit',
      fitsName: fits.name, fitsBytes: fits.bytes,
      tplName: tpl.name, tplBytes: tpl.bytes,
      cfgJson: '{}',
    }, [fits.bytes.buffer, tpl.bytes.buffer]);
  } catch (e) {
    log('❌ ' + e.message);
    btn.disabled = false;
    setState('出错');
  }
};

boot();
