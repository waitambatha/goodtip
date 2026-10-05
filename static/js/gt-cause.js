/* Charity partners: one plate that turns.

   The title of a set sits in the middle first, spins up into its place, and the
   cards flip in, each in its own colour. Then the whole plate turns 360 and the
   other set is on the other side. Round again while on screen. The two tabs
   above jump to a side.

   The markup is the finished picture: both sets written out, one under the
   other. That is what shows with JavaScript off, or with reduced motion (the
   site's own gate, so ?motion=1 overrides it). This script takes it apart and
   plays it back. Cancelling is a token bump, as in gt-how.js. */
(function () {
  'use strict';

  var sec = document.querySelector('[data-cpx]');
  if (!sec) return;

  var forced = document.documentElement.classList.contains('gt-motion');
  if (!forced && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  var stage = sec.querySelector('[data-cpx-stage]');
  var faces = [].slice.call(sec.querySelectorAll('.cpx-face'));
  var tabs = [].slice.call(sec.querySelectorAll('[data-cpx-tabs] button'));
  var token = 0, inView = false, running = false, cur = 0;

  function wait(ms, t) { return new Promise(function (res) { setTimeout(function () { res(t === token); }, ms); }); }
  function cards(f) { return [].slice.call(f.querySelectorAll('.cpx-card')); }

  function centre(f) {                          /* how far the title is from the middle of its face */
    var title = f.querySelector('.cpx-title');
    title.classList.remove('mid');
    var fh = f.offsetHeight, tr = title.offsetTop + title.offsetHeight / 2;
    title.style.setProperty('--ty', (fh / 2 - tr) + 'px');
  }
  function stage0(f) {                          /* title in the middle, no cards yet */
    var title = f.querySelector('.cpx-title');
    title.classList.remove('spin');
    cards(f).forEach(function (c) { c.classList.remove('in'); });
    centre(f);
    void title.offsetWidth;
    title.style.transition = 'none';
    title.classList.add('mid');
    void title.offsetWidth;
    title.style.transition = '';
  }
  function tab(i) { tabs.forEach(function (b, k) { b.classList.toggle('on', k === i); b.setAttribute('aria-selected', k === i); }); }

  async function fill(i, t) {
    var f = faces[i], title = f.querySelector('.cpx-title');
    if (!(await wait(900, t))) return false;
    title.classList.remove('mid'); void title.offsetWidth; title.classList.add('spin');   /* up and round */
    if (!(await wait(550, t))) return false;
    var cs = cards(f);
    for (var k = 0; k < cs.length; k++) {
      cs[k].classList.add('in');
      if (!(await wait(120, t))) return false;
    }
    return true;
  }

  async function turn(to, t) {
    stage.classList.remove('turn'); void stage.offsetWidth; stage.classList.add('turn');
    if (!(await wait(750, t))) return false;      /* half way round, the back is showing */
    faces[cur].classList.remove('on');
    stage0(faces[to]);
    faces[to].classList.add('on');
    cur = to; tab(to);
    if (!(await wait(800, t))) return false;
    stage.classList.remove('turn');
    return true;
  }

  async function play(t, first) {
    var i = first;
    for (;;) {
      if (!(await fill(i, t))) return;
      if (!(await wait(i === 0 ? 5200 : 4200, t))) return;
      i = 1 - i;
      if (!(await turn(i, t))) return;
    }
  }

  function begin(first) {
    var t = ++token;
    running = true;
    faces.forEach(function (f, k) { f.classList.toggle('on', k === cur); });
    if (first !== cur) { faces[cur].classList.remove('on'); cur = first; faces[cur].classList.add('on'); }
    tab(cur); stage0(faces[cur]);
    play(t, cur).then(function () { if (t === token) running = false; });
  }
  function halt() { token++; running = false; }

  sec.classList.add('cpx-js');
  faces.forEach(function (f) { f.classList.remove('on'); });
  faces[0].classList.add('on');

  tabs.forEach(function (b, k) {
    b.addEventListener('click', function () {
      if (!inView || k === cur) return;
      var t = ++token; running = true;
      stage.classList.remove('turn');
      turn(k, t).then(function (ok) { if (ok) return play(t, k); });
    });
  });

  window.addEventListener('resize', function () { if (!running) faces.forEach(function (f) { if (f.classList.contains('on')) centre(f); }); });

  if (!('IntersectionObserver' in window)) { inView = true; begin(0); return; }
  new IntersectionObserver(function (es) {
    inView = es[es.length - 1].isIntersecting;
    if (inView && !running) begin(cur);
    else if (!inView) { halt(); stage.classList.remove('turn'); }
  }, { threshold: .35 }).observe(sec);
})();
