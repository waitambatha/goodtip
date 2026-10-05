/* Pricing: the plans table, one plan at a time.

   3 OCT 2026 (client): "the table remains", but each plan takes a turn — its
   row lights in the plan's colour, a card grows out of the row showing more
   about it (each plan arriving its own way: a 360 turn, a flip, a burst, a
   swing, a rise — "I don't want all of them doing 360 turns"), then it
   shrinks back into its row and the next plan goes. Workplace's card says
   why it is the most popular.

   The card is one element moved from the row's rectangle to the size of the
   whole table and back, over the table rather than inside it, and the table
   keeps the height of the tallest card — so a turn never moves the page.

   Runs while the table is on screen; hovering the open card holds it;
   pressing a row opens that plan and keeps it open until its × is pressed.
   Reduced motion (unless ?motion=1 set html.gt-motion) keeps the turns and
   drops the movement. Without script the table is the page, as it was. */
(function () {
  'use strict';

  var wrap = document.querySelector('[data-plans]');
  if (!wrap) return;
  var rows = [].slice.call(wrap.querySelectorAll('tr[data-plan]'));
  var source = wrap.querySelector('.pt-details');
  if (!rows.length || !source) return;

  var root = document.documentElement;
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches &&
    !root.classList.contains('gt-motion');
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function frame() { return new Promise(function (r) { requestAnimationFrame(function () { requestAnimationFrame(r); }); }); }

  wrap.classList.add('pt-on');
  rows.forEach(function (r) { r.setAttribute('tabindex', '0'); r.setAttribute('role', 'button'); });

  var pop = document.createElement('div');
  pop.className = 'pt-pop';
  pop.hidden = true;
  pop.setAttribute('aria-live', 'polite');
  wrap.appendChild(pop);

  var open = -1, held = -1, token = 0, hovering = false, inView = false;
  var heights = [];                              // each plan's card height, open

  function fill(i, el) {
    var key = rows[i].getAttribute('data-plan');
    var d = source.querySelector('[data-plan-detail="' + key + '"]');
    el.className = 'pt-pop pp-' + key;
    el.innerHTML = '';
    if (d) el.innerHTML = d.innerHTML;
    var x = document.createElement('button');
    x.type = 'button'; x.className = 'pt-x'; x.setAttribute('aria-label', 'Close'); x.innerHTML = '&times;';
    el.appendChild(x);
    var bar = document.createElement('i'); bar.className = 'pt-bar'; el.appendChild(bar);
  }

  /* the table keeps the height of the tallest card */
  function size() {
    wrap.style.minHeight = '';
    var probe = document.createElement('div');
    probe.style.cssText = 'position:absolute;visibility:hidden;left:0;top:0;height:auto;width:' + wrap.clientWidth + 'px';
    wrap.appendChild(probe);
    var max = 0;
    rows.forEach(function (r, i) { fill(i, probe); probe.style.position = 'absolute'; probe.style.visibility = 'hidden'; probe.style.height = 'auto'; heights[i] = probe.scrollHeight; max = Math.max(max, heights[i]); });
    probe.remove();
    wrap.style.minHeight = Math.max(max, wrap.scrollHeight) + 'px';
  }

  function rectOf(row) {
    var b = wrap.getBoundingClientRect(), r = row.getBoundingClientRect();
    return { l: r.left - b.left, t: r.top - b.top, w: r.width, h: r.height };
  }
  /* Open (3 Oct 2026, client: "expand a little bit more … it comes to the
     centre and the whole table becomes translucent"): on a wide screen the
     card grows a little past the table's edges and settles in the MIDDLE of
     it, while the table behind fades back (.pt-focus). On a phone, where the
     table is a tall stack, it opens centred on its own row instead, so it
     appears where you are reading rather than a screen away. */
  var GROW = 28;                                   // px past each side, wide screens
  function full(i) {
    var W = wrap.clientWidth, H = wrap.clientHeight;
    var h = Math.max(heights[i] || 0, 260);
    if (window.innerWidth > 860) {
      h = Math.min(H + 2 * GROW, h + 2 * GROW);
      return { l: -GROW, t: (H - h) / 2, w: W + 2 * GROW, h: h };
    }
    h = Math.min(H, h);
    var r = rectOf(rows[i]);
    var t = Math.max(0, Math.min(H - h, r.t + r.h / 2 - h / 2));
    return { l: 0, t: t, w: W, h: h };
  }
  function place(r) {
    pop.style.left = r.l + 'px'; pop.style.top = r.t + 'px';
    pop.style.width = r.w + 'px'; pop.style.height = r.h + 'px';
  }
  function mark(i) {
    rows.forEach(function (r, k) { r.classList.toggle('is-lit', k === i); });
    pop.classList.toggle('is-held', i >= 0 && held === i);
  }

  async function expand(i, t) {
    var row = rows[i], fx = row.getAttribute('data-fx') || 'rise';
    fill(i, pop);
    place(rectOf(row));
    pop.hidden = false;
    open = i; mark(i);
    wrap.classList.add('pt-focus');                 // the table steps back
    if (reduced) { place(full(i)); pop.classList.add('is-full'); return t === token; }
    await frame();
    if (t !== token) return false;
    pop.classList.add('is-moving', 'is-full', 'fx-' + fx);
    place(full(i));
    await wait(750);
    return t === token;
  }
  async function collapse() {
    if (open < 0) return;
    var row = rows[open];
    wrap.classList.remove('pt-focus');              // the table comes back as the card returns
    if (!reduced) {
      pop.classList.remove('is-full');
      pop.classList.add('is-moving', 'is-closing');
      place(rectOf(row));
      await wait(560);
    }
    pop.hidden = true;
    open = -1; mark(-1);
  }

  async function auto(t, from) {
    for (var i = from || 0; ; i = (i + 1) % rows.length) {
      while (!inView || document.hidden) { await wait(300); if (t !== token) return; }
      if (!(await expand(i, t))) return;
      var total = rows[i].getAttribute('data-plan') === 'workplace' ? 8000 : 6000, left = total;
      while (left > 0) {
        await wait(100);
        if (t !== token) return;
        if (!hovering && inView && !document.hidden) left -= 100;
        var bar = pop.querySelector('.pt-bar');
        if (bar) bar.style.transform = 'scaleX(' + (1 - left / total).toFixed(3) + ')';
      }
      await collapse();
      if (t !== token) return;
      /* a breather with the table in full view, the next plan's row already
         lit, so the table stays the thing you are reading */
      mark((i + 1) % rows.length);
      await wait(1900);
      if (t !== token) return;
    }
  }
  function resume(from) { held = -1; var t = ++token; auto(t, from); }

  async function hold(i) {
    var t = ++token;
    held = i;
    if (open >= 0 && open !== i) await collapse();
    if (t !== token) return;
    if (open === i) { mark(i); return; }
    await expand(i, t);
  }
  async function letGo() {
    var from = open >= 0 ? (open + 1) % rows.length : 0;
    held = -1; token++;
    await collapse();
    await wait(400);
    resume(from);
  }

  rows.forEach(function (r, i) {
    r.addEventListener('click', function () { hold(i); });
    r.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); hold(i); }
    });
  });
  pop.addEventListener('click', function (e) {
    if (e.target.closest('.pt-x')) { letGo(); return; }
    if (open >= 0 && held !== open) hold(open);     // pressing the open card keeps it
  });
  pop.addEventListener('mouseenter', function () { hovering = true; });
  pop.addEventListener('mouseleave', function () { hovering = false; });

  if ('IntersectionObserver' in window) {
    new IntersectionObserver(function (es) { inView = es[0].isIntersecting; }, { threshold: 0.3 }).observe(wrap);
  } else { inView = true; }

  size();
  var rt;
  window.addEventListener('resize', function () {
    clearTimeout(rt);
    rt = setTimeout(function () { size(); if (open >= 0) place(full(open)); }, 150);
  });
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(size);
  resume(0);
})();
