// Headless-Edge CDP driver: loads the page, waits for Compose to report its first text-bearing
// frame, screenshots *that* frame, then a short series, then an optional click.
// No third-party deps: Node 24 has global fetch + WebSocket.
//
// CLI: node tools/cdp-shot.mjs <url> <out.png> [clickX clickY <out2.png>] [--width N] [--height N] [--dark]
//      [--clickDelay N]  (ms to wait between the click and the second screenshot; default 400)
//      [--settle N]      (extra ms before the final screenshot; default 0)
//      [--noPark]        (do not move the pointer away before the second screenshot)
// Writes <out>-first.png (the frame the app stamped) and <out>-t0/400/800/1500.png.
//
// `--dark` emulates `prefers-color-scheme: dark`, which is the only switch the app reads
// (`ThemeController(ColorSchemeMode.System)`): without it the headless browser always reports light,
// so the dark half of the palette was never photographed.
import { spawn } from 'node:child_process';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const argv = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 ? Number(argv[i + 1]) : fallback;
};
const positional = argv.filter((a, i) => !a.startsWith('--') && !argv[i - 1]?.startsWith('--'));
const [url, outPng1, clickX, clickY, outPng2] = positional;
const WIDTH = flag('width', 900);
const HEIGHT = flag('height', 600);
const PORT = flag('port', 9333);
const PROFILE = path.join(process.env.TEMP, `edge-cdp-compose-${Date.now()}`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const edge = spawn(
  EDGE,
  [
    '--headless=new',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${PROFILE}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-extensions',
    '--enable-unsafe-swiftshader',
    '--hide-scrollbars',
    'about:blank',
  ],
  { stdio: 'ignore' },
);

async function waitForDevtools() {
  for (let i = 0; i < 200; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (r.ok) return await r.json();
    } catch {}
    await sleep(150);
  }
  throw new Error('devtools endpoint never came up');
}

const consoleLines = [];

async function main() {
  const version = await waitForDevtools();
  console.log('browser:', version.Browser);

  const target = await (
    await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: 'PUT' })
  ).json();

  // Optionally drive the token field through the page's own input instead of a click: the harness
  // only needs "the page really fetched /api/*", and this keeps the evidence honest.
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener('open', res, { once: true });
    ws.addEventListener('error', rej, { once: true });
  });

  let seq = 0;
  const pending = new Map();
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { res, rej } = pending.get(m.id);
      pending.delete(m.id);
      m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
      return;
    }
    if (m.method === 'Runtime.consoleAPICalled') {
      consoleLines.push(
        `[console.${m.params.type}] ` +
          m.params.args.map((a) => a.value ?? a.description ?? a.type).join(' '),
      );
    }
    if (m.method === 'Runtime.exceptionThrown') {
      consoleLines.push(
        '[exception] ' +
          (m.params.exceptionDetails.exception?.description ?? m.params.exceptionDetails.text),
      );
    }
  });

  const cmd = (method, params = {}) => {
    const id = ++seq;
    return new Promise((res, rej) => {
      pending.set(id, { res, rej });
      ws.send(JSON.stringify({ id, method, params }));
    });
  };
  const evalJs = async (expression) => {
    const r = await cmd('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
    return r.result.value;
  };

  await cmd('Page.enable');
  await cmd('Runtime.enable');
  await cmd('Log.enable').catch(() => {});
  // deviceScaleFactor 1 keeps CSS px == screenshot px, so click coords are literal.
  await cmd('Emulation.setDeviceMetricsOverride', {
    width: WIDTH,
    height: HEIGHT,
    deviceScaleFactor: 1,
    mobile: false,
  });

  if (argv.includes('--dark')) {
    await cmd('Emulation.setEmulatedMedia', {
      features: [{ name: 'prefers-color-scheme', value: 'dark' }],
    });
  }

  await cmd('Page.navigate', { url });

  // Kotlin sets window.__dshFirstText after the frame that lays out real text.
  let firstText = null;
  let waited = 0;
  while (waited < 90_000) {
    firstText = await evalJs('window.__dshFirstText ?? null');
    if (typeof firstText === 'number') break;
    await sleep(50);
    waited += 50;
  }
  const polledAt = await evalJs('performance.now()');
  const fontReady = await evalJs('window.__dshFontReady ?? null');

  // The frame the app stamped — captured as soon as it is observable, before any settle time.
  const firstShot = await cmd('Page.captureScreenshot', { format: 'png' });
  const firstFile = outPng1.replace(/\.png$/i, '-first.png');
  await writeFile(firstFile, Buffer.from(firstShot.data, 'base64'));

  const hasCanvas = await evalJs(
    'JSON.stringify({ canvas: !!document.querySelector("canvas"), ' +
      'canvasSize: document.querySelector("canvas") ? ' +
      '[document.querySelector("canvas").width, document.querySelector("canvas").height] : null })',
  );
  const perf = await evalJs(
    `JSON.stringify({
      nav: performance.getEntriesByType('navigation').map(e => ({
        domContentLoaded: Math.round(e.domContentLoadedEventEnd),
        loadEventEnd: Math.round(e.loadEventEnd),
      })),
      resources: performance.getEntriesByType('resource').map(r => ({
        name: r.name.split('/').pop(),
        durationMs: Math.round(r.duration),
        transferBytes: r.transferSize,
        decodedBytes: r.decodedBodySize,
      })),
    })`,
  );

  console.log('viewport:', `${WIDTH}x${HEIGHT}`);
  console.log('firstText(ms):', firstText);
  console.log('polledAt(ms):', polledAt.toFixed(1), 'hasCanvas:', hasCanvas);
  console.log('fontReady:', fontReady);
  console.log('perf:', perf);
  console.log('first frame saved:', firstFile);
  console.log(
    'console/exception lines:',
    consoleLines.length ? JSON.stringify(consoleLines, null, 1) : 'none',
  );

  const series = [];
  let prev = 0;
  for (const at of [0, 400, 800, 1500]) {
    if (at > prev) await sleep(at - prev);
    prev = at;
    const s = await cmd('Page.captureScreenshot', { format: 'png' });
    const stamp = await evalJs('performance.now()');
    const file = outPng1.replace(/\.png$/i, `-t${at}.png`);
    await writeFile(file, Buffer.from(s.data, 'base64'));
    series.push({ at, stamp: Math.round(stamp), file });
  }
  console.log('series:', JSON.stringify(series, null, 1));

  // Optional wheel scroll before the final shot: the rail is scrollable by design, and the only way
  // to photograph its lower half (the 插件 group) is to actually scroll it.
  const wheel = argv.indexOf('--wheel') >= 0 ? argv.slice(argv.indexOf('--wheel') + 1, argv.indexOf('--wheel') + 4).map(Number) : null;
  if (wheel) {
    const [wx, wy, dy] = wheel;
    for (let i = 0; i < 3; i++) {
      await cmd('Input.dispatchMouseEvent', {
        type: 'mouseWheel',
        x: wx,
        y: wy,
        deltaX: 0,
        deltaY: dy,
        buttons: 0,
        pointerType: 'mouse',
      });
      await sleep(150);
    }
  }

  // `--settle N` waits N extra ms before the final shot. Needed when the page loads data
  // asynchronously: the series above ends ~1.5s after the first text frame, which is sometimes
  // *before* `/api/*` has answered (caught once: the account page was photographed in its
  // "载入中…" state). 0 keeps the old behaviour.
  const settle = flag('settle', 0);
  if (settle > 0) await sleep(settle);

  const last = await cmd('Page.captureScreenshot', { format: 'png' });
  await writeFile(outPng1, Buffer.from(last.data, 'base64'));
  console.log('saved:', outPng1);

  if (clickX && clickY && outPng2) {
    const x = Number(clickX);
    const y = Number(clickY);
    for (const type of ['mousePressed', 'mouseReleased']) {
      await cmd('Input.dispatchMouseEvent', { type, x, y, button: 'left', buttons: 1, clickCount: 1 });
    }
    // `--clickDelay` shorter than the animation length (the rail is 220ms) is how the mid-transition
    // frame gets photographed; 400ms (the default) is "after it settled".
    await sleep(flag('clickDelay', 400));
    // Park the pointer in the corner and click empty space: Miuix's selectable rows paint a hover /
    // focus overlay, and a screenshot taken with the cursor still on (or focus still inside) the
    // just-clicked item shows that overlay instead of the normal selected pill — it looks like a much
    // darker pill than it really is.
    // `--noPark` skips that parking (and its 200ms settle), which is the only way the second frame
    // lands *inside* an animation — the block below alone costs ~200ms.
    if (!argv.includes('--noPark')) {
      await cmd('Input.dispatchMouseEvent', { type: 'mouseMoved', x: 1, y: 1, buttons: 0 });
      for (const type of ['mousePressed', 'mouseReleased']) {
        await cmd('Input.dispatchMouseEvent', { type, x: 1, y: 1, button: 'left', buttons: 1, clickCount: 1 });
      }
      await sleep(200);
    }
    const shot2 = await cmd('Page.captureScreenshot', { format: 'png' });
    await writeFile(outPng2, Buffer.from(shot2.data, 'base64'));
    console.log('clicked at', x, y, '-> saved:', outPng2);
  }

  ws.close();
}

main()
  .catch((e) => {
    console.error('FAILED:', e.message);
    process.exitCode = 1;
  })
  .finally(() => {
    try {
      spawn('taskkill', ['/PID', String(edge.pid), '/T', '/F'], { stdio: 'ignore' });
    } catch {}
  });
