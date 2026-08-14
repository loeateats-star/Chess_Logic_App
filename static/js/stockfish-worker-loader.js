// Same-origin shim for loading Stockfish from a CDN inside a Web Worker.
//
// `new Worker(url)` requires `url` to be same-origin — browsers throw a
// SecurityError for a classic worker script hosted on a different origin,
// even with permissive CORS headers on the response. `importScripts()`
// called from *inside* an already-running worker does not have that
// restriction, so this tiny same-origin file is what the page actually
// constructs a Worker from, and it immediately imports the real engine.
//
// The WASM build (stockfish.wasm@0.10.0) ships as an Emscripten
// "modularized" factory: importScripts()-ing it only defines a global
// Stockfish() function, it does NOT wire up postMessage/onmessage on its
// own the way a classic single-file engine does. We call the factory
// ourselves and bridge its instance-level API (engine.postMessage /
// engine.addMessageListener) back onto this worker's own postMessage/
// onmessage, so from the page's point of view this worker behaves like a
// classic UCI worker either way, regardless of which build loaded.
self.addEventListener('error', function (e) {
  postMessage('WORKER-INTERNAL-ERROR: ' + (e.message || e.type || 'unknown') + ' @ ' + e.filename + ':' + e.lineno);
});
self.addEventListener('unhandledrejection', function (e) {
  var r = e.reason;
  postMessage('WORKER-INTERNAL-UNHANDLED-REJECTION: ' + (r && r.stack ? r.stack : r));
});

var wasmSupported = typeof WebAssembly === 'object'
  && WebAssembly.validate(Uint8Array.of(0x0, 0x61, 0x73, 0x6d, 0x01, 0x00, 0x00, 0x00));

if (wasmSupported) {
  try {
    // The stockfish.wasm package's multi-threaded runtime spawns its own
    // nested pthread Worker (stockfish.worker.js) internally via a plain
    // `new Worker(url)` call — the exact same same-origin restriction that
    // required this shim file in the first place applies to THAT worker
    // too, and there is no CDN-side fix for a raw Worker constructor call
    // buried inside the library. So the three package files (~375KB total —
    // stockfish.js, stockfish.wasm, stockfish.worker.js) are self-hosted
    // under static/js/stockfish-wasm/ instead of fetched from jsdelivr at
    // runtime. This is also the deployment pattern the package's own docs
    // recommend. Nothing native/platform-specific is checked in — this is
    // the same small WASM/JS bundle any browser downloads from the CDN
    // anyway, just served from our own origin.
    importScripts('/static/js/stockfish-wasm/stockfish.js');

    // Buffer anything the page sends before the WASM module finishes booting.
    var _outbox = [];
    self.onmessage = function (e) { _outbox.push(e.data); };

    // mainScriptUrlOrBlob tells the runtime what URL to hand its own
    // spawned pthread workers so they can re-import this same script.
    // Emscripten normally infers this from document.currentScript, which
    // doesn't exist inside a worker — left unset, the pthread bootstrap
    // message ships a `urlOrBlob` of undefined and stockfish.worker.js
    // crashes trying to URL.createObjectURL(undefined).
    var _instance = Stockfish({
      locateFile: function (path) {
        return '/static/js/stockfish-wasm/' + path;
      },
      mainScriptUrlOrBlob: '/static/js/stockfish-wasm/stockfish.js'
    });
    var _readyPromise = (_instance && typeof _instance.then === 'function')
      ? _instance
      : (_instance.ready || Promise.resolve(_instance));

    _readyPromise.then(function (engine) {
      engine.addMessageListener(function (line) { postMessage(line); });
      self.onmessage = function (e) { engine.postMessage(e.data); };
      _outbox.forEach(function (cmd) { engine.postMessage(cmd); });
      _outbox = [];
    }).catch(function (err) {
      postMessage('error: WASM engine failed to initialize (async) — ' + (err && (err.stack || err.message) ? (err.stack || err.message) : err));
    });
  } catch (err) {
    postMessage('error: WASM engine failed to initialize (sync) — ' + (err && (err.stack || err.message) ? (err.stack || err.message) : err));
  }
} else {
  // Classic asm.js build — self-contained, wires up postMessage/onmessage itself.
  importScripts('https://cdnjs.cloudflare.com/ajax/libs/stockfish.js/10.0.2/stockfish.js');
}
