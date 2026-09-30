/*
 * Offline sync queue for progress-saving requests (puzzle attempts, rush
 * scores, video progress, diagnostic results). A dropped connection on
 * mobile shouldn't silently discard a student's progress: if a POST fails
 * because the network is down, it's queued in localStorage and retried
 * automatically once connectivity returns.
 */
(function (global) {
  var STORAGE_KEY   = 'synapchess_sync_queue_v1';
  var MIN_BACKOFF_MS = 1500;
  var MAX_BACKOFF_MS = 60000;

  var backoffMs  = MIN_BACKOFF_MS;
  var flushTimer = null;
  var flushing   = false;

  function readQueue() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
    } catch (e) {
      return [];
    }
  }

  function writeQueue(queue) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(queue));
    } catch (e) { /* storage unavailable (private mode, quota) -- drop silently */ }
  }

  function enqueue(url, body) {
    var queue = readQueue();
    queue.push({ url: url, body: body, ts: Date.now() });
    writeQueue(queue);
  }

  function postJson(url, body) {
    return fetch(url, {
      method:      'POST',
      headers:     { 'Content-Type': 'application/json' },
      body:        JSON.stringify(body),
      credentials: 'same-origin'
    });
  }

  // Sends `body` to `url` as JSON. If the network request fails outright
  // (offline, DNS, timeout), the request is queued for later and the
  // promise still resolves -- with `{queued: true}` -- instead of
  // rejecting, so callers can update the UI optimistically either way.
  function queuedPost(url, body) {
    return postJson(url, body)
      .then(function (r) {
        if (!r.ok && r.status >= 500) throw new Error('server error ' + r.status);
        return r.json().catch(function () { return {}; }).then(function (json) {
          json.queued = false;
          json.ok     = r.ok;
          return json;
        });
      })
      .catch(function () {
        enqueue(url, body);
        scheduleFlush(0, true);
        return { queued: true, ok: false };
      });
  }

  function scheduleFlush(delay, force) {
    if (flushTimer && !force) return;
    if (flushTimer) clearTimeout(flushTimer);
    flushTimer = setTimeout(function () {
      flushTimer = null;
      flushQueue();
    }, delay != null ? delay : backoffMs);
  }

  function flushQueue() {
    if (flushing) return;
    var queue = readQueue();
    if (!queue.length) return;
    if (typeof navigator !== 'undefined' && navigator.onLine === false) {
      scheduleFlush();
      return;
    }

    flushing = true;
    var item = queue[0];

    postJson(item.url, item.body)
      .then(function (r) {
        if (r.ok || (r.status >= 400 && r.status < 500)) {
          // Success, or a client error that will never succeed on retry
          // (e.g. session expired) -- either way, stop holding onto it.
          queue.shift();
          writeQueue(queue);
          backoffMs = MIN_BACKOFF_MS;
          if (r.ok) {
            global.dispatchEvent(new CustomEvent('synapchess-sync', { detail: { url: item.url } }));
          }
        } else {
          backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
        }
      })
      .catch(function () {
        backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
      })
      .then(function () {
        flushing = false;
        if (readQueue().length) scheduleFlush();
      });
  }

  global.addEventListener('online', function () {
    backoffMs = MIN_BACKOFF_MS;
    scheduleFlush(0, true);
  });
  global.addEventListener('load', function () { scheduleFlush(0, true); });
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') scheduleFlush(0, true);
  });

  global.SynapchessSync = {
    queuedPost:    queuedPost,
    pendingCount:  function () { return readQueue().length; }
  };
})(window);
