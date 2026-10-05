/* The Wall page's hero rotor: one post card at a time, its words typed out
 * live rather than just crossfaded in (client, 28 Sep 2026: "I want it to
 * act like a live chat, so I should see the words as if they are being live
 * typed... after that is complete the other one is typed, so a nice loop").
 *
 * The card itself (avatar, name, chips) still just fades in as before —
 * only the post's own words, .wr-body, type themselves out, one character
 * at a time, before the card holds and the next one takes its turn.
 *
 * The stack's children are the finished picture already (server-rendered,
 * full text, the first one shown) — that is what stays with JavaScript off
 * or with reduced motion. This script blanks the words and replays them,
 * the same "markup is the source of truth" rule as gt-live.js next to it.
 */
(function () {
  'use strict';

  var stack = document.querySelector('[data-rotor]');
  if (!stack) return;
  var slots = [].slice.call(stack.children);
  if (!slots.length) return;
  slots.forEach(function (s, i) { s.classList.add('wr-slot'); if (i === 0) s.classList.add('on'); });

  var dotsWrap = document.querySelector('[data-rotor-dots]');
  if (dotsWrap && slots.length > 1) {
    slots.forEach(function (_, i) {
      var d = document.createElement('span');
      d.className = 'wr-dot' + (i === 0 ? ' on' : '');
      d.addEventListener('click', function () { jump(i); });
      dotsWrap.appendChild(d);
    });
  }

  var forced = document.documentElement.classList.contains('gt-motion');
  var reduce = !forced && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduce) return;

  function runs(node, out) {
    out = out || [];
    [].forEach.call(node.childNodes, function (kid) {
      if (kid.nodeType === 3) { if (kid.nodeValue.length) out.push(kid); }
      else if (kid.nodeType === 1) runs(kid, out);
    });
    return out;
  }

  var script = slots.map(function (slot) {
    var body = slot.querySelector('.wr-body');
    return { slot: slot, body: body, parts: body ? runs(body).map(function (n) { return { node: n, full: n.nodeValue }; }) : [] };
  });
  if (!script.some(function (e) { return e.parts.length; })) return;

  function blank(e) { e.parts.forEach(function (p) { p.node.nodeValue = ''; }); }
  function fill(e) { e.parts.forEach(function (p) { p.node.nodeValue = p.full; }); }

  /* Blank every card's words up front, before the first paint has a chance
     to show them full and then yank them away. */
  stack.classList.add('wrh-js');
  script.forEach(blank);

  var CPS = 30;
  var THINK_MS = 500, HOLD_MS = 2600, OUT_MS = 550;
  var gen = 0, idx = 0, running = false, inView = false;

  function wait(ms, t) { return new Promise(function (res) { setTimeout(function () { res(t === gen); }, ms); }); }

  function typeBody(e, t) {
    return new Promise(function (done) {
      var parts = e.parts, i = 0, k = 0;
      (function chr() {
        if (t !== gen) return done(false);
        if (i >= parts.length) return done(true);
        var full = parts[i].full;
        if (k >= full.length) { i++; k = 0; return chr(); }
        k++;
        parts[i].node.nodeValue = full.slice(0, k);
        var ch = full.charAt(k - 1), d = 1000 / CPS * (0.6 + Math.random() * 0.8);
        if (ch === '.' || ch === '!' || ch === '?') d += 260;
        else if (ch === ',') d += 100;
        setTimeout(chr, d);
      })();
    });
  }

  function showSlot(n) {
    slots.forEach(function (s, i) {
      if (i === n) { s.classList.remove('out'); s.classList.add('on'); }
      else { s.classList.remove('on'); }
    });
    if (dotsWrap) [].forEach.call(dotsWrap.children, function (d, i) { d.classList.toggle('on', i === n); });
  }

  async function step(n, t) {
    var e = script[n];
    showSlot(n);
    if (!(await wait(THINK_MS, t))) return false;
    if (e.parts.length) {
      e.slot.classList.add('typing');
      var ok = await typeBody(e, t);
      e.slot.classList.remove('typing');
      if (!ok) return false;
    }
    if (!(await wait(HOLD_MS, t))) return false;
    e.slot.classList.add('out'); e.slot.classList.remove('on');
    if (!(await wait(OUT_MS, t))) return false;
    blank(e);
    return true;
  }

  async function play(t) {
    for (;;) {
      if (!(await step(idx, t))) return;
      idx = (idx + 1) % slots.length;
    }
  }

  function begin() {
    var t = ++gen;
    running = true;
    play(t).then(function () { if (t === gen) running = false; });
  }
  function halt() { gen++; running = false; }

  function jump(n) {
    halt();
    script.forEach(fill);
    idx = n;
    begin();
  }

  function sync() {
    if (inView && !document.hidden) { if (!running) begin(); }
    else if (running) halt();
  }

  stack.addEventListener('mouseenter', halt);
  stack.addEventListener('mouseleave', function () { if (inView) begin(); });
  document.addEventListener('visibilitychange', sync);

  if ('IntersectionObserver' in window) {
    new IntersectionObserver(function (es) {
      inView = es[es.length - 1].isIntersecting;
      sync();
    }, { threshold: .25 }).observe(stack);
  } else { inView = true; sync(); }
})();
