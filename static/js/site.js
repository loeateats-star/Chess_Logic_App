/*
 * Shared behaviour for every page that extends base.html: theme toggle,
 * navigation menus, the login dialog, the "My stats" popover, scroll-in
 * motion and a lazy confetti loader.
 *
 * Pages read auth state through window.Synap:
 *   Synap.user            -> { username } or null
 *   Synap.onAuth(fn)      -> fn(user, data, source) now and on every change;
 *                            source is 'init', 'login' or 'logout'
 *   Synap.openAuth(tab)   -> open the dialog on 'login' or 'register'
 *   Synap.refreshStats()  -> re-fetch /get-analytics; resolves with the data
 *   Synap.confetti(opts)  -> loads canvas-confetti on first use, then fires
 */
(function (global) {
  'use strict';

  var doc  = document;
  var root = doc.documentElement;
  var reduceMotion = global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function $(id) { return doc.getElementById(id); }
  function all(sel, ctx) { return Array.prototype.slice.call((ctx || doc).querySelectorAll(sel)); }

  // ── Theme ────────────────────────────────────────────────────────────────
  function currentTheme() {
    var set = root.getAttribute('data-theme');
    if (set) return set;
    return global.matchMedia && global.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  var themeBtn = $('theme-toggle');
  if (themeBtn) {
    themeBtn.addEventListener('click', function () {
      var next = currentTheme() === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem('theme', next); } catch (e) {}
    });
  }

  // ── Header: dropdowns, mobile menu, scrolled state ───────────────────────
  function closeDropdowns(except) {
    all('.dd.open').forEach(function (dd) {
      if (dd === except) return;
      dd.classList.remove('open');
      dd.querySelector('.dd-trigger').setAttribute('aria-expanded', 'false');
    });
  }
  all('.dd').forEach(function (dd) {
    var trigger = dd.querySelector('.dd-trigger');
    var hoverTimer = null;
    function open() {
      closeDropdowns(dd);
      dd.classList.add('open');
      trigger.setAttribute('aria-expanded', 'true');
    }
    function close() {
      dd.classList.remove('open');
      trigger.setAttribute('aria-expanded', 'false');
    }
    // With a mouse, hovering opens the menu, so the click that usually
    // follows must not close it again straight away.
    var openedByHover = false;
    trigger.addEventListener('click', function (e) {
      e.stopPropagation();
      if (dd.classList.contains('open') && !openedByHover) close(); else open();
      openedByHover = false;
    });
    if (global.matchMedia && global.matchMedia('(hover: hover)').matches) {
      dd.addEventListener('mouseenter', function () {
        clearTimeout(hoverTimer);
        if (!dd.classList.contains('open')) openedByHover = true;
        open();
      });
      dd.addEventListener('mouseleave', function () { openedByHover = false; hoverTimer = setTimeout(close, 120); });
    }
  });
  doc.addEventListener('click', function (e) {
    if (!e.target.closest('.dd')) closeDropdowns();
  });

  var menuBtn = $('menu-toggle');
  function setMenu(open) {
    root.classList.toggle('menu-open', open);
    if (menuBtn) {
      menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
      menuBtn.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
    }
    doc.body.style.overflow = open ? 'hidden' : '';
  }
  if (menuBtn) {
    menuBtn.addEventListener('click', function () { setMenu(!root.classList.contains('menu-open')); });
    all('#mobile-panel a, #mobile-panel button').forEach(function (el) {
      el.addEventListener('click', function () { setMenu(false); });
    });
    global.addEventListener('resize', function () { if (global.innerWidth > 960) setMenu(false); });
  }

  var head = $('site-head');
  if (head) {
    var onScroll = function () { head.classList.toggle('is-scrolled', global.scrollY > 4); };
    global.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
  }

  // ── Auth ─────────────────────────────────────────────────────────────────
  var Synap = { user: null };
  var authListeners = [];
  var lastAuthData  = null;

  function emitAuth(data, source) {
    lastAuthData = data;
    all('[data-auth="out"]').forEach(function (el) { el.hidden = !!Synap.user; });
    all('[data-auth="in"]').forEach(function (el) { el.hidden = !Synap.user; });
    authListeners.forEach(function (fn) {
      try { fn(Synap.user, data, source); } catch (e) { if (global.console) console.error(e); }
    });
  }

  Synap.onAuth = function (fn) {
    authListeners.push(fn);
    if (lastAuthData !== null) fn(Synap.user, lastAuthData, 'init');
  };

  fetch('/me', { credentials: 'same-origin' })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      Synap.user = d.logged_in ? { username: d.username } : null;
      emitAuth(d, 'init');
    })
    .catch(function () { emitAuth({ logged_in: false }, 'init'); });

  var dialog   = $('auth-dialog');
  var form     = $('auth-form');
  var userIn   = $('auth-username');
  var passIn   = $('auth-password');
  var msg      = $('auth-msg');
  var submit   = $('auth-submit');
  var authTab  = 'login';
  var lastFocus = null;

  function setMsg(text, kind) {
    msg.textContent = text || '';
    msg.className = 'form-msg' + (kind ? ' is-' + kind : '');
  }

  function setTab(tab) {
    authTab = tab;
    all('[data-auth-tab]').forEach(function (b) {
      b.setAttribute('aria-selected', b.getAttribute('data-auth-tab') === tab ? 'true' : 'false');
    });
    var reg = tab === 'register';
    $('auth-title').textContent = reg ? 'Make an account' : 'Welcome back';
    $('auth-sub').textContent = reg
      ? 'Free forever. Your streak and stats follow you to any device.'
      : 'Log in to save your streak and see your stats.';
    $('auth-pw-hint').hidden = !reg;
    passIn.setAttribute('autocomplete', reg ? 'new-password' : 'current-password');
    submit.textContent = reg ? 'Create account' : 'Log in';
  }

  Synap.openAuth = function (tab) {
    lastFocus = doc.activeElement;
    setTab(tab || 'login');
    setMsg('');
    form.reset();
    dialog.hidden = false;
    setTimeout(function () { userIn.focus(); }, 30);
  };
  Synap.closeAuth = function () {
    dialog.hidden = true;
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  };

  all('[data-auth-tab]').forEach(function (b) {
    b.addEventListener('click', function () { setTab(b.getAttribute('data-auth-tab')); setMsg(''); });
  });
  doc.addEventListener('click', function (e) {
    var opener = e.target.closest('[data-open-auth]');
    if (opener) { e.preventDefault(); Synap.openAuth(opener.getAttribute('data-open-auth')); return; }
    if (e.target.closest('[data-close-auth]') || e.target === dialog) Synap.closeAuth();
    if (e.target.closest('[data-logout]')) logout();
  });

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var username = userIn.value.trim();
    var password = passIn.value;
    if (!username || !password) { setMsg('Fill in both fields.', 'error'); return; }
    var label = submit.textContent;
    submit.textContent = 'One moment';
    submit.disabled = true;
    fetch(authTab === 'login' ? '/login' : '/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ username: username, password: password })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        submit.disabled = false;
        submit.textContent = label;
        if (!res.ok) { setMsg(res.data.error || 'Something went wrong. Try again.', 'error'); return; }
        if (authTab === 'register') {
          setTab('login');
          userIn.value = username;
          passIn.value = '';
          passIn.focus();
          setMsg('Account created. Log in to start.', 'ok');
          return;
        }
        Synap.user = { username: username };
        dialog.hidden = true;
        emitAuth(res.data, 'login');
      })
      .catch(function () {
        submit.disabled = false;
        submit.textContent = label;
        setMsg('Network error. Check your connection and try again.', 'error');
      });
  });

  function logout() {
    fetch('/logout', { credentials: 'same-origin' }).then(function () {
      Synap.user = null;
      closeStats();
      emitAuth({ logged_in: false }, 'logout');
    });
  }

  // ── My stats popover ─────────────────────────────────────────────────────
  var pop = $('stats-popover');
  var popBtn = $('stats-toggle');

  function prettyTheme(token) {
    if (!token) return '';
    return token.replace(/([A-Z])/g, ' $1').replace(/^./, function (c) { return c.toUpperCase(); });
  }
  Synap.prettyTheme = prettyTheme;

  Synap.refreshStats = function () {
    if (!Synap.user) return Promise.resolve(null);
    return fetch('/get-analytics', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.error) return null;
        $('pop-total').textContent = d.total_attempts;
        $('pop-accuracy').textContent = d.total_attempts > 0 ? Math.round(d.accuracy) + '%' : '0%';
        $('pop-velocity').textContent = d.avg_time_ms > 0 ? (d.avg_time_ms / 1000).toFixed(1) + 's' : '-';
        var themes = (d.by_theme || []).slice(0, 5);
        $('pop-themes').innerHTML = themes.length
          ? '<dl>' + themes.map(function (t) {
              return '<div class="kv"><dt>' + prettyTheme(t.theme) + '</dt><dd>' + Math.round(t.accuracy) + '%</dd></div>';
            }).join('') + '</dl>'
          : '';
        global.dispatchEvent(new CustomEvent('synap:stats', { detail: d }));
        return d;
      })
      .catch(function () { return null; });
  };

  function closeStats() {
    if (!pop) return;
    pop.hidden = true;
    if (popBtn) popBtn.setAttribute('aria-expanded', 'false');
  }
  if (popBtn) {
    popBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = pop.hidden;
      pop.hidden = !open;
      popBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (open) Synap.refreshStats();
    });
    doc.addEventListener('click', function (e) {
      if (!pop.hidden && !pop.contains(e.target)) closeStats();
    });
  }

  doc.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    closeDropdowns();
    closeStats();
    setMenu(false);
    if (!dialog.hidden) Synap.closeAuth();
  });

  // ── Script loader + confetti ─────────────────────────────────────────────
  var loaded = {};
  Synap.loadScript = function (src) {
    if (!loaded[src]) {
      loaded[src] = new Promise(function (resolve, reject) {
        var s = doc.createElement('script');
        s.src = src;
        s.onload = resolve;
        s.onerror = reject;
        doc.head.appendChild(s);
      });
    }
    return loaded[src];
  };
  Synap.confetti = function (opts) {
    if (reduceMotion) return;
    Synap.loadScript('/static/vendor/confetti-1.9.3.min.js').then(function () {
      if (typeof global.confetti !== 'function') return;
      global.confetti(Object.assign({
        particleCount: 80, spread: 70, startVelocity: 36, origin: { y: 0.65 },
        colors: ['#00dc82', '#0f2b1d', '#e9f1e6', '#5c9f7b'], shapes: ['square'],
        disableForReducedMotion: true
      }, opts || {}));
    }).catch(function () {});
  };

  // ── Scroll-in motion ─────────────────────────────────────────────────────
  // Children of [data-reveal] rise in one after another as the block enters.
  // Content is only hidden when the .js class is present, so it stays
  // visible if this script never runs.
  var reveals = all('[data-reveal]');
  reveals.forEach(function (block) {
    Array.prototype.forEach.call(block.children, function (child, i) { child.style.setProperty('--i', i); });
  });
  if ('IntersectionObserver' in global && !reduceMotion) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('in');
        io.unobserve(entry.target);
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });
    reveals.forEach(function (el) { io.observe(el); });
  } else {
    reveals.forEach(function (el) { el.classList.add('in'); });
  }

  // Count-up for figures marked data-count="100" (optional data-suffix).
  var counters = all('[data-count]');
  function runCount(el) {
    var target = parseFloat(el.getAttribute('data-count'));
    var suffix = el.getAttribute('data-suffix') || '';
    var decimals = (el.getAttribute('data-count').split('.')[1] || '').length;
    if (reduceMotion) { el.textContent = target.toFixed(decimals) + suffix; return; }
    var start = null;
    function step(ts) {
      if (start === null) start = ts;
      var p = Math.min(1, (ts - start) / 1100);
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = (target * eased).toFixed(decimals) + suffix;
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }
  if (counters.length && 'IntersectionObserver' in global) {
    var cio = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        runCount(entry.target);
        cio.unobserve(entry.target);
      });
    }, { threshold: 0.4 });
    counters.forEach(function (el) { cio.observe(el); });
  }

  global.Synap = Synap;
})(window);
