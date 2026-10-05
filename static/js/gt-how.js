/* "How it works"-style sections: cards that take turns on stage.

   The section keeps its normal height. When it scrolls into view the first card
   lifts out of its row, travels to the middle of the section and grows well past
   its own size, then spins and opens into a wide panel (10% clear either side):
   a third of it the explanation, the rest a short clip of the product doing
   exactly what the card says. When the clip is done the panel spins shut,
   shrinks and glides back to its place, and the next card takes its turn.

   It is timed, not scroll-driven, so nothing pins and nothing is scrolled past.
   It keeps going round while the section is on screen, and clicking a card
   gives that card its turn. Leaving the section cancels it and puts the cards
   back; coming back plays it again from the first card.

   Only wide, tall screens that are allowed to animate get the show. Narrow
   screens, short windows, reduced motion and data-saver keep the plain cards,
   and no video is fetched.

   28 SEP 2026: pulled out of the home page so the "How it works" page's own
   timeline and scoring sections could run the identical trick on their own
   cards — a [data-hw] section per instance now, found and set up independently
   (there can be more than one on a page), and the resting card is whatever
   carries .hw-card inside [data-hw-row] rather than a hardcoded .step-flow
   .step: the timeline keeps its marker-and-card grid, scoring keeps its
   points-badge squares, and each one's own row layout, shape and resting
   colours are untouched — only the class markers (.hw-card, s1/s2/s3 for the
   colour cascade) are shared plumbing. */
(function () {
  'use strict';

  var sections = [].slice.call(document.querySelectorAll('[data-hw]'));
  if (!sections.length) return;

  function clamp(x, a, b) { return x < a ? a : x > b ? b : x; }
  function ease(t) { return t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; }
  function lerp(a, b, t) { return a + (b - a) * t; }

  function initHow(sec) {
    var root = document.documentElement;
    var stage = sec.querySelector('.hw-stage');
    var cards = [].slice.call(sec.querySelectorAll('[data-hw-row] .hw-card'));
    var panels = [].slice.call(sec.querySelectorAll('.hw-panel'));
    var base = sec.getAttribute('data-base') || '';
    if (!stage || !cards.length || cards.length !== panels.length) return;

    var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches && !root.classList.contains('gt-motion');
    var wide = window.matchMedia('(min-width: 1100px) and (min-height: 640px)');
    var slow = navigator.connection && (navigator.connection.saveData || /^(slow-)?2g$/.test(navigator.connection.effectiveType || ''));

    var GROW = 1.6;               /* how much bigger than itself the card gets on the way to the middle */
    var T_TRAVEL = 1000;          /* card leaves the row, crosses to the middle, grows */
    var T_SPIN = 1100;            /* spin, and open into the wide panel (and back) */
    var T_REST = 450;             /* beat between one card and the next */

    var on = false, geo = null, players = [], token = 0, inView = false;

    /* Every wait is tied to the run that started it, so cancelling is just
       bumping the token: anything still pending resolves false and unwinds. */
    function wait(ms, t) { return new Promise(function (res) { setTimeout(function () { res(t === token); }, ms); }); }
    function tween(ms, fn, t) {
      return new Promise(function (res) {
        var s = performance.now();
        (function f(now) {
          if (t !== token) return res(false);
          var k = clamp((now - s) / ms, 0, 1);
          fn(k);
          if (k < 1) requestAnimationFrame(f); else res(true);
        })(s);
      });
    }

    /* ---- clips ------------------------------------------------------------ */
    function Player(panel) {
      var segs = JSON.parse(panel.getAttribute('data-segs') || '[]');
      var v = panel.querySelector('video');
      var cap = panel.querySelector('.hw-cap');
      var dots = panel.querySelector('.hw-dots');
      var cur = '';
      segs.forEach(function () { dots.appendChild(document.createElement('i')); });

      function mark(k) {
        [].forEach.call(dots.children, function (d, j) { d.className = j === k ? 'on' : (j < k ? 'done' : ''); });
        cap.textContent = k >= 0 && segs[k] ? segs[k][3] : '';
      }
      function ready(t) {
        return new Promise(function (res) {
          if (v.readyState >= 1) return res();
          var to = setTimeout(res, 3000);
          v.addEventListener('loadedmetadata', function () { clearTimeout(to); res(); }, { once: true });
        });
      }
      function point(k) {
        var s = segs[k];
        if (cur !== s[0]) { cur = s[0]; v.src = base + cur + '.mp4'; v.load(); }
      }
      return {
        /* Start fetching the first clip before its panel opens. */
        warm: function () { if (!cur && segs.length) { point(0); } },
        /* Play every segment in turn; resolves true if it ran to the end. */
        run: async function (t) {
          for (var k = 0; k < segs.length; k++) {
            if (t !== token) return false;
            mark(k); point(k);
            await ready(t);
            if (t !== token) return false;
            try { v.currentTime = segs[k][1]; } catch (e) {}
            var p = v.play(); if (p && p.catch) p.catch(function () {});
            if (!(await wait((segs[k][2] - segs[k][1]) * 1000, t))) return false;
          }
          return true;
        },
        stop: function () { v.pause(); mark(-1); }
      };
    }

    /* ---- geometry --------------------------------------------------------- */
    function measure() {
      var st = stage.getBoundingClientRect();
      var sw = st.width, sh = stage.offsetHeight;
      var W = sw * 0.8;
      var H = Math.min(W * 2 / 3 * 0.625, sh, 560);
      var to = { l: (sw - W) / 2, t: (sh - H) / 2, w: W, h: H };
      var from = cards.map(function (c) {
        var r = c.getBoundingClientRect();
        return { l: r.left - st.left, t: r.top - st.top, w: r.width, h: r.height };
      });
      /* The middle stop: the card itself, much bigger, centred in the section. */
      var big = from.map(function (f) {
        var g = Math.min(GROW, (sh * 0.94) / f.h, (sw * 0.5) / f.w);
        var w = f.w * g, h = f.h * g;
        return { l: (sw - w) / 2, t: (sh - h) / 2, w: w, h: h };
      });
      geo = { to: to, from: from, big: big };
      stage.style.setProperty('--hw-w', W + 'px');
      stage.style.setProperty('--hw-h', H + 'px');
    }

    /* q1: 0 = in the row, 1 = in the middle, enlarged.  q2: 0 = still a card,
       1 = the wide panel.  Spinning goes with q2. */
    function draw(j, q1, q2) {
      var pn = panels[j], f = geo.from[j], A = geo.big[j], B = geo.to;
      var e1 = ease(q1), e2 = ease(q2), L, T, W, H;
      if (q2 > 0) {
        L = lerp(A.l, B.l, e2); T = lerp(A.t, B.t, e2); W = lerp(A.w, B.w, e2); H = lerp(A.h, B.h, e2);
      } else {
        L = lerp(f.l, A.l, e1); T = lerp(f.t, A.t, e1); W = lerp(f.w, A.w, e1); H = lerp(f.h, A.h, e1);
      }
      /* How the card turns into the panel. Spin (the default, and all How It
         Works uses) is the full 360. A panel can ask for another with
         data-turn — Tell the Boss gives each of its three a different one:
           zoom: no rotation; the card face blows up and fades as the panel
                 opens, a small swell at the midpoint
           flap: a flip-board turn about the horizontal axis — the card tips
                 away to edge-on, the panel comes up from edge-on to flat
         Each yields the transform plus how much of the card face (--out) and
         the open panel (--in) shows; run backwards it closes the same way. */
      var turn = pn.getAttribute('data-turn') || 'spin';
      var angle = 360 * e2, tf, out, inn;
      if (turn === 'zoom') {
        tf = 'perspective(1800px) scale(' + (1 + Math.sin(Math.PI * e2) * .06).toFixed(4) + ')';
        out = clamp(1 - e2 / .45, 0, 1);
        inn = clamp((e2 - .5) / .4, 0, 1);
      } else if (turn === 'flap') {
        var fa = 180 * e2, tilt = fa <= 90 ? fa : fa - 180;
        tf = 'perspective(1800px) rotateX(' + (-tilt).toFixed(1) + 'deg)';
        out = clamp((90 - fa) / 30, 0, 1);
        inn = clamp((fa - 90) / 30, 0, 1);
      } else {
        tf = 'perspective(1800px) rotateY(' + angle.toFixed(1) + 'deg)';
        /* Face of the card shows until the spin turns it edge-on; the split
           layout only once it has come round the other side. */
        out = clamp((90 - angle) / 40, 0, 1);
        inn = clamp((angle - 270) / 40, 0, 1);
      }
      pn.style.visibility = 'visible';
      pn.style.left = L + 'px';
      pn.style.top = T + 'px';
      pn.style.width = W + 'px';
      pn.style.height = H + 'px';
      pn.style.borderRadius = lerp(14, 24, Math.max(e1 * .5, e2)) + 'px';
      pn.style.transform = tf;
      pn.style.setProperty('--lift', e1.toFixed(3));
      pn.style.setProperty('--out', out.toFixed(3));
      pn.style.setProperty('--in', inn.toFixed(3));
      var face = pn.firstChild, s = Math.min(W / f.w, H / f.h);
      face.style.width = f.w + 'px';
      face.style.height = f.h + 'px';
      face.style.transform = 'translate(' + ((W - f.w * s) / 2) + 'px,' + ((H - f.h * s) / 2) + 'px) scale(' + s + ')';
      var a = Math.max(q1, q2);
      sec.style.setProperty('--a', a.toFixed(3));
      cards.forEach(function (c, m) {
        c.style.visibility = m === j && q1 > 0 ? 'hidden' : '';
        c.style.opacity = m === j ? '' : String(1 - a * 0.75);
      });
    }

    function rest() {
      sec.style.setProperty('--a', '0');
      panels.forEach(function (pn) { pn.style.visibility = 'hidden'; });
      cards.forEach(function (c) { c.style.visibility = ''; c.style.opacity = ''; });
      players.forEach(function (p) { p.stop(); });
    }

    /* ---- the show ---------------------------------------------------------- */
    async function show(t, first) {
      /* Round and round: 01, 02, 03, 01 ... until it is cancelled. */
      for (var j = first || 0; ; j = (j + 1) % cards.length) {
        if (t !== token) return;
        var nx = (j + 1) % cards.length;
        players[j].warm();
        players[nx].warm();
        if (!(await tween(T_TRAVEL, function (k) { draw(j, k, 0); }, t))) return;
        if (!(await tween(T_SPIN, function (k) { draw(j, 1, k); }, t))) return;
        if (!(await players[j].run(t))) return;
        players[j].stop();
        if (!(await tween(T_SPIN, function (k) { draw(j, 1, 1 - k); }, t))) return;
        if (!(await tween(T_TRAVEL, function (k) { draw(j, 1 - k, 0); }, t))) return;
        panels[j].style.visibility = 'hidden';
        cards[j].style.visibility = '';
        if (!(await wait(T_REST, t))) return;
      }
    }

    function begin(first) {
      var t = ++token;
      sec.__playing = true;
      show(t, first).then(function () { if (t === token) sec.__playing = false; });
    }

    function stop() { token++; rest(); }

    function sync() {
      if (!on || !geo) return;
      if (inView && !document.hidden) {
        if (!sec.__playing) begin(0);
      } else if (sec.__playing) {
        stop(); sec.__playing = false;
      }
    }

    /* A card that is clicked takes the stage now, and the round carries on from
       the card after it. */
    function pick(n) {
      if (!on || !geo) return;
      token++; rest();
      begin(n);
    }
    cards.forEach(function (c, n) {
      c.addEventListener('click', function () { pick(n); });
      c.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') { e.preventDefault(); pick(n); }
      });
    });

    /* ---- switching the show on and off -------------------------------------- */
    function enable() {
      if (on) return;
      on = true;
      sec.classList.add('hw-on');
      cards.forEach(function (c) { c.setAttribute('tabindex', '0'); c.setAttribute('role', 'button'); });
      if (!players.length) {
        players = panels.map(Player);
        /* The card's own words ride inside the panel and fade as it turns, so the
           card never appears to go blank on the way out. Whatever shape the card
           actually is (timeline card, points square, plain step) rides along —
           only .hw-face is added, nothing about the source card's own class is
           stripped. */
        panels.forEach(function (pn, n) {
          var face = cards[n].cloneNode(true);
          var shot = face.querySelector('.step-shot');
          if (shot) shot.parentNode.removeChild(shot);
          face.className = cards[n].className + ' hw-face';
          pn.insertBefore(face, pn.firstChild);
        });
      }
      measure();
    }
    function disable() {
      if (!on) return;
      stop();
      on = false; sec.__playing = false;
      sec.classList.remove('hw-on');
      cards.forEach(function (c) { c.removeAttribute('tabindex'); c.removeAttribute('role'); });
      panels.forEach(function (pn) { pn.removeAttribute('style'); });
    }
    function decide() {
      if (reduce || slow || !wide.matches) disable(); else { enable(); sync(); }
    }
    function relayout() {
      if (!on) return;
      /* Re-measure with everything at rest, and start over. */
      if (sec.__playing) { stop(); sec.__playing = false; }
      rest();
      measure();
      sync();
    }

    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (entries) {
        var e = entries[entries.length - 1];
        var was = inView;
        /* Enough of it on screen: over half the section, or over 60% of the window. */
        inView = e.isIntersecting && (e.intersectionRatio >= 0.55 || e.intersectionRect.height >= window.innerHeight * 0.6);
        if (!e.isIntersecting) inView = false;
        if (inView !== was) sync();
      }, { threshold: [0, .15, .3, .45, .55, .7, 1] }).observe(sec);
    }

    window.addEventListener('resize', function () { decide(); relayout(); });
    if (wide.addEventListener) wide.addEventListener('change', decide);
    document.addEventListener('visibilitychange', sync);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(relayout);
    window.addEventListener('load', relayout);
    decide();
  }

  sections.forEach(initHow);
})();
