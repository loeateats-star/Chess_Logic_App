/*
 * First-login onboarding: three quick questions, saved to /save-diagnostic,
 * then a redirect to the starting point that matches the student's answer.
 * Opens after a login on any page when the account hasn't done it yet.
 */
(function () {
  'use strict';
  var dialog = document.getElementById('wizard-dialog');
  if (!dialog || !window.Synap) return;

  var data = {}, redirect = '/', step = 1;

  function show(n) {
    step = n;
    Array.prototype.forEach.call(dialog.querySelectorAll('[data-wiz-step]'), function (el) {
      el.hidden = el.getAttribute('data-wiz-step') !== String(n);
    });
    Array.prototype.forEach.call(dialog.querySelectorAll('.steps i'), function (dot, i) {
      dot.classList.toggle('on', i < n);
    });
    var first = dialog.querySelector('[data-wiz-step="' + n + '"] .choice');
    if (first) first.focus();
  }

  function open() {
    data = { experience_level: '', daily_time_budget: 30, training_goal: '' };
    redirect = '/';
    Array.prototype.forEach.call(dialog.querySelectorAll('.choice'), function (c) { c.classList.remove('is-picked'); });
    dialog.hidden = false;
    show(1);
  }

  function close() { dialog.hidden = true; }

  dialog.addEventListener('click', function (e) {
    if (e.target.closest('[data-wiz-skip]')) { close(); return; }
    var choice = e.target.closest('.choice');
    if (!choice) return;
    var key = choice.getAttribute('data-wiz');
    var val = choice.getAttribute('data-value');
    data[key] = key === 'daily_time_budget' ? parseInt(val, 10) : val;
    if (choice.getAttribute('data-go')) redirect = choice.getAttribute('data-go');
    Array.prototype.forEach.call(choice.parentNode.children, function (c) { c.classList.remove('is-picked'); });
    choice.classList.add('is-picked');
    setTimeout(function () {
      if (step < 3) { show(step + 1); return; }
      SynapchessSync.queuedPost('/save-diagnostic', data).then(function () {
        close();
        window.location.href = redirect;
      });
    }, 240);
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !dialog.hidden) close();
  });

  Synap.onAuth(function (user, d, source) {
    if (source === 'login' && d && d.diagnostic_done === 0) open();
  });
})();
