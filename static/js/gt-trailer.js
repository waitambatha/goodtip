/* The trailer page (/coming-soon/). Nothing on it moves until the visitor
   asks (client, 20 Oct 2026: "too much happening and moving all at once...
   give people things to press and interact with at their leisure"):
     1. kinetic headlines: the join heading's words arrive once, on scroll
     2. the hero: still and complete on arrival; press play (or a scene
        marker, or a scene card) and the trailer plays from there to the end,
        then comes to rest again
     3. the scene cards: pick one and it takes the big place; its clip plays
        while the pointer rests on it
     4. the join section: one photograph, still
     5. the showcases: every menu and footer item opens a filmed tour of its
        page over the trailer (address hash), instead of leaving it
   Video is loaded sparingly (the current clip and the next), paused when the
   hero is off screen or the tab is hidden, and never played on data saver or
   a 2G link, where each clip stays as its poster frame.
   Videos carry data-tsrc, never src or data-src: gt-video.js owns data-src. */
(function () {
  'use strict';
  var root = document.documentElement;
  root.classList.add('tr-js');

  var reduced = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var conn = navigator.connection || {};
  // Data saver and slow links get the stills. Reduced motion does not: the hero is
  // words appearing and clips playing (content, not travel), and the global
  // reduced-motion rule already removes every slide, grow and fade from it.
  var stills = conn.saveData === true || /(^|-)2g$/.test(conn.effectiveType || '');

  function $(s, c) { return (c || document).querySelector(s); }
  function $$(s, c) { return Array.prototype.slice.call((c || document).querySelectorAll(s)); }
  function pad(n) { return (n < 10 ? '0' : '') + n; }

  /* ---- 1. kinetic words ---- */
  function splitWords(el) {
    var i = 0;
    (function walk(node) {
      Array.prototype.slice.call(node.childNodes).forEach(function (n) {
        if (n.nodeType === 3) {
          var frag = document.createDocumentFragment();
          n.textContent.split(/(\s+)/).forEach(function (part) {
            if (!part) return;
            if (/^\s+$/.test(part)) { frag.appendChild(document.createTextNode(' ')); return; }
            var w = document.createElement('span'), inner = document.createElement('span');
            w.className = 'tr-w'; inner.textContent = part; inner.style.setProperty('--i', i++);
            w.appendChild(inner); frag.appendChild(w);
          });
          node.replaceChild(frag, n);
        } else if (n.nodeType === 1) { walk(n); }
      });
    })(el);
  }
  var seen = 'IntersectionObserver' in window ? new IntersectionObserver(function (es, obs) {
    es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add('tr-in'); obs.unobserve(e.target); } });
  }, { threshold: 0.35 }) : null;
  $$('[data-kinetic]').forEach(function (el) {
    splitWords(el);
    if (seen) seen.observe(el); else el.classList.add('tr-in');
  });

  /* ---- video helpers ---- */
  function load(v) {
    if (stills || !v || v.getAttribute('src') || !v.dataset.tsrc) return;
    v.src = v.dataset.tsrc;
  }
  function play(v) {
    if (stills || !v) return;
    load(v);
    var p = v.play(); if (p && p.catch) p.catch(function () {});
  }
  function stop(v) { if (v && !v.paused) v.pause(); }

  /* ---- shared: a photograph carousel ---- */
  function carousel(container) {
    var shots = $$('.tr-shot', container), on = 0;
    return {
      count: shots.length,
      show: function (n) {
        n = ((n % shots.length) + shots.length) % shots.length;
        if (n === on) return;
        on = n;
        shots.forEach(function (s, i) { s.classList.toggle('is-on', i === n); });
      },
      next: function () { this.show(on + 1); }
    };
  }

  /* ---- 2. the hero ----
     STILL UNTIL ASKED (client, 20 Oct 2026, passing on what visitors said:
     "there's too much happening and moving all at once... give people things
     to press and interact with at their leisure"). The hero used to play
     itself: it typed its own words, turned its photographs, sent a hand in to
     press play and looped fourteen scenes forever. Now it arrives complete and
     still — the promise, the reasons to join, one photograph, and the product
     framed with a play button — and nothing moves until the visitor presses
     play, a scene marker, or a scene card. Then it plays the trailer as a
     trailer, from that scene to the last, and comes to rest again at the end
     rather than starting over. */
  var hero = $('[data-hero]');
  var box = $('[data-stagebox]');
  var vids = $$('.tr-vid', box || document);
  var cards = $$('.tr-card');
  var cur = -1;
  var go = function () {};

  if (hero && box && vids.length) {
    var photos = carousel($('[data-photos]', hero));
    var copy = $('[data-copy]', hero);
    var bignum = $('[data-bignum]', hero), steps = $$('[data-step]', hero);
    var capN = $('[data-cap-n]', hero), capT = $('[data-cap-t]', hero);
    var kT = $('[data-k-t]', copy), hEl = $('[data-h]', copy), lEl = $('[data-l]', copy);
    var why = $('[data-why]', copy), whyGo = $('[data-why-go]', copy);
    var N = vids.length;
    // The intro exactly as written, so coming to rest after the trailer puts
    // the page back the way the visitor first found it.
    var original = { k: kT.innerHTML, h: hEl.innerHTML, l: lEl.innerHTML };

    /* -- typed text, during the trailer only. Every letter is laid out up
          front (hidden) so nothing reflows as it types. -- */
    var caret = document.createElement('span');
    caret.className = 'tr-caret'; caret.setAttribute('aria-hidden', 'true');
    var block = function (el, segs, b) {
      var chars = [], w = 0;
      el.textContent = '';
      el.classList.remove('is-typing');
      el.classList.add('tr-built');
      el.style.setProperty('--b', b || 0);
      segs.forEach(function (seg) {
        seg.t.split(/(\s+)/).forEach(function (part) {
          if (!part) return;
          if (/^\s+$/.test(part)) { el.appendChild(document.createTextNode(' ')); return; }
          var word = document.createElement('span');
          word.className = 'tr-tw' + (seg.hl ? ' hl' : '');
          word.style.setProperty('--w', w++);
          for (var i = 0; i < part.length; i++) {
            var c = document.createElement('span');
            c.className = 'tr-c'; c.textContent = part.charAt(i);
            word.appendChild(c); chars.push(c);
          }
          el.appendChild(word);
        });
      });
      return { el: el, chars: chars };
    };

    /* -- a clock that holds still while the trailer is paused, and gives up
          the moment a newer run has started (run bumps on every jump) -- */
    var CANCEL = {}, run = 0, paused = false, userPaused = false, offscreen = false, peekOpen = false;
    var frames = function (id, fn) {
      return new Promise(function (res, rej) {
        var last = null;
        (function tick(now) {
          if (id !== run) return rej(CANCEL);
          var dt = last === null ? 0 : Math.min(now - last, 100);
          last = now;
          if (!paused && fn(dt) === true) return res();
          requestAnimationFrame(tick);
        })(performance.now());
      });
    };
    var sleep = function (ms, id) { var left = ms; return frames(id, function (dt) { left -= dt; return left <= 0; }); };
    var typeBlock = function (b, cps, id) {
      var t = 0, n = 0, total = b.chars.length;
      b.el.classList.add('is-typing');
      if (b.el.firstChild) b.el.insertBefore(caret, b.el.firstChild);
      return frames(id, function (dt) {
        t += dt;
        var want = Math.min(total, Math.floor(t * cps / 1000));
        while (n < want) { b.chars[n].classList.add('on'); n++; }
        if (n > 0 && n <= total) b.chars[n - 1].parentNode.appendChild(caret);
        return n >= total;
      });
    };

    /* -- the pieces of a scene -- */
    // The big number is a green outline that fills with yellow, like water, at
    // the start of each scene.
    var setNum = function (n) { bignum.textContent = pad(n + 1); bignum.dataset.n = pad(n + 1); };
    var waterId = 0;
    var level = function (p, w) { bignum.style.setProperty('--p', p); bignum.style.setProperty('--w', w || 0); };
    var fillUp = function (ms) {
      var id = ++waterId, t0 = performance.now();
      level(0, 0);
      var tick = function (now) {
        if (id !== waterId) return;
        var t = now - t0, k = Math.min(1, t / ms);
        level(1 - Math.pow(1 - k, 2.2), t / 520);
        if (k < 1) requestAnimationFrame(tick); else level(1, 0);
      };
      requestAnimationFrame(tick);
    };
    var fillNow = function () { waterId++; level(1, 0); };
    var markCards = function (n) { cards.forEach(function (c, i) { c.classList.toggle('is-now', i === n); }); };
    var markSteps = function (n) {
      steps.forEach(function (st, i) {
        st.classList.toggle('is-now', i === n);
        st.style.setProperty('--p', i < n ? 1 : 0);
      });
    };
    var pick = function (n) {
      var v = vids[n];
      vids.forEach(function (o) { if (o !== v) { o.classList.remove('is-on'); stop(o); } });
      v.classList.add('is-on');
      try { v.currentTime = 0; } catch (e) {}
      if (!stills) { v.preload = 'auto'; load(v); }
    };
    var wipe = async function (id) {
      copy.classList.add('is-out');
      await sleep(700, id);
      copy.classList.remove('is-out');
      caret.remove();
    };

    var showScene = async function (n, id, animateOut) {
      var v = vids[n];
      if (animateOut) await wipe(id);
      else { copy.classList.remove('is-out'); caret.remove(); }
      cur = n;
      photos.show(n); markCards(n); markSteps(n);
      pick(n);
      if (capN) capN.textContent = pad(n + 1);
      if (capT) capT.textContent = v.dataset.title;
      copy.classList.add('is-scene');
      why.hidden = true;
      setNum(n); level(0, 0);
      bignum.classList.remove('on');
      void bignum.offsetWidth;
      bignum.classList.add('on');
      fillUp(1500);
      var bs = [
        [block(kT, [{ t: pad(n + 1) + ' / ' + pad(N) + ' · ' + v.dataset.act }], 0), 60],
        [block(hEl, [{ t: v.dataset.title }], 1), 34],
        [block(lEl, [{ t: v.dataset.blurb }], 2), 70]
      ];
      for (var i = 0; i < bs.length; i++) {
        await typeBlock(bs[i][0], bs[i][1], id);
        await sleep(i === 0 ? 120 : 200, id);
      }
    };

    var active = null, playing = false;
    var playUntilEnd = function (v, id) {
      active = v; playing = true;
      play(v);
      var last = -1, stall = 0;
      return frames(id, function (dt) {
        if (v.ended || v.error) return true;
        if (v.duration && steps[cur]) steps[cur].style.setProperty('--p', Math.min(1, v.currentTime / v.duration));
        if (v.currentTime > last + 0.01) { last = v.currentTime; stall = 0; } else stall += dt;
        return stall > 8000;   // a browser that refuses to play must not freeze the story
      }).then(function () { playing = false; }, function (e) { playing = false; throw e; });
    };

    // The visitor pressed play, so no hand presses it for them: the product
    // simply fills the hero and plays.
    var playScene = async function (n, id) {
      await sleep(350, id);
      hero.classList.add('is-theatre'); document.body.classList.add('tr-on-air');
      box.classList.remove('is-framed');
      await playUntilEnd(vids[n], id);
      if (steps[n]) steps[n].style.setProperty('--p', 1);
      fillNow();
      hero.classList.remove('is-theatre'); document.body.classList.remove('tr-on-air');
      box.classList.add('is-framed');
    };

    var setPauseBtn = function () {
      if (!pauseBtn) return;
      var showPlay = userPaused || hero.classList.contains('is-idle');
      pauseBtn.setAttribute('aria-pressed', showPlay ? 'true' : 'false');
      pauseBtn.setAttribute('aria-label', showPlay ? 'Play the trailer' : 'Pause the trailer');
    };

    // At rest: the page as the visitor first found it. Nothing moving.
    var rest = function () {
      run++;
      vids.forEach(stop); playing = false; active = null;
      userPaused = false; paused = false;
      hero.classList.remove('is-theatre', 'is-paused'); document.body.classList.remove('tr-on-air');
      box.classList.add('is-framed');
      hero.classList.add('is-idle');
      copy.classList.remove('is-scene', 'is-out');
      caret.remove();
      [[kT, original.k], [hEl, original.h], [lEl, original.l]].forEach(function (p) {
        p[0].innerHTML = p[1]; p[0].classList.remove('tr-built', 'is-typing');
      });
      why.hidden = false;
      if (whyGo) whyGo.classList.add('on');
      bignum.classList.remove('on');
      // Back to the cover frame, not wherever the last clip happened to stop.
      vids.forEach(function (o, i) {
        o.classList.toggle('is-on', i === 0);
        if (o.getAttribute('src')) { o.removeAttribute('src'); o.load(); }
      });
      photos.show(0); markCards(-1); markSteps(-1);
      cur = -1;
      setPauseBtn();
    };

    var flow = async function (id, n, animateOut) {
      try {
        for (; n < N; n++) {
          await showScene(n, id, animateOut);
          await playScene(n, id);
          animateOut = false;
        }
        rest();
      } catch (e) { if (e !== CANCEL) throw e; }
    };

    // Play from scene n: cancel whatever is running and begin there.
    go = function (n) {
      n = ((n % N) + N) % N;
      var id = ++run, fromRest = hero.classList.contains('is-idle');
      vids.forEach(stop); playing = false;
      hero.classList.remove('is-theatre', 'is-idle'); document.body.classList.remove('tr-on-air'); box.classList.add('is-framed');
      userPaused = false; applyPause();
      setPauseBtn();
      if (stills) return plain(n);
      flow(id, n, !fromRest);
    };

    // Data saver and slow links: the same story, as still frames.
    var plain = function (n) {
      cur = n;
      var v = vids[n];
      why.hidden = true; copy.classList.add('is-scene');
      kT.textContent = pad(n + 1) + ' / ' + pad(N) + ' · ' + v.dataset.act;
      hEl.textContent = v.dataset.title; lEl.textContent = v.dataset.blurb;
      setNum(n); fillNow(); bignum.classList.add('on');
      if (capN) capN.textContent = pad(n + 1);
      if (capT) capT.textContent = v.dataset.title;
      vids.forEach(function (o) { o.classList.toggle('is-on', o === v); });
      photos.show(n); markCards(n); markSteps(n);
    };

    var prev = $('[data-prev]', hero), next = $('[data-next]', hero), pauseBtn = $('[data-pause]', hero);
    if (prev) prev.addEventListener('click', function () { go(cur <= 0 ? 0 : cur - 1); });
    if (next) next.addEventListener('click', function () { go(cur + 1); });
    steps.forEach(function (st, i) { st.addEventListener('click', function () { go(i); }); });

    var applyPause = function () {
      paused = userPaused || offscreen || document.hidden || peekOpen;
      hero.classList.toggle('is-paused', userPaused);
      if (active && playing) { if (paused) active.pause(); else play(active); }
    };
    var togglePause = function () {
      if (hero.classList.contains('is-idle')) { go(0); return; }
      userPaused = !userPaused;
      applyPause(); setPauseBtn();
    };
    if (pauseBtn) pauseBtn.addEventListener('click', togglePause);
    // The framed product is the play button: press it to start, and while the
    // trailer fills the hero, press it again to pause.
    box.addEventListener('click', togglePause);
    box.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); togglePause(); }
    });
    var stopBtn = $('[data-stop]', hero);
    if (stopBtn) stopBtn.addEventListener('click', rest);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !hero.classList.contains('is-idle') && !peekOpen) rest();
    });
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (es) { offscreen = !es[0].isIntersecting; applyPause(); }, { threshold: 0.15 }).observe(hero);
    }
    document.addEventListener('visibilitychange', applyPause);
    // A showcase over the hero holds the hero still until it is closed.
    document.addEventListener('tr:peek', function (e) { peekOpen = e.detail.open; applyPause(); });

    rest();
  }

  /* ---- 3. the scene cards ----
     One big card and seven small ones. They used to turn over on their own
     every three seconds; now they wait (client, 20 Oct 2026). Pick a card and
     it glides into the big place (FLIP, so it reads as one movement); the big
     card's clip plays only while the pointer rests on it; and pressing the
     big card plays its scene in the hero. */
  var grid = $('[data-grid]');
  if (grid && cards.length) {
    var SLOTS = 8;
    var dotsBox = $('[data-dots]');
    var feat = 0, previewing = null;
    var total = cards.length;
    var slotsNow = function () { return window.innerWidth > 1000 ? 8 : 5; };

    // The last word of every title is lime on the big card.
    cards.forEach(function (c) {
      var b = $('b', c), m = /^(.*?)(\S+)$/.exec(b.textContent.trim());
      if (m) { b.textContent = m[1]; var em = document.createElement('em'); em.textContent = m[2]; b.appendChild(em); }
    });

    var visible = function (c) { return c.getClientRects().length > 0; };
    var slotOf = function (i) { return (i - feat + total) % total; };

    var layout = function (animate) {
      var before = new Map();
      if (animate) cards.forEach(function (c) { if (visible(c)) before.set(c, c.getBoundingClientRect()); });
      var gr = grid.getBoundingClientRect();
      var leaving = [];
      cards.forEach(function (c, i) {
        var s = slotOf(i), was = c.dataset.slot;
        c.dataset.slot = s < SLOTS ? String(s) : 'x';
        c.classList.toggle('is-big', s === 0);
        c.setAttribute('aria-current', s === 0 ? 'true' : 'false');
        if (animate && was !== 'x' && was !== undefined && c.dataset.slot === 'x' && before.has(c)) leaving.push(c);
      });
      markDots();
      if (!animate) return;
      // Whoever left is held where it stood and fades out over the top.
      leaving.forEach(function (c) {
        var r = before.get(c);
        c.classList.add('is-leaving');
        c.style.display = 'flex';
        c.style.left = (r.left - gr.left) + 'px'; c.style.top = (r.top - gr.top) + 'px';
        c.style.width = r.width + 'px'; c.style.height = r.height + 'px';
        var a = c.animate([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateX(-28px) scale(.94)' }], { duration: 420, easing: 'ease-in' });
        a.onfinish = function () { c.classList.remove('is-leaving'); c.style.cssText = ''; };
      });
      cards.forEach(function (c) {
        if (!visible(c) || c.classList.contains('is-leaving')) return;
        var r0 = before.get(c), r1 = c.getBoundingClientRect();
        if (!r0) {
          c.animate([{ opacity: 0, transform: 'translateY(30px) scale(.96)' }, { opacity: 1, transform: 'none' }], { duration: 600, easing: 'cubic-bezier(.2,.7,.2,1)', delay: 200, fill: 'backwards' });
          return;
        }
        var dx = r0.left - r1.left, dy = r0.top - r1.top, sx = r0.width / r1.width, sy = r0.height / r1.height;
        if (Math.abs(dx) + Math.abs(dy) + Math.abs(1 - sx) + Math.abs(1 - sy) < 2) return;
        c.style.transformOrigin = '0 0';
        c.style.zIndex = c.classList.contains('is-big') ? 2 : '';
        var a = c.animate([
          { transform: 'translate(' + dx + 'px,' + dy + 'px) scale(' + sx + ',' + sy + ')' },
          { transform: 'none' }
        ], { duration: 680, easing: 'cubic-bezier(.65,0,.25,1)' });
        a.onfinish = function () { c.style.transformOrigin = ''; c.style.zIndex = ''; };
      });
    };

    var buildDots = function () {
      dotsBox.innerHTML = '';
      cards.forEach(function (_, i) {
        var b = document.createElement('button');
        b.type = 'button'; b.setAttribute('aria-label', 'Scene ' + (i + 1));
        b.addEventListener('click', function () { feature(i); });
        dotsBox.appendChild(b);
      });
    };
    var markDots = function () { $$('button', dotsBox).forEach(function (b, i) { b.classList.toggle('is-on', i === feat); }); };

    // The big card's clip, while the pointer rests on it — and only then.
    var stopPreview = function () {
      if (previewing) { previewing.remove(); previewing = null; }
      cards.forEach(function (c) { c.classList.remove('is-previewing'); });
    };
    var startPreview = function () {
      stopPreview();
      if (stills) return;
      var c = cards[feat], media = $('.tr-card-media', c);
      var pv = document.createElement('video');
      pv.muted = true; pv.loop = true; pv.playsInline = true; pv.setAttribute('aria-hidden', 'true');
      pv.src = c.dataset.tsrc; media.appendChild(pv); previewing = pv;
      c.classList.add('is-previewing');
      var p = pv.play(); if (p && p.catch) p.catch(function () {});
    };

    var feature = function (n) {
      stopPreview();
      feat = ((n % total) + total) % total;
      layout(true);
    };

    SLOTS = slotsNow();
    buildDots(); layout(false);
    window.addEventListener('resize', function () { var s = slotsNow(); if (s !== SLOTS) { SLOTS = s; layout(false); } });

    $('[data-cprev]').addEventListener('click', function () { feature(feat - 1); });
    $('[data-cnext]').addEventListener('click', function () { feature(feat + 1); });

    cards.forEach(function (c, i) {
      c.addEventListener('mouseenter', function () { if (i === feat) startPreview(); });
      c.addEventListener('mouseleave', function () { if (i === feat) stopPreview(); });
      // The big card plays its scene in the hero; any other card takes the big place first.
      c.addEventListener('click', function () {
        if (i !== feat) { feature(i); return; }
        stopPreview();
        go(i);
        hero.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' });
      });
    });
  }

  /* ---- 4. the join section ----
     One photograph, standing still. It used to turn every five seconds. */

  /* ---- 5. the showcases ----
     The hash is the state: #how-it-works opens that page's tour, anything
     else closes it. So a link to one works, and Back closes it. Moving from
     one showcase to another replaces the history entry instead of adding one,
     so Close is always one step back to the trailer. */
  var peeks = $$('[data-peek]');
  if (peeks.length) {
    var nav = $('.nav'), openPeek = null, opener = null, pushed = false;
    var slugOf = function () { return decodeURIComponent((location.hash || '').slice(1)); };
    var find = function (slug) { return peeks.filter(function (p) { return p.dataset.peek === slug; })[0] || null; };
    // The bar (and the programme behind its Menu) is the one menu inside a
    // tour too, so it is what says which tour is open.
    var markHere = function (slug) {
      $$('.hm-bar .nav-links a, [data-hm-item]').forEach(function (a) {
        var here = !!slug && a.getAttribute('href') === '#' + slug;
        a.classList.toggle('is-current', here);
        if (here) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
      });
    };
    var lockAir = function (on) {
      root.classList.toggle('tr-peek-lock', on);
      document.body.classList.toggle('tr-peek-open', on);
      document.dispatchEvent(new CustomEvent('tr:peek', { detail: { open: on } }));
    };
    var setVideo = function (peek, on) {
      var v = $('video', peek);
      if (!v) return;
      if (!on) { v.pause(); return; }
      if (!v.getAttribute('src')) v.src = v.dataset.psrc;
      if (stills || reduced) { v.controls = true; return; }
      var pr = v.play(); if (pr && pr.catch) pr.catch(function () {});
    };
    var show = function (peek) {
      if (openPeek === peek) return;
      if (openPeek) { openPeek.hidden = true; setVideo(openPeek, false); }
      else opener = document.activeElement;
      openPeek = peek; peek.hidden = false; peek.scrollTop = 0;
      // Play again from the top, and give the copy its small arrival.
      peek.classList.remove('is-in'); void peek.offsetWidth; peek.classList.add('is-in');
      lockAir(true); setVideo(peek, true);
      markHere(peek.dataset.peek);
      if (nav && nav.getAttribute('data-open') === 'true') nav.setAttribute('data-open', 'false');
      peek.focus({ preventScroll: true });
    };
    var hide = function () {
      if (!openPeek) return;
      openPeek.hidden = true; setVideo(openPeek, false);
      openPeek = null; pushed = false; lockAir(false); markHere(null);
      if (opener && opener.focus && document.contains(opener)) opener.focus({ preventScroll: true });
      opener = null;
    };
    var sync = function () {
      var p = find(slugOf());
      if (p) show(p); else hide();
      if (!p && slugOf() === 'lock-in') {
        var j = document.getElementById('lock-in');
        if (j) j.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' });
      }
    };
    peeks.forEach(function (p) { p.tabIndex = -1; });
    window.addEventListener('hashchange', sync);

    // With a showcase open, any link to another one (in it, the bar or the
    // programme) goes there without piling up history.
    document.addEventListener('click', function (e) {
      var a = e.target.closest && e.target.closest('a[href^="#"]');
      if (!a) return;
      var slug = a.getAttribute('href').slice(1);
      if (openPeek && find(slug)) {
        e.preventDefault();
        history.replaceState(history.state, '', '#' + slug); sync();
      } else if (!openPeek && find(slug)) pushed = true;
      else if (openPeek && a.hasAttribute('data-peek-join')) {
        e.preventDefault();
        history.replaceState(history.state, '', '#join'); sync();
        window.dispatchEvent(new Event('wl:hash'));
      }
    });
    var close = function () {
      if (!openPeek) return;
      if (pushed) { pushed = false; history.back(); return; }
      history.replaceState(history.state, '', location.pathname + location.search); sync();
    };
    peeks.forEach(function (p) {
      $$('[data-peek-close]', p).forEach(function (b) { b.addEventListener('click', close); });
      // A click on the dark margin closes it too.
      p.addEventListener('click', function (e) { if (e.target === p || e.target.classList.contains('tr-peek-in')) close(); });
    });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
    sync();
  }
})();
