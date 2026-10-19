/* ------------------------------------------------------------------
   SEW web — Pyodide 工作线程（所有 Python 计算都在这里跑）

   依赖全部自托管在 ./vendor/ 下，不访问外网 CDN。
   消息协议（主线程 → worker）：
     {type:'init'}
     {type:'request', id, method, path, query, body}   → {type:'response', id, json}
     {type:'write',   id, path, bytes}                 → {type:'response', id, json}
     {type:'delete',  id, path}                        → {type:'response', id, json}
   worker → 主线程：log / progress / ready / response / error
   ------------------------------------------------------------------ */
const CDN = new URL('./vendor/pyodide/', self.location.href).href;
const WHEELS = new URL('./vendor/wheels/', self.location.href).href;
const BASE = self.location.href.replace(/[^/]*$/, '');

importScripts(CDN + 'pyodide.js');

let pyodide = null;

const send = (type, payload) => postMessage(Object.assign({ type }, payload));
const log = (msg) => send('log', { msg: String(msg) });
const respond = (id, json, error) => send('response', { id, json, error });

async function fetchToFS(url, dst) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`下载失败 ${url}: HTTP ${r.status}`);
  const buf = new Uint8Array(await r.arrayBuffer());
  pyodide.FS.mkdirTree(dst.replace(/\/[^/]+$/, ''));
  pyodide.FS.writeFile(dst, buf);
  return buf.length;
}

function writeFS(dst, bytes) {
  pyodide.FS.mkdirTree(dst.replace(/\/[^/]+$/, ''));
  pyodide.FS.writeFile(dst, new Uint8Array(bytes));
}

async function boot() {
  log('加载 Pyodide（WASM）…');
  pyodide = await loadPyodide({ indexURL: CDN });

  log('装载 numpy / scipy / matplotlib / astropy …');
  await pyodide.loadPackage(['numpy', 'scipy', 'matplotlib', 'astropy',
                             'micropip']);

  log('装载 ppxf / ppxf-sew / plotbin / capfit / powerbin …');
  const micropip = pyodide.pyimport('micropip');
  await micropip.install([
    WHEELS + 'astropy_iers_data-0.2026.10.19.0.52.1-py3-none-any.whl',
    WHEELS + 'ppxf-9.5.0-py3-none-any.whl',
    WHEELS + 'ppxf_sew-0.8.2-py3-none-any.whl',
    WHEELS + 'plotbin-3.2.0-py3-none-any.whl',
    WHEELS + 'capfit-2.7.1-py3-none-any.whl',
    WHEELS + 'powerbin-1.1.12-py3-none-any.whl',
  ]);

  log('载入算法模块…');
  pyodide.FS.mkdirTree('/sew/backend');
  for (const f of ['__init__.py', 'fits_io.py', 'fitting.py', 'templates.py',
                   'binning.py', 'wasm_app.py']) {
    await fetchToFS(`${BASE}py/backend/${f}`, `/sew/backend/${f}`);
  }
  pyodide.FS.mkdirTree('/sew/data/templates');
  pyodide.FS.mkdirTree('/sew/data/uploads');
  pyodide.FS.mkdirTree('/sew/data/samples');

  // 静态数据：谱线表 + 示例谱（没有就跳过）
  try {
    await fetchToFS(`${BASE}data/line_use.dat`, '/sew/data/line_use.dat');
  } catch (e) { log('（缺少 data/line_use.dat，谱线标注不可用）'); }
  try {
    await fetchToFS(`${BASE}data/samples/spectrum.fit`,
                    '/sew/data/samples/spectrum.fit');
  } catch (e) { /* 可选 */ }

  const info = pyodide.runPython(`
import sys, importlib.metadata as md
sys.path.insert(0, '/sew')
from backend import wasm_app
sew_request = wasm_app.sew_request
' | '.join([
    'python ' + sys.version.split()[0],
    'numpy ' + md.version('numpy'),
    'scipy ' + md.version('scipy'),
    'astropy ' + md.version('astropy'),
    'ppxf ' + md.version('ppxf'),
    'ppxf-sew ' + md.version('ppxf-sew'),
    'powerbin ' + md.version('powerbin'),
])
`);
  log('内核就绪：' + info);
  send('ready');
}

/* 进度回调：Python 调 progress(frac, msg, key, argsJson) */
function progressFn(frac, msg, key, argsJson) {
  let args = {};
  try { args = JSON.parse(argsJson || '{}'); } catch (e) { /* ignore */ }
  send('progress', { frac, msg: String(msg), key: String(key || ''), args });
}

function handleRequest(m) {
  const req = pyodide.globals.get('sew_request');
  const out = req(m.method || 'GET', m.path,
                  JSON.stringify(m.query || {}),
                  JSON.stringify(m.body || {}),
                  progressFn);
  respond(m.id, out);
}

self.onmessage = async (ev) => {
  const m = ev.data || {};
  try {
    if (m.type === 'init') { await boot(); return; }
    if (m.type === 'request') { handleRequest(m); return; }
    if (m.type === 'write') {
      writeFS(m.path, m.bytes);
      respond(m.id, '{"ok":true}');
      return;
    }
    if (m.type === 'delete') {
      try { pyodide.FS.unlink(m.path); } catch (e) { /* ignore */ }
      respond(m.id, '{"ok":true}');
      return;
    }
  } catch (e) {
    if (m.id != null) respond(m.id, null, String((e && e.message) || e));
    else send('error', { msg: String((e && e.stack) || e) });
  }
};
