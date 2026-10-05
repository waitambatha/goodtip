/* "Who it's for" on the home page: one of the three cards is always lit.

   While the section is on screen the light walks 1 -> 2 -> 3 -> 1 ... every few
   seconds. Pointing at a card, or tabbing into it, takes the light and holds it
   there; letting go hands it back to the walk. The pointer also drives a soft
   glow across the lit card (--mx / --my).

   Reduced motion and no-JS get three plain cards. */
(function () {
  'use strict';

  var sec = document.querySelector('[data-who]');
  var grid = sec && sec.querySelector('[data-who-grid]');
  if (!grid) return;
  var cards = [].slice.call(grid.querySelectorAll('.who-card'));
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches && !document.documentElement.classList.contains('gt-motion');
  if (reduce) return;

  var STEP = 3200;
  var cur = -1, held = false, inView = false, timer = 0;

  function light(n) {
    cur = n;
    cards.forEach(function (c, i) { c.classList.toggle('lit', i === n); });
    grid.classList.toggle('has-lit', n >= 0);
  }
  function tick() {
    timer = 0;
    if (!inView || held || document.hidden) return;
    light((cur + 1) % cards.length);
    timer = setTimeout(tick, STEP);
  }
  function go() { if (!timer && inView && !held && !document.hidden) timer = setTimeout(tick, cur < 0 ? 500 : STEP); }
  function halt() { clearTimeout(timer); timer = 0; }

  cards.forEach(function (c, i) {
    c.addEventListener('pointerenter', function () { held = true; halt(); light(i); });
    c.addEventListener('pointerleave', function () { held = false; go(); });
    c.addEventListener('pointermove', function (e) {
      var r = c.getBoundingClientRect();
      c.style.setProperty('--mx', (e.clientX - r.left) + 'px');
      c.style.setProperty('--my', (e.clientY - r.top) + 'px');
    });
    c.addEventListener('focusin', function () { held = true; halt(); light(i); });
    c.addEventListener('focusout', function () { held = false; go(); });
  });

  document.addEventListener('visibilitychange', function () { if (document.hidden) halt(); else go(); });

  if ('IntersectionObserver' in window) {
    new IntersectionObserver(function (es) {
      inView = es[es.length - 1].isIntersecting;
      if (inView) go(); else { halt(); light(-1); }
    }, { threshold: .35 }).observe(grid);
  }
})();
