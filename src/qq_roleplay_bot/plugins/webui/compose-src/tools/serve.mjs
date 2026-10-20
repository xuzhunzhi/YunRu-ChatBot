// Minimal static file server for the Compose/wasm production bundle.
// Exists instead of `python -m http.server` because `application/wasm` must be served
// correctly for WebAssembly.instantiateStreaming; a wrong MIME silently breaks the app.
//
// `--api <origin>` additionally reverse-proxies `/api/*` to the panel. That is a **test harness
// only**: the real deployment serves the bundle from the panel itself, so `/api/*` is same-origin
// and no CORS/proxy is involved (see NOTES.md §部署). It is here so the acceptance run can prove
// the Bearer header + rendering path without touching the live panel.
import { createServer, request } from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';

const args = process.argv.slice(2);
const flag = (name) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : undefined;
};
const ROOT = path.resolve(args[0] ?? '.');
const PORT = Number(args[1] ?? 8741);
const API = flag('api');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.wasm': 'application/wasm',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.map': 'application/json; charset=utf-8',
  '.ttf': 'font/ttf',
  '.woff2': 'font/woff2',
};

function proxy(req, res) {
  const target = new URL(req.url, API);
  const upstream = request(
    { hostname: target.hostname, port: target.port, path: target.pathname + target.search, method: req.method, headers: req.headers },
    (upstreamRes) => {
      res.writeHead(upstreamRes.statusCode ?? 502, upstreamRes.headers);
      upstreamRes.pipe(res);
    },
  );
  upstream.on('error', (e) => res.writeHead(502).end('proxy error: ' + e.message));
  req.pipe(upstream);
}

createServer(async (req, res) => {
  if (API && req.url.startsWith('/api/')) return proxy(req, res);
  const urlPath = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
  const rel = urlPath === '/' ? 'index.html' : urlPath.replace(/^\/+/, '');
  const file = path.join(ROOT, rel);
  if (!file.startsWith(ROOT)) {
    res.writeHead(403).end('forbidden');
    return;
  }
  try {
    const buf = await readFile(file);
    res.writeHead(200, {
      'Content-Type': MIME[path.extname(file).toLowerCase()] ?? 'application/octet-stream',
      'Content-Length': buf.length,
      'Cache-Control': 'no-store',
    });
    res.end(buf);
  } catch {
    res.writeHead(404, { 'Content-Type': 'text/plain' }).end('not found: ' + rel);
  }
}).listen(PORT, '127.0.0.1', () => {
  console.log(`serving ${ROOT} on http://127.0.0.1:${PORT}` + (API ? ` (proxying /api -> ${API})` : ''));
});
