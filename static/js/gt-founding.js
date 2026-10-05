/* Founding Partner Program: the left column types itself out as the section
 * scrolls into view; once it finishes, the calendar on the right flips
 * through its pages and lands on the real closing date, on a loop.
 *
 * CLIENT, 27 SEP 2026: "for this section... looks good, but I want that part
 * to be not static, but let it get typed as I reach there, then after typing
 * this part... what I want to see on the right [is] a calendar, that we see
 * we flicker through the calendar, the calendar that looks like pages, so we
 * see move from page to page quickly and get to that date, have it
 * highlighted, and that action will be on a loop."
 *
 * MARKUP IS THE REAL DATE, same rule as everywhere else here. .fp-cal-mo/
 * -day/-yr already hold the correct founding_closes value from the template.
 * This script reads that value once, uses it to build a short run of made-up
 * "flipping past" dates that lead up to it, and always ends a cycle back on
 * the genuine text — so JavaScript off, reduced motion, or this script
 * failing to parse the date all leave a perfectly normal date card behind. */
(function () {
  'use strict';

  var sec = document.querySelector('[data-founding]');
  if (!sec) return;

  var forced = document.documentElement.classList.contains('gt-motion');
  var reduced = !forced && window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced || !('IntersectionObserver' in window)) return;   /* the template's own markup stands */

  /* ---------- typed copy ---------- */

  var tyEls = [].slice.call(sec.querySelectorAll('[data-ty]'));

  function runs(node, out) {
    out = out || [];
    [].forEach.call(node.childNodes, function (kid) {
      if (kid.nodeType === 3) { if (kid.nodeValue.length) out.push(kid); }
      else if (kid.nodeType === 1) {
        // The checklist tick and the live-dot are icons, not prose — they
        // have no useful text of their own, but skip them by name anyway so
        // a future edit that puts a symbol in one does not get typed out.
        if (kid.classList && (kid.classList.contains('bk') || kid.classList.contains('live-dot'))) return;
        runs(kid, out);
      }
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

  var copy;
  try {
    copy = tyEls.map(function (el) {
      return { el: el, parts: runs(el).map(withRest) };
    });
  } catch (err) { copy = []; }

  function wait(ms, t) { return new Promise(function (r) { setTimeout(function () { r(t === token); }, ms); }); }

  function blankCopy() {
    copy.forEach(function (c) { c.parts.forEach(function (p) { put(p, 0); }); });
  }
  function revealCopy() {
    copy.forEach(function (c) { c.parts.forEach(function (p) { put(p, p.full.length); }); });
  }

  async function typeLine(c, t) {
    for (var k = 0; k < c.parts.length; k++) {
      var p = c.parts[k];
      for (var i = 1; i <= p.full.length; i++) {
        put(p, i);
        var ch = p.full.charAt(i - 1), d = 1000 / 52 * (0.6 + Math.random() * 0.7);
        if (ch === '.' || ch === '?') d += 200; else if (ch === ',') d += 80;
        if (!(await wait(d, t))) return false;
      }
    }
    return wait(220, t);
  }

  async function typeCopy(t) {
    if (!copy.length) return true;
    blankCopy();
    for (var i = 0; i < copy.length; i++) {
      if (!(await typeLine(copy[i], t))) return false;
    }
    return true;
  }

  /* ---------- flipping calendar ---------- */

  var cal = sec.querySelector('[data-cal]');
  var page = cal && cal.querySelector('.fp-cal-page');
  var moEl = page && page.querySelector('.fp-cal-mo');
  var dayEl = page && page.querySelector('.fp-cal-day');
  var yrEl = page && page.querySelector('.fp-cal-yr');

  var MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
  var real = null;   /* { mo, day, yr } as they appear in the template, and a parsed Date */
  if (page) {
    var realMo = moEl.textContent, realDay = dayEl.textContent, realYr = yrEl.textContent;
    var d = new Date(realMo + ' ' + realDay + ' ' + realYr);
    if (!isNaN(d.getTime())) real = { mo: realMo, day: realDay, yr: realYr, date: d };
  }

  /* A short run of fake dates stepping backward from the real one by a few
     weeks each, oldest first, so playing the array forward reads as paging
     forward through the calendar toward the date that actually matters. */
  function fakeRun() {
    var out = [];
    for (var back = 6; back >= 1; back--) {
      var f = new Date(real.date.getTime());
      f.setDate(f.getDate() - back * 11);
      out.push({ mo: MONTHS[f.getMonth()], day: String(f.getDate()).padStart(2, '0'), yr: String(f.getFullYear()) });
    }
    return out;
  }

  function setPage(v) { moEl.textContent = v.mo; dayEl.textContent = v.day; yrEl.textContent = v.yr; }

  /* The swap happens mid-flip, when the page is rotated edge-on to the
     viewer — so what actually gets seen is a page turning over to reveal
     the next date, not a date changing while sitting flat. */
  function flipTo(v, hit) {
    return new Promise(function (resolve) {
      page.classList.remove('hit');
      page.classList.add('flip');
      setTimeout(function () { setPage(v); if (hit) page.classList.add('hit'); }, 150);
      setTimeout(function () { page.classList.remove('flip'); resolve(); }, 330);
    });
  }

  async function calLoop(t) {
    if (!page || !real) return;
    var run = fakeRun();
    for (;;) {
      for (var i = 0; i < run.length; i++) {
        await flipTo(run[i], false);
        if (!(await wait(70, t)) || t !== token) return;
      }
      await flipTo(real, true);
      if (!(await wait(3200, t)) || t !== token) return;
    }
  }

  function resetCal() {
    if (page) { page.classList.remove('flip', 'hit'); if (real) setPage(real); }
  }

  /* ---------- orchestration ---------- */

  var token = 0, inView = false, running = false;

  async function play(t) {
    if (!(await typeCopy(t))) return;
    await calLoop(t);
  }

  function begin() {
    var t = ++token;
    running = true;
    play(t).then(function () { if (t === token) running = false; });
  }
  function halt() {
    token++; running = false;
    revealCopy();
    resetCal();
  }

  function sync() {
    if (inView && !document.hidden) { if (!running) begin(); }
    else if (running) halt();
  }

  blankCopy();
  document.addEventListener('visibilitychange', sync);
  new IntersectionObserver(function (es) {
    var was = inView;
    inView = es[es.length - 1].isIntersecting;
    if (inView !== was) sync();
  }, { threshold: .3 }).observe(sec);

  /* If the observer never fires, the section must not sit blank forever. */
  setTimeout(function () { if (!running) { revealCopy(); resetCal(); } }, 20000);
})();
