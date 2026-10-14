/* The live demos inside the Charity Vote panels on /charities/ (client,
 * 6 Oct 2026: "I see the votes being done ... show case what it's talking
 * about"). gt-how.js opens each panel and announces its steps as hw:play
 * events ({step, ms}); this file only restarts the demo's CSS animations for
 * that step and counts any [data-count-to] numbers up across it. hw:stop puts
 * the demo back to its first frame for the next time round.
 *
 * Everything drawn is written out in the template at its finished state, so
 * with this file missing (or on a screen too small for the show) the cards
 * and panels still read correctly. */
(function () {
  'use strict';

  function fmt(n, el) {
    var s = Math.round(n).toLocaleString('en-AU');
    return (el.getAttribute('data-prefix') || '') + s;
  }

  function count(el, ms) {
    var from = parseFloat(el.getAttribute('data-count-from') || '0');
    var to = parseFloat(el.getAttribute('data-count-to'));
    var delay = parseFloat(el.getAttribute('data-count-delay') || '0') * 1000;
    var span = Math.max(300, (ms - delay) * 0.75);
    var t0 = performance.now() + delay;
    el.textContent = fmt(from, el);
    (function f(now) {
      if (!el.__counting) return;
      var k = Math.min(1, Math.max(0, (now - t0) / span));
      k = 1 - Math.pow(1 - k, 3);
      el.textContent = fmt(from + (to - from) * k, el);
      if (k < 1) requestAnimationFrame(f);
    })(t0);
  }

  [].forEach.call(document.querySelectorAll('[data-demo]'), function (demo) {
    demo.addEventListener('hw:play', function (e) {
      var d = e.detail || {};
      demo.classList.remove('go');
      void demo.offsetWidth;                       /* restart the CSS animations */
      demo.setAttribute('data-step', d.step || '');
      var app = demo.querySelector('.dm-app');
      if (app) {
        app.style.removeProperty('--dm-s');
        var room = demo.clientHeight - 36, need = app.offsetHeight;
        app.style.setProperty('--dm-s', need > room && room > 0 ? (room / need).toFixed(3) : '1');
      }
      demo.classList.add('go');
      [].forEach.call(demo.querySelectorAll('[data-count-to]'), function (el) {
        var only = el.getAttribute('data-count-step');
        if (only && only !== d.step) {
          /* A number that climbs in a later step shows where it starts. */
          var before = (el.getAttribute('data-count-before') || '').split(' ');
          if (before.indexOf(d.step) >= 0) { el.__counting = false; el.textContent = fmt(parseFloat(el.getAttribute('data-count-from') || '0'), el); }
          return;
        }
        el.__counting = true;
        count(el, d.ms || 3000);
      });
    });
    demo.addEventListener('hw:stop', function () {
      demo.classList.remove('go');
      demo.removeAttribute('data-step');
      [].forEach.call(demo.querySelectorAll('[data-count-to]'), function (el) { el.__counting = false; });
    });
  });
})();
