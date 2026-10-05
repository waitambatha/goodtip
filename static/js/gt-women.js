/* "Women's sport included, always" on the home page.

   CLIENT, 26 SEP 2026: split it. On the left, what GoodTip does for women's
   sport, typed out live. On the right, the women's games being tipped, and
   only the ones that are not locked.

   LEFT. The words are written in the template and this script empties them and
   types them back, in order, so the copy can still be edited where it lives.
   Each line keeps its height while it is empty, so nothing below it jumps.

   RIGHT. A sample round, AFLW then NRLW then round again. A cursor tries the
   game that has already started (it is locked, and says so), then tips each
   game that is still open. A tip takes the competition's colour, the slip
   counts up, and the round is marked saved. Clicking a tab plays that
   competition now.

   It only runs while on screen. Reduced motion (by the site's own gate, so
   ?motion=1 overrides it) gets the section as written, with the tips already
   made and the tabs still working. */
(function () {
  'use strict';

  var sec = document.querySelector('[data-wm]');
  var card = sec && sec.querySelector('[data-wm-card]');
  if (!card) return;

  var forced = document.documentElement.classList.contains('gt-motion');
  var reduce = !forced && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var tabs = [].slice.call(card.querySelectorAll('.wm-tab'));
  var panels = [].slice.call(card.querySelectorAll('.wm-panel'));
  var cursor = card.querySelector('.wm-cursor');
  var count = card.querySelector('.wm-count b');
  var bar = card.querySelector('.wm-bar i');
  var CHOICE = [[0, 1, 0], [1, 0, 1]];    /* which side of each open game gets tipped, per tab */

  var token = 0, tab = 0, inView = false, running = false, typed = false;

  function wait(ms, t) { return new Promise(function (res) { setTimeout(function () { res(t === token); }, ms); }); }

  /* ---- the sample round --------------------------------------------------- */
  function open(n) { return [].slice.call(panels[n].querySelectorAll('.wm-fix:not(.locked)')); }
  function picks(fix) { return [].slice.call(fix.querySelectorAll('.wm-pick')); }

  function setTab(n) {
    tab = n;
    card.classList.toggle('is-nrlw', n === 1);
    tabs.forEach(function (b, i) { b.classList.toggle('on', i === n); b.setAttribute('aria-pressed', i === n ? 'true' : 'false'); });
    panels.forEach(function (p, i) { p.classList.toggle('on', i === n); });
    clear();
  }
  function clear() {
    card.classList.remove('done', 'warn');
    cursor.classList.remove('show');
    panels.forEach(function (p) {
      [].forEach.call(p.querySelectorAll('.wm-pick'), function (k) { k.classList.remove('on', 'off'); });
      [].forEach.call(p.querySelectorAll('.wm-fix'), function (f) { f.classList.remove('nope'); });
    });
    tally(0);
  }
  function tally(k) {
    count.textContent = k;
    bar.style.width = (k / 3 * 100) + '%';
  }
  function tip(n, i) {
    var pk = picks(open(n)[i]);
    pk[CHOICE[n][i]].classList.add('on');
    pk[1 - CHOICE[n][i]].classList.add('off');
  }
  function finished(n) {
    setTab(n);
    for (var i = 0; i < 3; i++) tip(n, i);
    tally(3);
    card.classList.add('done');
  }

  function moveTo(el, t) {
    var r = el.getBoundingClientRect(), c = card.getBoundingClientRect();
    cursor.classList.add('show');
    cursor.style.transform = 'translate(' + (r.left - c.left + r.width * .55) + 'px,' + (r.top - c.top + r.height * .55) + 'px)';
    return wait(850, t);
  }
  function tap() {
    cursor.classList.remove('tap'); void cursor.offsetWidth; cursor.classList.add('tap');
  }

  async function round(n, t) {
    setTab(n);
    var c = card.getBoundingClientRect();
    cursor.style.transition = 'none';
    cursor.style.transform = 'translate(' + (c.width - 70) + 'px,' + (c.height - 50) + 'px)';
    void cursor.offsetWidth; cursor.style.transition = '';
    cursor.classList.add('show');
    if (!(await wait(900, t))) return false;

    /* the game that has started: it will not take a tip */
    var lockedFix = panels[n].querySelector('.wm-fix.locked');
    if (!(await moveTo(picks(lockedFix)[0], t))) return false;
    tap();
    lockedFix.classList.remove('nope'); void lockedFix.offsetWidth; lockedFix.classList.add('nope');
    card.classList.add('warn');
    if (!(await wait(1500, t))) return false;
    card.classList.remove('warn');
    if (!(await wait(250, t))) return false;

    /* the ones still open */
    for (var i = 0; i < 3; i++) {
      if (!(await moveTo(picks(open(n)[i])[CHOICE[n][i]], t))) return false;
      tap();
      if (!(await wait(140, t))) return false;
      tip(n, i);
      tally(i + 1);
      if (!(await wait(520, t))) return false;
    }
    cursor.classList.remove('show');
    card.classList.add('done');
    return wait(2800, t);
  }

  async function loop(t, first) {
    for (var n = first; ; n = 1 - n) {
      if (!(await round(n, t))) return;
    }
  }
  function begin(n) {
    var t = ++token;
    running = true;
    loop(t, n).then(function () { if (t === token) running = false; });
  }
  function halt() { token++; running = false; }

  tabs.forEach(function (b, i) {
    b.addEventListener('click', function () {
      if (reduce) { finished(i); return; }
      halt();
      begin(i);
    });
  });

  /* ---- typing the words ---------------------------------------------------- */
  function runs(node, out) {
    [].forEach.call(node.childNodes, function (kid) {
      if (kid.nodeType === 3) { if (kid.nodeValue.length) out.push(kid); }
      else if (kid.nodeType === 1) runs(kid, out);
    });
    return out;
  }

  /* 2 Oct 2026: the untyped remainder of each run is laid out — invisible —
     right after the typed part (span.ty-rest, visibility:hidden), so a line
     takes its finished size from the start and typing it never changes the
     page's height. Emptying the text instead made the section grow as it
     typed, shoving everything below it about: the home page "vibrating". */
  function withRest(n) {
    var rest = document.createElement('span');
    rest.className = 'ty-rest';
    rest.setAttribute('aria-hidden', 'true');
    n.parentNode.insertBefore(rest, n.nextSibling);
    return { node: n, full: n.nodeValue, rest: rest };
  }
  function put(p, i) { p.node.nodeValue = p.full.slice(0, i); p.rest.textContent = p.full.slice(i); }

  var lines = [].slice.call(sec.querySelectorAll('[data-type]')).map(function (el) {
    var holder = el.tagName === 'SPAN' ? el.parentNode : el;
    return { el: el, holder: holder, nodes: runs(el, []).map(withRest) };
  });
  var late = [].slice.call(sec.querySelectorAll('.wm-codes, .wm-left .src'));

  function prepare() {
    /* blank each line (its remainder stays laid out, unseen) */
    lines.forEach(function (l) { l.nodes.forEach(function (p) { put(p, 0); }); l.holder.classList.add('wm-pending'); });
    late.forEach(function (e) { e.classList.add('wm-late'); });
    sec.classList.add('wm-js');
  }

  async function typeLine(l, cps) {
    var caret = document.createElement('span');
    caret.className = 'wm-caret';
    caret.setAttribute('aria-hidden', 'true');
    l.holder.classList.remove('wm-pending');
    for (var k = 0; k < l.nodes.length; k++) {
      var p = l.nodes[k];
      p.node.parentNode.insertBefore(caret, p.node.nextSibling);
      for (var i = 1; i <= p.full.length; i++) {
        put(p, i);
        var ch = p.full.charAt(i - 1), nx = p.full.charAt(i);
        var d = 1000 / cps * (0.6 + Math.random() * 0.8);
        if (ch === '.' && nx === ' ') d += 320; else if (ch === ',') d += 120;
        await new Promise(function (r) { setTimeout(r, d); });
      }
    }
    if (caret.parentNode) caret.parentNode.removeChild(caret);
  }

  async function typeAll() {
    typed = true;
    await new Promise(function (r) { setTimeout(r, 350); });
    for (var i = 0; i < lines.length; i++) {
      await typeLine(lines[i], i === 0 ? 34 : 58);
      await new Promise(function (r) { setTimeout(r, i === 0 ? 450 : 260); });
    }
    late.forEach(function (e) { e.classList.add('in'); });
  }

  /* ---- on screen ----------------------------------------------------------- */
  function sync() {
    if (reduce) return;
    if (inView && !document.hidden) {
      if (!typed) typeAll();
      if (!running) begin(tab);
    } else if (running) {
      halt(); clear();
    }
  }

  if (reduce) {
    finished(0);
  } else {
    prepare();
    setTab(0);
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (es) {
        var was = inView;
        inView = es[es.length - 1].isIntersecting;
        if (inView !== was) sync();
      }, { threshold: .3 }).observe(sec);
    } else { inView = true; sync(); }
    document.addEventListener('visibilitychange', sync);
  }
})();
