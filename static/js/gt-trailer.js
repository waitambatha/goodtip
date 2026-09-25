/* The trailer page (/coming-soon/). Four jobs:
     1. kinetic headlines: split into words so they can arrive one at a time
     2. the hero, which plays itself: words are typed on the left, a hand
        presses play on the framed product at the right, the product grows to
        fill the whole hero and plays, then steps back and the next scene's
        words are typed. Prev/next, the step bar and the cards jump around it.
     3. the scene cards: two rows at a time, turning over in pages
     4. the join section's photographs
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
  /* The sport loader covers the page for its first moments; anything cinematic
     waits for it to leave (body.ready), with a ceiling in case it never does. */
  function whenReady(cb) {
    var b = document.body, done = false;
    function go() { if (done) return; done = true; if (mo) mo.disconnect(); cb(); }
    if (b.classList.contains('ready') || !b.classList.contains('loading')) return setTimeout(go, 150);
    var mo = window.MutationObserver ? new MutationObserver(function () { if (b.classList.contains('ready')) setTimeout(go, 200); }) : null;
    if (mo) mo.observe(b, { attributes: true, attributeFilter: ['class'] });
    setTimeout(go, 5000);
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

  /* ---- 2. the hero ---- */
  var hero = $('[data-hero]');
  var box = $('[data-stagebox]');
  var vids = $$('.tr-vid', box || document);
  var cards = $$('.tr-card');
  var cur = -1;

  if (hero && box && vids.length) {
    var photos = carousel($('[data-photos]', hero));
    var pane = $('[data-pane]', hero), copy = $('[data-copy]', hero), hand = $('[data-hand]', hero), badge = $('[data-play]', hero);
    var bignum = $('[data-bignum]', hero), steps = $$('[data-step]', hero);
    var capN = $('[data-cap-n]', hero), capT = $('[data-cap-t]', hero);
    var kEl = $('[data-k]', copy), kT = $('[data-k-t]', copy), hEl = $('[data-h]', copy), lEl = $('[data-l]', copy);
    var why = $('[data-why]', copy), whyH = $('[data-why-h]', copy), whyI = $$('[data-why-i]', copy), whyGo = $('[data-why-go]', copy);
    var N = vids.length;

    /* -- typed text. Every letter is laid out up front (hidden) so nothing
          reflows as it types; typing only switches them on. -- */
    var caret = document.createElement('span');
    caret.className = 'tr-caret'; caret.setAttribute('aria-hidden', 'true');

    // The words of an element, with the lime ones (<em>) marked.
    var segsOf = function (el) {
      var out = [];
      Array.prototype.slice.call(el.childNodes).forEach(function (n) {
        if (n.nodeType === 3) out.push({ t: n.textContent });
        else if (n.nodeType === 1) out.push({ t: n.textContent, hl: true });
      });
      return out;
    };
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
    // The big number is a green outline that fills with yellow, like water, in
    // about a second and a half at the start of each scene.
    var setNum = function (n) { bignum.textContent = pad(n + 1); bignum.dataset.n = pad(n + 1); };
    var waterId = 0;
    var level = function (p, w) {
      bignum.style.setProperty('--p', p);
      bignum.style.setProperty('--w', w || 0);
    };
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
      await sleep(900, id);
      copy.classList.remove('is-out');
      caret.remove();
    };

    // The intro: the promise, then why anyone should join.
    var intro = {
      k: segsOf(kT), h: segsOf(hEl), l: segsOf(lEl), whyH: segsOf(whyH),
      whyI: whyI.map(segsOf)
    };
    // Built the moment the script runs (hidden, letter by letter) so the full
    // sentence never flashes before it types.
    var introBs = stills ? [] : [
      [block(kT, intro.k, 0), 48], [block(hEl, intro.h, 1), 30], [block(lEl, intro.l, 2), 64],
      [block(whyH, intro.whyH, 3), 40]
    ].concat(whyI.map(function (li, i) { return [block(li, intro.whyI[i], 4 + i), 78]; }));
    var playIntro = async function (id) {
      var bs = introBs;
      for (var i = 0; i < bs.length; i++) {
        await typeBlock(bs[i][0], bs[i][1], id);
        await sleep(i === 2 ? 450 : 160, id);
      }
      whyGo.classList.add('on');
      await sleep(3000, id);
      await wipe(id);
      whyGo.classList.remove('on');
    };

    var showScene = async function (n, id, animateOut) {
      var v = vids[n];
      if (animateOut) await wipe(id);
      else { copy.classList.remove('is-out'); caret.remove(); }
      cur = n;
      photos.show(n); markCards(n); markSteps(n);
      // While the box is still shrinking the last clip is on show; give it its moment.
      if (animateOut) pick(n); else setTimeout(function () { if (id === run) pick(n); }, 900);
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

    /* -- the hand: in from the corner, onto the play badge, a press -- */
    var pointAt = function () {
      var pr = pane.getBoundingClientRect(), br = badge.getBoundingClientRect();
      var x = br.left - pr.left + br.width / 2, y = br.top - pr.top + br.height / 2;
      hand.style.transition = 'none';
      hand.classList.remove('is-in', 'is-press');
      hand.style.setProperty('--hx', (pr.width * 0.97) + 'px');
      hand.style.setProperty('--hy', (pr.height * 1.02) + 'px');
      void hand.offsetWidth;
      hand.style.transition = '';
      hand.style.setProperty('--hx', x + 'px');
      hand.style.setProperty('--hy', y + 'px');
      hand.classList.add('is-in');
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

    var playScene = async function (n, id) {
      var v = vids[n];
      await sleep(400, id);
      pointAt();
      await sleep(1250, id);
      hand.classList.add('is-press'); box.classList.add('is-tap');
      await sleep(300, id);
      // The press: the product fills the hero and starts to play.
      hero.classList.add('is-theatre'); document.body.classList.add('tr-on-air');
      box.classList.remove('is-framed', 'is-tap');
      hand.classList.remove('is-in');
      await playUntilEnd(v, id);
      if (steps[n]) steps[n].style.setProperty('--p', 1);
      fillNow();
      // Done: step back so the next scene's words can be typed.
      hero.classList.remove('is-theatre'); document.body.classList.remove('tr-on-air');
      box.classList.add('is-framed');
    };

    var flow = async function (id, n, animateOut, withIntro) {
      try {
        if (withIntro) { await playIntro(id); animateOut = false; n = 0; }
        for (;;) {
          await showScene(n, id, animateOut);
          await playScene(n, id);
          n = (n + 1) % N; animateOut = false;
        }
      } catch (e) { if (e !== CANCEL) throw e; }
    };

    // Jump: cancel whatever is running and begin at scene n. From full size it
    // steps back first; from the framed view the words are wiped where they stand.
    var go = function (n) {
      n = ((n % N) + N) % N;
      var id = ++run, wasFull = hero.classList.contains('is-theatre');
      hand.classList.remove('is-in', 'is-press'); box.classList.remove('is-tap');
      vids.forEach(stop); playing = false;
      hero.classList.remove('is-theatre'); document.body.classList.remove('tr-on-air'); box.classList.add('is-framed');
      if (stills) return plain(n);
      flow(id, n, !wasFull, false);
    };

    // Reduced motion and data saver: the same story, still and complete.
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
    // Before the first scene (the intro) "next" means scene one, "back" means scene one too.
    if (prev) prev.addEventListener('click', function () { go(cur < 0 ? 0 : cur - 1); });
    if (next) next.addEventListener('click', function () { go(cur + 1); });
    steps.forEach(function (st, i) { st.addEventListener('click', function () { go(i); }); });

    var applyPause = function () {
      paused = userPaused || offscreen || document.hidden || peekOpen;
      hero.classList.toggle('is-paused', userPaused);
      if (active && playing) { if (paused) active.pause(); else play(active); }
    };
    if (pauseBtn) pauseBtn.addEventListener('click', function () {
      userPaused = !userPaused;
      pauseBtn.setAttribute('aria-pressed', userPaused ? 'true' : 'false');
      pauseBtn.setAttribute('aria-label', userPaused ? 'Play the trailer' : 'Pause the trailer');
      applyPause();
    });
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (es) { offscreen = !es[0].isIntersecting; applyPause(); }, { threshold: 0.15 }).observe(hero);
    }
    document.addEventListener('visibilitychange', applyPause);
    // A showcase over the hero holds the hero still until it is closed.
    document.addEventListener('tr:peek', function (e) { peekOpen = e.detail.open; applyPause(); });

    // Let the loader leave and the cinema bars part, then roll.
    if (stills) { hand.hidden = true; markSteps(-1); }
    else {
      // Ready-made: the intro's words are built now (hidden) so nothing flashes.
      whenReady(function () { setTimeout(function () { flow(++run, 0, false, true); }, 500); });
    }

    /* ---- 3. the scene cards ----
       One big card and seven small ones. The scene in the big place changes
       every few seconds: the next card grows into it, the one that was there
       leaves, a new one joins at the end. Cards glide between their places
       (FLIP), so the shift reads as one movement rather than a redraw. */
    var grid = $('[data-grid]');
    if (grid && cards.length) {
      var HOLD = 3000, SLOTS = 8;
      var dotsBox = $('[data-dots]');
      var feat = 0, tick, hovered = false, inView = false, previewing = null;
      var total = cards.length;
      var slotsNow = function () { return window.innerWidth > 1000 ? 8 : 5; };

      // The last word of every title is lime on the big card.
      cards.forEach(function (c) {
        var b = $('b', c), m = /^(.*?)(\S+)$/.exec(b.textContent.trim());
        if (m) { b.textContent = m[1]; var em = document.createElement('em'); em.textContent = m[2]; b.appendChild(em); }
      });

      var seen = function (c) { return c.getClientRects().length > 0; };
      var slotOf = function (i) { return (i - feat + total) % total; };

      var layout = function (animate) {
        var before = new Map();
        if (animate) cards.forEach(function (c) { if (seen(c)) before.set(c, c.getBoundingClientRect()); });
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
          if (!seen(c) || c.classList.contains('is-leaving')) return;
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
          ], { duration: 780, easing: 'cubic-bezier(.65,0,.25,1)' });
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

      // A muted preview plays in the big card (a still where data is dear).
      var preview = function () {
        if (previewing) { previewing.remove(); previewing = null; }
        if (stills || !window.matchMedia('(pointer: fine)').matches) return;
        var c = cards[feat], media = $('.tr-card-media', c);
        var pv = document.createElement('video');
        pv.muted = true; pv.loop = true; pv.playsInline = true; pv.setAttribute('aria-hidden', 'true');
        pv.src = c.dataset.tsrc; media.appendChild(pv); previewing = pv;
        var p = pv.play(); if (p && p.catch) p.catch(function () {});
      };

      var restart = function () {
        clearInterval(tick);
        if (stills) return;
        tick = setInterval(function () { if (inView && !hovered && !document.hidden) feature(feat + 1, true); }, HOLD);
      };
      var feature = function (n, auto) {
        feat = ((n % total) + total) % total;
        layout(true);
        preview();
        if (!auto) restart();
      };

      SLOTS = slotsNow();
      buildDots(); layout(false);
      window.addEventListener('resize', function () { var s = slotsNow(); if (s !== SLOTS) { SLOTS = s; layout(false); } });

      $('[data-cprev]').addEventListener('click', function () { feature(feat - 1); });
      $('[data-cnext]').addEventListener('click', function () { feature(feat + 1); });
      grid.addEventListener('mouseenter', function () { hovered = true; });
      grid.addEventListener('mouseleave', function () { hovered = false; });
      grid.addEventListener('focusin', function () { hovered = true; });
      grid.addEventListener('focusout', function () { hovered = false; });

      // The story starts when the section is on screen, so scene one gets its full three seconds.
      if ('IntersectionObserver' in window) {
        new IntersectionObserver(function (es) {
          var was = inView; inView = es[0].isIntersecting;
          if (inView && !was) { restart(); preview(); }
        }, { threshold: 0.35 }).observe(grid);
      } else inView = true;
      restart();

      // The big card opens its scene in the hero; any other card takes the big place first.
      cards.forEach(function (c, i) {
        c.addEventListener('click', function () {
          if (i !== feat) { feature(i); return; }
          go(i);
          hero.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' });
        });
      });
    }
  }

  /* ---- 4. the join section's photographs ---- */
  var joinBox = $('[data-photos-join]');
  if (joinBox && !stills) {
    var joinPhotos = carousel(joinBox), joinVisible = false;
    if ('IntersectionObserver' in window) new IntersectionObserver(function (es) { joinVisible = es[0].isIntersecting; }, { threshold: 0.1 }).observe(joinBox);
    setInterval(function () { if (joinVisible && !document.hidden) joinPhotos.next(); }, 5200);
  }

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
      if (nav && nav.getAttribute('data-open') === 'true') nav.setAttribute('data-open', 'false');
      peek.focus({ preventScroll: true });
    };
    var hide = function () {
      if (!openPeek) return;
      openPeek.hidden = true; setVideo(openPeek, false);
      openPeek = null; pushed = false; lockAir(false);
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

    // From inside a showcase, every link goes to the next place without piling up history.
    document.addEventListener('click', function (e) {
      var a = e.target.closest && e.target.closest('a[href^="#"]');
      if (!a) return;
      var slug = a.getAttribute('href').slice(1);
      if (openPeek && a.closest('[data-peek]') && find(slug)) {
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
