/* The legal pages (Terms, Privacy): the short version, and the clause cards.

   2 OCT 2026 (client), second pass — "that should be a button, not a drop
   down … all cards the same size, then it expands and we see full details".

   THE SHORT VERSION is a button. Pressing it brings the summary up in its own
   window, and every time it comes up it is the next of five colours. Close it
   with the ×, the backdrop or Escape. Without script it is printed open.

   THE CLAUSES are cards of one size, four to a row, showing their titles. A
   card EXPANDS — out of its own place, over the grid — to show the clause in
   full, then shrinks back into its place. Three ways that happens:
     1. on its own: while the cards are on screen they expand one after
        another, each a different way, staying open about as long as the
        clause takes to read (hovering the open card holds it);
     2. press a card: it expands and stays open until you close it (×), then
        the rest carry on by themselves;
     3. the menu: hovering an entry expands that card while you are over it;
        clicking it keeps it open, like pressing the card.
   The grid keeps the height of the longest clause, so a card opening or
   closing never moves anything else on the page.

   The articles stay in the page, whole — they are what search engines, the
   Pages editor and no-script visitors read; the open card shows a copy.
   Reduced motion (unless ?motion=1 set html.gt-motion) keeps all of it and
   drops only the movement. */
(function () {
  'use strict';

  var root = document.documentElement;
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches &&
    !root.classList.contains('gt-motion');
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function frame() { return new Promise(function (r) { requestAnimationFrame(function () { requestAnimationFrame(r); }); }); }

  /* ---- the short version: a button that swaps it in --------------------
     2 Oct 2026 (client): pressing it, the menu and the cards make way and the
     short version is shown THERE, in their place — not floating anywhere
     else. Each time it is shown it is the next of five colours. Pressing the
     button again (it now says so), the ×, or Escape brings the clauses back.
     The space it takes is held at the clauses' height, so the swap does not
     jolt the page. */
  var SHADES = ['purple', 'blue', 'orange', 'pink', 'lime'];
  [].forEach.call(document.querySelectorAll('[data-short]'), function (box) {
    var btn = box.querySelector('[data-short-open]');
    var pop = box.querySelector('[data-short-pop]');
    var layout = document.querySelector('[data-legal]');
    if (!btn || !pop) return;
    var label = btn.querySelector('.lg-short-t'), go = btn.querySelector('.lg-short-go');
    var said = label ? label.textContent : '';
    var n = -1, showing = false;
    box.classList.add('lg-short-on');
    pop.hidden = true;
    pop.classList.add('lg-short-moved');           // styled as the swapped-in panel
    if (layout) layout.appendChild(pop);            // it is shown where the clauses are
    function show() {
      n = (n + 1) % SHADES.length;
      SHADES.forEach(function (c) { pop.classList.remove('sv-' + c); });
      pop.classList.add('sv-' + SHADES[n]);
      if (layout) {
        layout.style.minHeight = layout.offsetHeight + 'px';
        layout.classList.add('sv-showing');
      }
      pop.hidden = false;
      showing = true;
      if (label) label.textContent = 'Back to the full version';
      if (go) go.innerHTML = '&larr;';
      btn.setAttribute('aria-expanded', 'true');
    }
    function hide() {
      pop.hidden = true;
      showing = false;
      if (layout) { layout.classList.remove('sv-showing'); layout.style.minHeight = ''; }
      if (label) label.textContent = said;
      if (go) go.innerHTML = '&rarr;';
      btn.setAttribute('aria-expanded', 'false');
    }
    btn.setAttribute('aria-expanded', 'false');
    btn.addEventListener('click', function () { if (showing) hide(); else show(); });
    pop.querySelectorAll('[data-short-close]').forEach(function (b) { b.addEventListener('click', hide); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && showing) hide(); });
  });

  /* ---- the clause cards ------------------------------------------------ */
  var scope = document.querySelector('[data-legal]');
  if (!scope) return;
  var body = scope.querySelector('.legal-body');
  var cards = [].slice.call(scope.querySelectorAll('.legal-card'));
  if (!body || !cards.length) return;

  var links = {};
  scope.querySelectorAll('.legal-toc a[href^="#"]').forEach(function (a) {
    links[a.getAttribute('href').slice(1)] = a;
  });

  scope.classList.add('lg-on');
  cards.forEach(function (c) {
    c.classList.remove('reveal'); c.classList.add('in');
    c.setAttribute('tabindex', '0'); c.setAttribute('role', 'button');
  });

  /* the open card: one element, moved from tile to full size and back */
  var pop = document.createElement('div');
  pop.className = 'lg-pop';
  pop.setAttribute('aria-live', 'polite');
  pop.hidden = true;
  body.appendChild(pop);

  var FX = ['grow', 'spin', 'flip', 'swing', 'zoom'];
  var open = -1, held = -1, peekBack = -2, token = 0, hovering = false, inView = false;

  function colours(card, el) {
    var cs = getComputedStyle(card);
    el.style.setProperty('--cc', cs.getPropertyValue('--cc').trim() || '#5AA9FF');
    el.style.setProperty('--cc-ink', cs.getPropertyValue('--cc-ink').trim() || '#12558F');
  }
  function content(card, el) {
    colours(card, el);
    el.innerHTML = '';
    var head = document.createElement('div'); head.className = 'lg-pop-head';
    head.appendChild(card.querySelector('h2').cloneNode(true));
    var x = document.createElement('button');
    x.type = 'button'; x.className = 'lg-pop-x'; x.setAttribute('aria-label', 'Close'); x.innerHTML = '&times;';
    head.appendChild(x);
    var bar = document.createElement('i'); bar.className = 'lg-pop-bar'; head.appendChild(bar);
    var words = card.querySelector('.prose').cloneNode(true); words.classList.add('lg-pop-body');
    el.appendChild(head); el.appendChild(words);
  }

  /* The grid is kept as tall as the longest clause needs when open, so
     opening and closing never pushes the page about. */
  function size() {
    body.style.minHeight = '';
    var probe = document.createElement('div');
    probe.className = 'lg-pop lg-probe';
    probe.style.width = body.clientWidth + 'px';
    body.appendChild(probe);
    var max = 0;
    cards.forEach(function (c) { content(c, probe); max = Math.max(max, probe.scrollHeight); });
    probe.remove();
    body.style.minHeight = Math.max(max, body.scrollHeight) + 'px';
  }

  function rectOf(card) {
    var b = body.getBoundingClientRect(), r = card.getBoundingClientRect();
    return { l: r.left - b.left, t: r.top - b.top, w: r.width, h: r.height };
  }
  function full() { return { l: 0, t: 0, w: body.clientWidth, h: body.clientHeight }; }
  function place(r) {
    pop.style.left = r.l + 'px'; pop.style.top = r.t + 'px';
    pop.style.width = r.w + 'px'; pop.style.height = r.h + 'px';
  }
  function mark(i) {
    cards.forEach(function (c, k) {
      c.classList.toggle('is-on', k === i);
      c.classList.toggle('is-held', k === i && held === i);
      var a = links[c.id];
      if (a) {
        a.classList.toggle('on', k === i);
        if (k === i) colours(c, a);
      }
    });
    pop.classList.toggle('is-held', i >= 0 && held === i);
  }

  async function expand(i, fx, t) {
    var card = cards[i];
    content(card, pop);
    pop.className = 'lg-pop';
    place(rectOf(card));
    pop.hidden = false;
    open = i; mark(i);
    if (reduced) { place(full()); pop.classList.add('is-full'); return t === token; }
    await frame();
    if (t !== token) return false;
    pop.classList.add('is-moving', 'is-full', 'fx-' + fx);   // words show at once
    place(full());
    await wait(650);
    if (t !== token) return false;
    pop.classList.add('is-full');
    return true;
  }

  async function collapse() {
    if (open < 0) return;
    var card = cards[open];
    pop.classList.remove('is-full');
    if (!reduced) {
      pop.className = 'lg-pop is-moving' + (held === open ? ' is-held' : '');
      place(rectOf(card));
      await wait(560);
    }
    pop.hidden = true;
    open = -1; mark(-1);
  }

  function holdFor(card) {
    var chars = card.querySelector('.prose').textContent.length;
    return Math.max(6000, Math.min(22000, 2500 + chars * 38));
  }

  /* 1. on its own, one after another */
  async function auto(t, from) {
    for (var i = from || 0; ; i = (i + 1) % cards.length) {
      while (!inView || document.hidden) { await wait(300); if (t !== token) return; }
      if (!(await expand(i, FX[i % FX.length], t))) return;
      var total = holdFor(cards[i]), left = total;
      while (left > 0) {
        await wait(100);
        if (t !== token) return;
        if (!hovering && inView && !document.hidden) left -= 100;
        var bar = pop.querySelector('.lg-pop-bar');
        if (bar) bar.style.transform = 'scaleX(' + (1 - left / total).toFixed(3) + ')';
      }
      await collapse();
      if (t !== token) return;
      await wait(450);
      if (t !== token) return;
    }
  }
  function resume(from) { held = -1; var t = ++token; auto(t, from); }

  /* 2. pressed: open and stay */
  async function hold(i) {
    var t = ++token;
    held = i;
    if (open >= 0 && open !== i) await collapse();
    if (t !== token) return;
    if (open === i) { mark(i); return; }
    await expand(i, 'grow', t);
  }
  async function letGo() {
    var from = open >= 0 ? (open + 1) % cards.length : 0;
    held = -1; token++;
    await collapse();
    await wait(500);
    resume(from);
  }

  cards.forEach(function (c, i) {
    c.addEventListener('click', function () { hold(i); });
    c.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); hold(i); }
    });
  });
  pop.addEventListener('click', function (e) {
    if (e.target.closest('.lg-pop-x')) { letGo(); return; }
    if (e.target.closest('a')) return;
    if (open >= 0 && held !== open) hold(open);       // pressing the open card keeps it
  });
  pop.addEventListener('mouseenter', function () { hovering = true; });
  pop.addEventListener('mouseleave', function () { hovering = false; });

  /* 3. the menu: hover shows, click keeps */
  var toc = scope.querySelector('.legal-toc');
  Object.keys(links).forEach(function (id) {
    var a = links[id], i = cards.indexOf(document.getElementById(id));
    if (i < 0) return;
    a.addEventListener('click', function (e) {
      e.preventDefault();
      peekBack = -2;
      hold(i);
      if (history.replaceState) history.replaceState(null, '', '#' + id);
      if (window.innerWidth < 960) body.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' });
    });
    a.addEventListener('mouseenter', async function () {
      if (open === i) return;
      if (peekBack === -2) peekBack = held;           // what to go back to
      var t = ++token;
      if (open >= 0) { await collapse(); if (t !== token) return; }
      await expand(i, 'grow', t);
    });
  });
  if (toc) toc.addEventListener('mouseleave', async function () {
    if (peekBack === -2) return;
    var back = peekBack; peekBack = -2;
    if (back >= 0) { held = -1; hold(back); return; }
    var from = open >= 0 ? (open + 1) % cards.length : 0;
    token++;
    await collapse();
    await wait(400);
    resume(from);
  });

  if ('IntersectionObserver' in window) {
    new IntersectionObserver(function (es) { inView = es[0].isIntersecting; }, { threshold: 0.25 }).observe(body);
  } else { inView = true; }

  size();
  var rt;
  window.addEventListener('resize', function () {
    clearTimeout(rt);
    rt = setTimeout(function () { size(); if (open >= 0) place(full()); }, 150);
  });
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(size);

  /* arriving with #clause in the address opens that clause and keeps it */
  var start = location.hash ? cards.indexOf(document.getElementById(location.hash.slice(1))) : -1;
  if (start >= 0) hold(start); else resume(0);
})();
