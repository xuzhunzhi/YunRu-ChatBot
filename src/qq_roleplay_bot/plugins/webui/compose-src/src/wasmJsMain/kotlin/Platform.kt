// Browser glue for the v2 panel: measurements, fetch, localStorage.
//
// Everything here is a `@JsFun` *function declaration* on purpose. The plain `js("…")` escape
// is unusable for anything but a single inline expression on Kotlin/Wasm (NOTES.md §"两个坑"),
// so the fetch/clock helpers are declared as ordinary JS functions and called as `external fun`
// (same shape Miuix's own web example uses in `webfont/WebFetch.kt`).

import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume
import kotlin.io.encoding.Base64
import kotlin.io.encoding.ExperimentalEncodingApi
import kotlin.js.ExperimentalWasmJsInterop

/** `performance.now()`, i.e. milliseconds since navigation start (same clock as `index.html`). */
@OptIn(ExperimentalWasmJsInterop::class)
@JsFun("function dshNowMs() { return performance.now(); }")
external fun nowMs(): Double

/**
 * Stamped once, from inside the frame that first lays out real text. `withFrameNanos` resumes at
 * the *start* of a frame, so a value written from the second resume is "this frame has been
 * drawn" — the same anchor the previous round measured with.
 */
@OptIn(ExperimentalWasmJsInterop::class)
@JsFun("function dshMarkFirstText() { window.__dshFirstText = performance.now(); }")
external fun markFirstText()

/** How long the CJK font took to fetch + register (0 means "preload disabled or failed"). */
@OptIn(ExperimentalWasmJsInterop::class)
@JsFun("function dshMarkFontReady(startedAt, ok) { window.__dshFontReady = { startedAt: startedAt, doneAt: performance.now(), ms: performance.now() - startedAt, ok: ok }; }")
external fun markFontReady(startedAt: Double, ok: Boolean)

/** Reads `?name=` off the page URL. Used for `?nofont=1` (A/B evidence) and `?api=…` (dev proxy). */
@OptIn(ExperimentalWasmJsInterop::class)
@JsFun(
    """
    function dshQueryParam(name) {
        try {
            return new URLSearchParams(window.location.search).get(name) || '';
        } catch (e) {
            return '';
        }
    }
    """,
)
external fun queryParam(name: String): String

@OptIn(ExperimentalWasmJsInterop::class)
@JsFun("function dshLocalGet(key) { try { return window.localStorage.getItem(key) || ''; } catch (e) { return ''; } }")
external fun localGet(key: String): String

@OptIn(ExperimentalWasmJsInterop::class)
@JsFun("function dshLocalSet(key, value) { try { window.localStorage.setItem(key, value); } catch (e) {} }")
external fun localSet(key: String, value: String)

@OptIn(ExperimentalWasmJsInterop::class)
@JsFun("function dshWarning(message) { if (typeof console !== 'undefined' && console.warn) console.warn(message); }")
external fun consoleWarn(message: String)

/**
 * `GET url` with an optional `Authorization: Bearer` header, delivered as text.
 *
 * Two details that are load-bearing:
 *  * the rejection carries the status **and** the first bytes of the body, because the panel's
 *    failures are `{"ok":false,"reason":…,"detail":…}` — swallowing that would turn "token 不对"
 *    into "操作失败", which is exactly the complaint in `webui_panel.py`'s write routes;
 *  * `credentials: 'same-origin'` keeps the remote-mode session cookie working, and the Bearer
 *    header is what local mode wants (`webui_access.WebAccess.check_bearer`).
 */
@OptIn(ExperimentalWasmJsInterop::class)
@JsFun(
    """
    function dshFetchText(url, token, onOk, onErr) {
        var headers = {};
        if (token) { headers['Authorization'] = 'Bearer ' + token; }
        fetch(url, { headers: headers, credentials: 'same-origin' })
            .then(function (r) {
                return r.text().then(function (body) {
                    if (r.ok) { onOk(body); }
                    else { onErr('HTTP ' + r.status + (body ? (' ' + body.slice(0, 200)) : '')); }
                });
            })
            .catch(function (e) { onErr(String(e)); });
    }
    """,
)
private external fun fetchTextJs(url: String, token: String, onOk: (String) -> Unit, onErr: (String) -> Unit)

/**
 * `GET url` as raw bytes, base64-hopped through JS.
 *
 * Why base64 instead of a direct `ArrayBuffer`: Kotlin/Wasm has no zero-copy `ArrayBuffer` →
 * `ByteArray` bridge in stdlib without extra interop declarations, and Skia's `Font` wants a real
 * `ByteArray`. The Miuix example (`webfont/WebFetch.kt`) does exactly this hop for the same reason.
 */
@OptIn(ExperimentalWasmJsInterop::class)
@JsFun(
    """
    function dshFetchBytesBase64(url, onOk, onErr) {
        fetch(url, { credentials: 'same-origin' })
            .then(function (r) {
                if (!r.ok) { onErr('HTTP ' + r.status); return null; }
                return r.arrayBuffer();
            })
            .then(function (buf) {
                if (buf === null) { return; }
                var bytes = new Uint8Array(buf);
                var chunks = [];
                var step = 0x8000;
                for (var i = 0; i < bytes.length; i += step) {
                    chunks.push(String.fromCharCode.apply(null, bytes.subarray(i, i + step)));
                }
                onOk(btoa(chunks.join('')));
            })
            .catch(function (e) { onErr(String(e)); });
    }
    """,
)
private external fun fetchBytesBase64Js(url: String, onOk: (String) -> Unit, onErr: (String) -> Unit)

/** Result of one HTTP attempt: body on success, human-readable reason on failure. */
sealed interface HttpResult {
    data class Ok(val body: String) : HttpResult
    data class Failed(val reason: String) : HttpResult
}

/**
 * Awaits the base64 font body that `index.html` kicked off before this module even downloaded.
 *
 * The HTML prefetch and this call are the same request (the promise is stored on `window`), which
 * is what lets the 2.1 MB font transfer overlap the wasm download. `onErr` is not an error path the
 * user sees: the caller simply fetches the font itself in that case, so a page served without the
 * prefetch script still works.
 */
@OptIn(ExperimentalWasmJsInterop::class)
@JsFun(
    """
    function dshAwaitPrefetchedFont(onOk, onErr) {
        var p = window.__dshFontB64;
        if (!p || typeof p.then !== 'function') { onErr('no preload promise'); return; }
        p.then(function (b64) { onOk(b64); }, function (e) { onErr(String(e)); });
    }
    """,
)
private external fun awaitPrefetchedFont(onOk: (String) -> Unit, onErr: (String) -> Unit)

suspend fun httpGetText(url: String, token: String): HttpResult = suspendCancellableCoroutine { cont ->
    fetchTextJs(
        url,
        token,
        { body -> if (cont.isActive) cont.resume(HttpResult.Ok(body)) },
        { err -> if (cont.isActive) cont.resume(HttpResult.Failed(err)) },
    )
}

@OptIn(ExperimentalEncodingApi::class)
private suspend fun fetchBytesDirect(url: String): ByteArray? = suspendCancellableCoroutine { cont ->
    fetchBytesBase64Js(
        url,
        { b64 ->
            if (cont.isActive) cont.resume(runCatching { Base64.decode(b64) }.getOrNull())
        },
        { err ->
            if (cont.isActive) {
                consoleWarn("[font] fetch failed: $url -> $err")
                cont.resume(null)
            }
        },
    )
}

/**
 * Font bytes for [url]: reuses `window.__dshFontB64` (started by `index.html`) when present, and
 * otherwise fetches — so the bundle also works if someone opens `index.html` from a page that lost
 * the prefetch snippet.
 */
@OptIn(ExperimentalEncodingApi::class)
suspend fun httpGetBytes(url: String): ByteArray? {
    val prefetched = suspendCancellableCoroutine { cont ->
        awaitPrefetchedFont(
            { b64 -> if (cont.isActive) cont.resume(b64) },
            { if (cont.isActive) cont.resume(null) },
        )
    }
    if (prefetched != null) {
        runCatching { Base64.decode(prefetched) }.getOrNull()?.let { return it }
    }
    return fetchBytesDirect(url)
}
