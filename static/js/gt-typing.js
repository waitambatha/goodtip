/* ---------------------------------------------------------------------------
 * "SAM IS TYPING…" — the Wall and Messages (client, 16 Nov 2026).
 *
 * A composer carrying data-typing-url and data-typing-ch takes part:
 *   - while its box has words and keys are being pressed, it tells the server
 *     at most every 3 seconds; it says it has stopped on send, on emptying the
 *     box, and after 5 quiet seconds;
 *   - every 2.5 seconds, while the page is in view, it asks who else is
 *     typing there and shows "Sam is typing…" above the box.
 * The server forgets a typist after 6 seconds on its own, so a closed tab
 * never leaves a ghost. Everything is found at the moment it is needed, so a
 * room or conversation swapped in by htmx simply takes over.
 * ------------------------------------------------------------------------- */
(function () {
  'use strict';

  var SEND_EVERY = 3000, QUIET = 5000, POLL = 2500;
  var lastSent = 0, sentCh = null, quietTimer = null;

  function csrf(form) {
    var i = form.querySelector('input[name="csrfmiddlewaretoken"]');
    return i ? i.value : '';
  }
  function composer() { return document.querySelector('form[data-typing-ch]'); }

  function signal(form, on) {
    var body = new FormData();
    body.append('ch', form.dataset.typingCh);
    body.append('state', on ? '1' : '0');
    fetch(form.dataset.typingUrl, {
      method: 'POST', body: body, credentials: 'same-origin', keepalive: true,
      headers: { 'X-CSRFToken': csrf(form) }
    }).catch(function () { /* a missed signal is harmless */ });
    lastSent = on ? Date.now() : 0;
    sentCh = on ? form.dataset.typingCh : null;
  }

  function stop(form) {
    clearTimeout(quietTimer);
    if (sentCh && form && form.dataset.typingCh === sentCh) signal(form, false);
  }

  document.addEventListener('input', function (e) {
    var box = e.target;
    if (!box.matches || !box.matches('textarea')) return;
    var form = box.closest('form[data-typing-ch]');
    if (!form || box.closest('[data-chat-edit]')) return;
    if (!box.value.trim()) { stop(form); return; }
    if (sentCh !== form.dataset.typingCh || Date.now() - lastSent > SEND_EVERY) signal(form, true);
    clearTimeout(quietTimer);
    quietTimer = setTimeout(function () { stop(form); }, QUIET);
  });

  document.addEventListener('submit', function (e) {
    var form = e.target.closest && e.target.closest('form[data-typing-ch]');
    if (form) stop(form);
  }, true);
  window.addEventListener('pagehide', function () { stop(composer()); });

  function words(names) {
    if (names.length === 1) return names[0] + ' is typing';
    if (names.length === 2) return names[0] + ' and ' + names[1] + ' are typing';
    if (names.length === 3) return names[0] + ', ' + names[1] + ' and ' + names[2] + ' are typing';
    return names[0] + ', ' + names[1] + ' and ' + (names.length - 2) + ' others are typing';
  }

  function show(form, names) {
    var bar = form.querySelector('[data-typing-show]');
    if (!bar) return;
    if (!names.length) { bar.hidden = true; return; }
    bar.querySelector('[data-typing-text]').textContent = words(names) + '…';
    bar.hidden = false;
  }

  var busy = false;
  function poll() {
    var form = composer();
    if (!form || busy || document.visibilityState !== 'visible') return;
    busy = true;
    var ch = form.dataset.typingCh;
    fetch(form.dataset.typingUrl + '?ch=' + encodeURIComponent(ch), { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : { typing: [] }; })
      .then(function (d) {
        var now = composer();
        if (now && now.dataset.typingCh === ch) show(now, d.typing || []);
      })
      .catch(function () {})
      .then(function () { busy = false; });
  }
  setInterval(poll, POLL);
  document.addEventListener('visibilitychange', poll);
  document.addEventListener('htmx:afterSettle', poll);
})();
