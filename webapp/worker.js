/* ------------------------------------------------------------------
   SEW web — Pyodide 工作线程（所有 Python 计算都在这里跑）
   依赖全部自托管在 ./vendor/ 下，不访问外网 CDN。
   ------------------------------------------------------------------ */
const CDN = new URL('./vendor/pyodide/', self.location.href).href;
const WHEELS = new URL('./vendor/wheels/', self.location.href).href;

importScripts(CDN + 'pyodide.js');

let pyodide = null;

const send = (type, payload) => postMessage(Object.assign({ type }, payload));
const log = (msg) => send('log', { msg: String(msg) });

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

async function boot(base) {
  log('加载 Pyodide（WASM，一次性约 10 MB）…');
  pyodide = await loadPyodide({ indexURL: CDN });

  log('安装 numpy / scipy / matplotlib / astropy（首次约 60–90 s，之后走浏览器缓存）…');
  await pyodide.loadPackage(['numpy', 'scipy', 'matplotlib', 'astropy',
                             'micropip']);

  log('安装 ppxf / ppxf-sew / plotbin / capfit / powerbin …');
  const micropip = pyodide.pyimport('micropip');
  await micropip.install([
    WHEELS + 'astropy_iers_data-0.2026.10.19.0.52.1-py3-none-any.whl',
    WHEELS + 'ppxf-9.5.0-py3-none-any.whl',
    WHEELS + 'ppxf_sew-0.8.2-py3-none-any.whl',
    WHEELS + 'plotbin-3.2.0-py3-none-any.whl',
    WHEELS + 'capfit-2.7.1-py3-none-any.whl',
    WHEELS + 'powerbin-1.1.12-py3-none-any.whl',
  ]);

  log('载入后端算法模块（与服务器版同一套代码）…');
  pyodide.FS.mkdirTree('/sew/backend');
  for (const f of ['__init__.py', 'fits_io.py', 'fitting.py', 'templates.py',
                   'binning.py', 'api.py']) {
    const n = await fetchToFS(`${base}py/backend/${f}`, `/sew/backend/${f}`);
    log(`  backend/${f} (${(n / 1024).toFixed(0)} KB)`);
  }
  pyodide.FS.mkdirTree('/sew/data/templates');
  pyodide.FS.mkdirTree('/sew/data/uploads');

  const info = pyodide.runPython(`
import sys, importlib.metadata as md
sys.path.insert(0, '/sew')
from backend import api
sew_fit = api.sew_fit          # 暴露到 __main__ 供 JS 调用
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
  log('版本：' + info);
  log('就绪。');
  send('ready');
}

self.onmessage = async (ev) => {
  const m = ev.data || {};
  try {
    if (m.type === 'init') {
      await boot(m.base || './');
      return;
    }
    if (m.type === 'fit') {
      writeFS('/sew/data/uploads/in.fits', m.fitsBytes);
      writeFS(`/sew/data/templates/${m.tplName}`, m.tplBytes);
      const fit = pyodide.globals.get('sew_fit');
      const out = fit('/sew/data/uploads/in.fits', m.tplName, m.cfgJson || '{}');
      send('result', { result: JSON.parse(out) });
      return;
    }
  } catch (e) {
    send('error', { msg: String((e && e.stack) || e) });
  }
};
