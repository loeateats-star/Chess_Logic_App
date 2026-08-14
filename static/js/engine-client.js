// Client-side Stockfish engine wrapper (runs in-browser via WASM/JS Worker,
// replacing the old server-side subprocess in engine_service.py).
//
// Public API mirrors the JSON shape the old `/api/engine/evaluate` endpoint
// used to return, so callers don't need to change how they consume results:
//   { fen, turn: 'w'|'b', depth, lines: [{ cp, mate, pv_uci, pv_san, best_move_uci, best_move_san }] }
var EngineClient = (function () {
  var worker = null;
  var readyPromise = null;
  var pending = null; // { resolve, reject, fen, depth, lines: [] }

  function ensureWorker() {
    if (worker) return;
    worker = new Worker('/static/js/stockfish-worker-loader.js');
    worker.onmessage = onMessage;
    worker.onerror = function (err) {
      var msg = (err && (err.message || err.toString())) || 'Unknown engine worker error.';
      if (pending) {
        pending.reject(new Error(msg));
        pending = null;
      }
    };
  }

  function send(cmd) {
    worker.postMessage(cmd);
  }

  function init() {
    if (readyPromise) return readyPromise;
    ensureWorker();
    readyPromise = new Promise(function (resolve, reject) {
      var uciSeen = false;
      var timeout = setTimeout(function () {
        worker.removeEventListener('message', handler);
        reject(new Error('Engine did not respond within 15s.'));
      }, 15000);

      function handler(e) {
        var line = typeof e.data === 'string' ? e.data : '';
        if (line === 'uciok') {
          uciSeen = true;
          send('isready');
        } else if (line === 'readyok' && uciSeen) {
          clearTimeout(timeout);
          worker.removeEventListener('message', handler);
          resolve();
        }
      }
      worker.addEventListener('message', handler);
      send('uci');
    });
    return readyPromise;
  }

  function onMessage(e) {
    if (!pending) return;
    var line = typeof e.data === 'string' ? e.data : '';

    if (line.indexOf('info') === 0 && line.indexOf(' pv ') !== -1) {
      var mpvMatch = line.match(/\smultipv\s(\d+)/);
      var mpvIdx = mpvMatch ? parseInt(mpvMatch[1], 10) : 1;
      var cpMatch = line.match(/\sscore\scp\s(-?\d+)/);
      var mateMatch = line.match(/\sscore\smate\s(-?\d+)/);
      var pvMatch = line.match(/\spv\s(.+)$/);
      if (!pvMatch) return;

      pending.lines[mpvIdx - 1] = {
        cp: cpMatch ? parseInt(cpMatch[1], 10) : null,
        mate: mateMatch ? parseInt(mateMatch[1], 10) : null,
        pv_uci: pvMatch[1].trim().split(/\s+/)
      };
    } else if (line.indexOf('bestmove') === 0) {
      finishPending();
    }
  }

  function finishPending() {
    if (!pending) return;
    var p = pending;
    pending = null;

    var startBoard = new Chess(p.fen);
    var lines = p.lines.filter(Boolean).map(function (line) {
      var walker = new Chess(p.fen);
      var pvSan = [];
      for (var i = 0; i < line.pv_uci.length; i++) {
        var uci = line.pv_uci[i];
        var mv = walker.move({ from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci.slice(4) || 'q' });
        if (!mv) break; // malformed/truncated PV tail — stop converting rather than desync
        pvSan.push(mv.san);
      }
      return {
        cp: line.cp,
        mate: line.mate,
        pv_uci: line.pv_uci,
        pv_san: pvSan,
        best_move_uci: line.pv_uci[0] || null,
        best_move_san: pvSan[0] || null
      };
    });

    p.resolve({
      fen: p.fen,
      turn: startBoard.turn(),
      depth: p.depth,
      lines: lines
    });
  }

  // Requests are serialized through this queue — sending a new
  // "position"/"go" while the engine is still mid-search on a previous one
  // (e.g. a user dragging several pieces in quick succession, each
  // triggering a live-eval call) leaves the single UCI process wedged with
  // no matching "bestmove" ever arriving for either request.
  var _queue = Promise.resolve();

  function evaluateFen(fen, opts) {
    opts = opts || {};
    var depth = opts.depth || 18;
    var multipv = opts.multipv || 1;

    var result = _queue.then(function () {
      return init();
    }).then(function () {
      return new Promise(function (resolve, reject) {
        pending = { resolve: resolve, reject: reject, fen: fen, depth: depth, lines: [] };
        send('setoption name MultiPV value ' + multipv);
        send('position fen ' + fen);
        send('go depth ' + depth);
      });
    });

    // Keep the queue moving even if this request fails.
    _queue = result.then(function () {}, function () {});
    return result;
  }

  return { init: init, evaluateFen: evaluateFen };
})();
