// SkyAgent Desktop — окно Electron поверх локального Python-сервера (web.py).
const { app, BrowserWindow, shell, nativeTheme } = require('electron');
const { spawn, execFile } = require('child_process');
const path = require('path');
const fs = require('fs');
const net = require('net');
const http = require('http');

const ROOT = path.resolve(__dirname, '..');
let server = null, win = null, quitting = false;

app.setAppUserModelId('SkyAgent');
if (!app.requestSingleInstanceLock()) app.quit();
app.on('second-instance', () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });

function freePort(start = 7870) {
  return new Promise(resolve => {
    const s = net.createServer();
    s.once('error', () => resolve(freePort(start + 1)));
    s.once('listening', () => s.close(() => resolve(start)));
    s.listen(start, '127.0.0.1');
  });
}

function waitReady(port, ms = 20000) {
  const until = Date.now() + ms;
  return new Promise((resolve, reject) => {
    const tick = () => {
      http.get({ host: '127.0.0.1', port, path: '/api/health', timeout: 1500 }, res => { res.resume(); resolve(); })
        .on('error', () => Date.now() > until ? reject(new Error('сервер не запустился')) : setTimeout(tick, 200));
    };
    tick();
  });
}

function killServer() {
  if (!server || server.exitCode !== null) return;
  if (process.platform === 'win32') execFile('taskkill', ['/pid', String(server.pid), '/T', '/F'], () => {});
  else server.kill();
}

async function start() {
  nativeTheme.themeSource = 'light';
  const port = await freePort();
  // Сначала Python из окружения проекта (.venv), иначе системный
  const venvPython = process.platform === 'win32'
    ? path.join(ROOT, '.venv', 'Scripts', 'python.exe')
    : path.join(ROOT, '.venv', 'bin', 'python');
  const python = process.env.SKYAGENT_PYTHON
    || (fs.existsSync(venvPython) ? venvPython : (process.platform === 'win32' ? 'python' : 'python3'));
  server = spawn(python, ['web.py', '--no-browser', '--port', String(port)], {
    cwd: ROOT, windowsHide: true, env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  });
  server.stdout.on('data', d => process.stdout.write(d));
  server.stderr.on('data', d => process.stderr.write(d));
  // сервер завершился (например, нажали «Открыть CLI») — закрываем приложение
  server.on('exit', () => { if (!quitting) { quitting = true; app.quit(); } });

  win = new BrowserWindow({
    width: 1360, height: 880, minWidth: 760, minHeight: 560,
    show: false, backgroundColor: '#5aa0f0', title: 'SkyAgent',
    titleBarStyle: 'hidden',
    titleBarOverlay: { color: '#00000000', symbolColor: '#0b2747', height: 44 },
    webPreferences: { contextIsolation: true, sandbox: true, spellcheck: false },
  });
  win.setMenuBarVisibility(false);
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: 'deny' }; });
  win.webContents.on('will-navigate', (e, url) => {
    if (!url.startsWith(`http://127.0.0.1:${port}`)) { e.preventDefault(); shell.openExternal(url); }
  });
  win.once('ready-to-show', () => win.show());
  win.on('closed', () => { win = null; });

  try { await waitReady(port); }
  catch (e) {
    await win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(
      `<body style="font:16px Segoe UI;background:#cfe6ff;color:#0b2747;padding:60px">
       <h2>Не удалось запустить SkyAgent</h2><p>Проверь, что установлен Python и зависимости:<br>
       <code>pip install -r requirements.txt</code></p></body>`));
    win.show();
    return;
  }
  win.loadURL(`http://127.0.0.1:${port}/?desktop=1`);
}

app.whenReady().then(start);
app.on('window-all-closed', () => { quitting = true; killServer(); app.quit(); });
app.on('before-quit', () => { quitting = true; killServer(); });
