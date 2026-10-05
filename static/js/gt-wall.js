/* The Wall, on the home page: words typed on the left, a phone on the right.

   The phone shows two threads (a post, and its reply, twice). Then a hand taps
   the rotate button at the bottom right, the phone turns to landscape, slides
   to the middle, and more of the room comes in while the ladder shuffles.
   Then it fades and the whole thing starts again while the section is on
   screen.

   The markup is the finished picture (every message written out, the phone
   upright). That is what shows with JavaScript off, or with reduced motion by
   the site's own gate, so ?motion=1 overrides it. This script takes it apart
   and plays it back. Cancelling is a token bump, as in gt-how.js. */
(function () {
  'use strict';

  var sec = document.querySelector('[data-ww]');
  if (!sec) return;

  var forced = document.documentElement.classList.contains('gt-motion');
  if (!forced && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  var split = sec.querySelector('[data-ww-split]');
  var stage = sec.querySelector('[data-ww-stage]');
  var phone = sec.querySelector('[data-phone]');
  var hand = sec.querySelector('.ww-hand');
  var rot = sec.querySelector('[data-rot]');
  var pflow = sec.querySelector('.ww-screen.p .ww-flow');
  var lflow = sec.querySelector('[data-lflow]');
  var P = [].slice.call(pflow.querySelectorAll('.ww-m'));
  var L = [].slice.call(lflow.querySelectorAll('.ww-m'));
  var ladder = [].slice.call(sec.querySelectorAll('.ww-lr'));
  var token = 0, inView = false, running = false, typedLeft = false;
  var tokenForever = -1;   /* the words are not part of the phone loop: they type on a token nothing cancels */

  function wait(ms, t) { return new Promise(function (res) { setTimeout(function () { res(t === tokenForever || t === token); }, ms); }); }
  function runs(node, out) {
    [].forEach.call(node.childNodes, function (kid) {
      if (kid.nodeType === 3) { if (kid.nodeValue.length) out.push(kid); }
      else if (kid.nodeType === 1) runs(kid, out);
    });
    return out;
  }
  function caretEl() {
    var c = document.createElement('span');
    c.className = 'ww-caret'; c.setAttribute('aria-hidden', 'true');
    return c;
  }
  /* a typed run of text nodes; cps is characters per second; cancels on token */
  async function typeNodes(nodes, cps, t, onTick, pauses) {
    var caret = caretEl();
    for (var k = 0; k < nodes.length; k++) {
      var p = nodes[k];
      p.node.parentNode.insertBefore(caret, p.node.nextSibling);
      for (var i = 1; i <= p.full.length; i++) {
        p.node.nodeValue = p.full.slice(0, i);
        if (p.rest) p.rest.textContent = p.full.slice(i);
        if (onTick && i % 5 === 0) onTick();
        var ch = p.full.charAt(i - 1), nx = p.full.charAt(i);
        var d = 1000 / cps * (0.6 + Math.random() * 0.8);
        if (pauses !== false) { if (ch === '.' && nx === ' ') d += 280; else if (ch === ',') d += 110; }
        if (!(await wait(d, t))) { if (caret.parentNode) caret.parentNode.removeChild(caret); return false; }
      }
    }
    if (caret.parentNode) caret.parentNode.removeChild(caret);
    return true;
  }

  /* ---- the words on the left, typed once ---------------------------------- */
  var lines = [].slice.call(sec.querySelectorAll('[data-ty]')).map(function (el) {
    var holder = el.tagName === 'SPAN' ? el.parentNode : el;
    /* 2 Oct 2026: the untyped remainder stays laid out, unseen (.ty-rest), so
       a line holds its finished size while it types and the page below it
       cannot move — the "vibration" the client saw scrolling past. */
    return { holder: holder, nodes: runs(el, []).map(function (n) {
      var rest = document.createElement('span');
      rest.className = 'ty-rest'; rest.setAttribute('aria-hidden', 'true');
      n.parentNode.insertBefore(rest, n.nextSibling);
      return { node: n, full: n.nodeValue, rest: rest };
    }) };
  });
  var late = [].slice.call(sec.querySelectorAll('.ww-cta'));

  function prepareLeft() {
    lines.forEach(function (l) { l.nodes.forEach(function (p) { p.node.nodeValue = ''; p.rest.textContent = p.full; }); l.holder.classList.add('ww-pending'); });
    late.forEach(function (e) { e.classList.add('ww-late'); });
    sec.classList.add('ww-js');
  }
  async function typeLeft() {
    typedLeft = true;
    await new Promise(function (r) { setTimeout(r, 300); });
    for (var i = 0; i < lines.length; i++) {
      lines[i].holder.classList.remove('ww-pending');
      await typeNodes(lines[i].nodes, i < 2 ? 36 : 70, tokenForever);
      await new Promise(function (r) { setTimeout(r, i === 1 ? 400 : 200); });
    }
    late.forEach(function (e) { e.classList.add('in'); });
  }

  /* ---- the phone ----------------------------------------------------------- */
  P.concat(L).forEach(function (m) {
    m.__nodes = runs(m.querySelector('p'), []).map(function (n) { return { node: n, full: n.nodeValue }; });
  });

  function fit(flow, instant) {
    var feed = flow.parentNode, over = flow.offsetHeight - feed.clientHeight;
    if (instant) flow.style.transition = 'none';
    flow.style.transform = over > 0 ? 'translateY(' + (-over) + 'px)' : '';
    if (instant) { void flow.offsetHeight; flow.style.transition = ''; }
  }

  function settleLadder() {
    var order = ['Priya', 'Big Dave', 'Sam', 'The Quiet One'];
    ladder.forEach(function (r) {
      var i = order.indexOf(r.getAttribute('data-n'));
      r.style.setProperty('--i', i); r.querySelector('b').textContent = i + 1; r.classList.remove('up');
    });
  }
  function overtake() {                          /* the Quiet One up to 3rd */
    var q = ladder.filter(function (r) { return r.getAttribute('data-n') === 'The Quiet One'; })[0];
    var s = ladder.filter(function (r) { return r.getAttribute('data-n') === 'Sam'; })[0];
    q.style.setProperty('--i', 2); q.querySelector('b').textContent = 3; q.classList.add('up');
    s.style.setProperty('--i', 3); s.querySelector('b').textContent = 4;
  }

  function reset() {
    var scr = [].slice.call(phone.querySelectorAll('.ww-screen'));
    phone.style.transition = 'none';
    scr.forEach(function (s) { s.style.transition = 'none'; });
    split.classList.remove('land');
    void phone.offsetWidth;
    phone.style.transition = '';
    scr.forEach(function (s) { s.style.transition = ''; });
    [].forEach.call(lflow.querySelectorAll('.ww-clone'), function (c) { c.parentNode.removeChild(c); });
    P.concat(L).forEach(function (m) {
      m.classList.add('ww-hide'); m.classList.remove('ww-in');
      m.__nodes.forEach(function (p) { p.node.nodeValue = ''; });
      m.querySelector('.ww-r').classList.add('ww-hide');
    });
    [].forEach.call(sec.querySelectorAll('.ww-typing-row'), function (e) { e.parentNode.removeChild(e); });
    fit(pflow, true); fit(lflow, true);
    settleLadder();
    hand.classList.remove('show', 'tap');
    rot.classList.remove('tap');
    stage.classList.remove('out');
  }

  function measure() {
    var a = split.getBoundingClientRect(), b = stage.getBoundingClientRect();
    var zs = parseFloat(getComputedStyle(sec).getPropertyValue('--zs')) || 1;   /* the phone sits inside a scaled box */
    phone.style.setProperty('--dx', (((a.left + a.width / 2) - (b.left + b.width / 2)) / zs) + 'px');
  }

  async function reveal(m, flow, dots, t) {
    if (dots) {
      var ind = document.createElement('div');
      ind.className = 'ww-m ww-typing-row' + (m.classList.contains('re') ? ' re' : '');
      ind.innerHTML = m.querySelector('.ww-av').outerHTML + '<div class="ww-typing"><i></i><i></i><i></i></div>';
      flow.insertBefore(ind, m);
      fit(flow);
      if (!(await wait(850, t))) return false;
      ind.parentNode.removeChild(ind);
    }
    m.classList.remove('ww-hide'); void m.offsetWidth; m.classList.add('ww-in');
    fit(flow);
    if (!(await typeNodes(m.__nodes, 62, t, function () { fit(flow); }))) return false;
    fit(flow);
    m.querySelector('.ww-r').classList.remove('ww-hide');
    return true;
  }

  async function tapRotate(t) {
    var s = stage.getBoundingClientRect(), r = rot.getBoundingClientRect();
    var tx = r.left - s.left + r.width * .5, ty = r.top - s.top + r.height * .5;
    function at(x, y) { hand.style.transform = 'translate(' + (x - 7) + 'px,' + (y - 4) + 'px)'; }
    hand.style.transition = 'none';
    at(tx + 50, s.height + 30);
    void hand.offsetWidth; hand.style.transition = '';
    hand.classList.add('show');
    at(tx, ty);
    if (!(await wait(1050, t))) return false;
    hand.classList.remove('tap'); void hand.offsetWidth; hand.classList.add('tap');
    rot.classList.add('tap');
    if (!(await wait(350, t))) return false;
    at(tx + 50, s.height + 30); hand.classList.remove('show');
    return true;
  }

  async function play(t) {
    for (;;) {
      reset();
      measure();
      if (!(await wait(700, t))) return;
      /* two threads: a post, then the reply to it */
      if (!(await reveal(P[0], pflow, true, t))) return;
      if (!(await wait(450, t))) return;
      if (!(await reveal(P[1], pflow, true, t))) return;
      if (!(await wait(700, t))) return;
      if (!(await reveal(P[2], pflow, false, t))) return;
      if (!(await wait(450, t))) return;
      if (!(await reveal(P[3], pflow, true, t))) return;
      if (!(await wait(1100, t))) return;
      /* the hand goes for the rotate button */
      if (!(await tapRotate(t))) return;
      /* the thread carries over into landscape, then the phone turns and slides */
      var first = L[0];
      P.forEach(function (m) {
        var c = m.cloneNode(true);
        c.classList.add('ww-clone'); c.classList.remove('ww-in');
        lflow.insertBefore(c, first);
      });
      fit(lflow, true);
      measure();
      split.classList.add('land');
      if (!(await wait(1900, t))) return;
      /* more of the room comes in */
      for (var k = 0; k < L.length; k++) {
        if (!(await reveal(L[k], lflow, k !== 2, t))) return;
        if (k === 0) overtake();
        if (!(await wait(k === 3 ? 100 : 600, t))) return;
      }
      if (!(await wait(3000, t))) return;
      stage.classList.add('out');
      if (!(await wait(700, t))) return;
    }
  }

  function begin() {
    var t = ++token;
    running = true;
    play(t).then(function () { if (t === token) running = false; });
  }
  function halt() { token++; running = false; }
  function sync() {
    if (inView && !document.hidden) {
      if (!typedLeft) typeLeft();
      if (!running) begin();
    } else if (running) {
      halt(); reset();
    }
  }

  prepareLeft();
  reset();
  window.addEventListener('resize', measure);
  document.addEventListener('visibilitychange', sync);
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(function (es) {
      var was = inView;
      inView = es[es.length - 1].isIntersecting;
      if (inView !== was) sync();
    }, { threshold: .3 }).observe(sec);
  } else { inView = true; sync(); }
})();
