/* The How It Works ladder showcase: three cards, one panel underneath.
 *
 * A synthetic cursor presses a card, the read-only board (the same one
 * gt-board.js drives, rotating competitions on its own) drops open capped
 * to six rows, the cursor scrolls down to show the rest and back up, then
 * presses the X to close it before moving on to the next card. Loops while
 * the section is on screen.
 *
 * The markup is the finished picture: the panel already shows open, one
 * card already reads as current. That is what shows with JavaScript off or
 * with reduced motion (the site's own gate, ?motion=1 overrides it). This
 * script takes it apart and plays it back. Cancelling is a token bump, as
 * in gt-pick.js.
 */
(function () {
  'use strict';

  var demo = document.querySelector('[data-lbdemo]');
  if (!demo) return;

  var forced = document.documentElement.classList.contains('gt-motion');
  if (!forced && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  var panel = demo.querySelector('[data-lb-panel]');
  var rows = demo.querySelector('[data-lb-rows]');
  var closeBtn = demo.querySelector('[data-lb-close]');
  var cursor = demo.querySelector('[data-lb-cursor]');
  var cards = [].slice.call(demo.querySelectorAll('[data-lb-card]'));
  if (!panel || !rows || !closeBtn || !cursor || !cards.length) return;

  var token = 0, inView = false, running = false;

  function wait(ms, t) { return new Promise(function (res) { setTimeout(function () { res(t === token); }, ms); }); }

  function pt(el, fx, fy) {
    var dr = demo.getBoundingClientRect(), r = el.getBoundingClientRect();
    cursor.style.transform = 'translate(' + (r.left - dr.left + r.width * fx) + 'px,' + (r.top - dr.top + r.height * fy) + 'px)';
  }
  function tap() { cursor.classList.remove('tap'); void cursor.offsetWidth; cursor.classList.add('tap'); }

  function clear() {
    cards.forEach(function (c) { c.classList.remove('active'); });
    panel.classList.remove('open');
    rows.scrollTop = 0;
    cursor.classList.remove('show');
  }

  async function step(k, t) {
    var card = cards[k];
    cursor.style.transition = 'none';
    cursor.style.transform = 'translate(' + (demo.offsetWidth / 2 - 14) + 'px,-40px)';
    void cursor.offsetWidth; cursor.style.transition = '';
    cursor.classList.add('show');
    pt(card, .5, .5);
    if (!(await wait(750, t))) return false;
    tap(); card.classList.add('active');
    if (!(await wait(250, t))) return false;
    panel.classList.add('open');
    if (!(await wait(650, t))) return false;

    pt(rows, .5, .35);
    if (!(await wait(700, t))) return false;
    if (rows.scrollHeight > rows.clientHeight) {
      rows.scrollTo({ top: rows.scrollHeight - rows.clientHeight, behavior: 'smooth' });
      pt(rows, .5, .85);
      if (!(await wait(900, t))) return false;
      rows.scrollTo({ top: 0, behavior: 'smooth' });
      if (!(await wait(700, t))) return false;
    }

    pt(closeBtn, .5, .5);
    if (!(await wait(600, t))) return false;
    tap();
    if (!(await wait(200, t))) return false;
    panel.classList.remove('open');
    card.classList.remove('active');
    cursor.classList.remove('show');
    if (!(await wait(550, t))) return false;
    return true;
  }

  async function play(t) {
    for (var k = 0; ; k = (k + 1) % cards.length) {
      if (!(await step(k, t))) return;
    }
  }

  function begin() {
    var t = ++token;
    running = true;
    clear();
    play(t).then(function () { if (t === token) running = false; });
  }
  function halt() { token++; running = false; clear(); }

  demo.classList.add('lb-js');

  if (!('IntersectionObserver' in window)) { begin(); return; }
  new IntersectionObserver(function (es) {
    inView = es[0].isIntersecting;
    if (inView && !running) begin();
    else if (!inView) halt();
  }, { threshold: 0.35 }).observe(demo);
})();
