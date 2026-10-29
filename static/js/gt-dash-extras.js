/* ==========================================================================
   gt-dash-extras.js — the dashboard's newer behaviours (client, 28 Oct 2026).

   1. The banner's stories turn over one at a time (data-dbn), paused while
      pointed at or focused, and not at all under reduced motion.
   2. The fixtures box shows eight matches, then scrolls (#roundFixtures) —
      measured from the cards themselves, since a card with MatchReader open
      is taller than one without. Re-measured after every htmx swap (a chip,
      a round) and when a card grows.
   3. The chip row and the round rail are one sideways strip each: the lit
      pill is scrolled into view, and a vertical wheel scrolls them sideways.
   ========================================================================== */
(function () {
  'use strict';
  var EVERY = 4000;          // the story card turns every four seconds
  var SHOW = 8;              // fixtures before the box scrolls

  /* 1. THE BANNER'S STORIES (client, 28 Oct 2026: "give it a little bit of
     time... five seconds the way it shows, then the next five seconds it
     enlarges — it's now that full thing — somebody reads the blog a little,
     then it shrinks... then the next blog comes with a 360, the next one a
     backflip"). One story = SPLIT (5s) -> BIG, the whole banner (5s) ->
     back to split -> the next arrives turning, the turn alternating. Pointing
     at it holds whatever it is doing. */
  var SPLIT = 5000, BIG = 5000, SETTLE = 900;
  function stories() {
    var box = document.querySelector('[data-dbn]');
    if (!box || box.dataset.ready) return;
    box.dataset.ready = '1';
    var banner = box.closest('.dash-banner');
    var cards = box.querySelectorAll('[data-dbn-card]');
    var dots = box.querySelectorAll('.dbn-dots i');
    if (!cards.length) return;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches &&
        !document.documentElement.classList.contains('gt-motion')) return;
    var i = 0, held = false, turn = 0, timer = null;
    box.addEventListener('mouseenter', function () { held = true; });
    box.addEventListener('mouseleave', function () { held = false; });
    box.addEventListener('focusin', function () { held = true; });
    box.addEventListener('focusout', function () { held = false; });
    function later(fn, ms) {
      timer = setTimeout(function wait() {
        if (held || document.hidden) { timer = setTimeout(wait, 400); return; }
        fn();
      }, ms);
    }
    function grow() {
      if (banner) banner.classList.add('dbn-is-big');
      later(shrink, BIG);
    }
    function shrink() {
      if (banner) banner.classList.remove('dbn-is-big');
      later(next, SETTLE);
    }
    function next() {
      if (cards.length < 2) { later(grow, SPLIT); return; }
      var out = cards[i];
      out.classList.remove('on', 'turn-y', 'turn-x');
      if (dots[i]) dots[i].classList.remove('on');
      i = (i + 1) % cards.length;
      var c = cards[i];
      c.classList.remove('turn-y', 'turn-x');
      void c.offsetWidth;                       // restart the turn animation
      c.classList.add('on', turn++ % 2 ? 'turn-x' : 'turn-y');
      if (dots[i]) dots[i].classList.add('on');
      later(grow, SPLIT);
    }
    later(grow, SPLIT);
  }

  /* The Rounds button on the Competitions panel opens the drawer below it.
     Delegated: both are swapped by htmx. */
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-rounds-toggle]');
    if (!b) return;
    var d = document.getElementById('roundDrawer');
    if (!d) return;
    var open = d.hidden;
    if (open) { d.hidden = false; requestAnimationFrame(function () { d.classList.add('is-open'); strips(); }); }
    else { d.classList.remove('is-open'); setTimeout(function () { d.hidden = true; }, 260); }
    b.setAttribute('aria-expanded', String(open));
    b.classList.toggle('on', open);
  });

  /* "Show more results" keeps your place in the fixtures box. */
  var keptScroll = null;
  document.body.addEventListener('htmx:beforeRequest', function (e) {
    var el = e.detail && e.detail.elt;
    if (el && el.hasAttribute && el.hasAttribute('data-keep-scroll')) {
      var box = document.getElementById('roundFixtures');
      keptScroll = box ? box.scrollTop : null;
    }
  });

  function fitFixtures() {
    var box = document.getElementById('roundFixtures');
    if (!box) return;
    var cards = box.querySelectorAll('.fxc');
    if (cards.length <= SHOW) { box.style.maxHeight = 'none'; return; }
    var last = cards[SHOW - 1];
    var top = box.getBoundingClientRect().top - box.scrollTop;
    var bottom = last.getBoundingClientRect().bottom - top;
    box.style.maxHeight = Math.ceil(bottom + 12) + 'px';
  }

  function strips() {
    document.querySelectorAll('[data-crow-scroll], [data-rrail]').forEach(function (strip) {
      if (!strip.dataset.wheel) {
        strip.dataset.wheel = '1';
        strip.addEventListener('wheel', function (e) {
          if (Math.abs(e.deltaY) > Math.abs(e.deltaX) && strip.scrollWidth > strip.clientWidth) {
            strip.scrollLeft += e.deltaY; e.preventDefault();
          }
        }, { passive: false });
      }
      var on = strip.querySelector('.on');
      if (on) strip.scrollLeft = on.offsetLeft - strip.clientWidth / 2 + on.offsetWidth / 2;
    });
  }

  /* 4. The chip row's arrows: shown only when there is more that way. */
  function chipArrows() {
    document.querySelectorAll('.crow').forEach(function (row) {
      var strip = row.querySelector('[data-crow-scroll]');
      var l = row.querySelector('.crow-arrow.is-left'), r = row.querySelector('.crow-arrow.is-right');
      if (!strip || !l || !r) return;
      function show() {
        l.hidden = strip.scrollLeft <= 4;
        r.hidden = strip.scrollLeft + strip.clientWidth >= strip.scrollWidth - 4;
      }
      if (!row.dataset.arrows) {
        row.dataset.arrows = '1';
        [l, r].forEach(function (b) {
          b.addEventListener('click', function () {
            strip.scrollBy({ left: (+b.getAttribute('data-crow-step')) * strip.clientWidth * 0.7, behavior: 'smooth' });
          });
        });
        strip.addEventListener('scroll', show, { passive: true });
      }
      show();
    });
  }

  /* 5. The news under the fixtures: pages of twelve that turn by themselves
     (paused while pointed at), and can be swiped, scrolled or stepped. */
  var PAGE_EVERY = 7000;
  function newsPages() {
    var box = document.querySelector('[data-dng]');
    if (!box || box.dataset.ready) return;
    box.dataset.ready = '1';
    var track = box.querySelector('[data-dng-track]');
    var pages = box.querySelectorAll('[data-dng-page]');
    var dots = box.querySelector('[data-dng-dots]');
    var nav = box.querySelector('.dng-nav');
    if (pages.length < 2) { if (nav) nav.hidden = true; return; }
    for (var k = 0; k < pages.length; k++) {
      var d = document.createElement('i'); dots.appendChild(d);
    }
    function current() { return Math.round(track.scrollLeft / track.clientWidth); }
    function go(n) {
      n = (n + pages.length) % pages.length;
      track.scrollTo({ left: n * track.clientWidth, behavior: 'smooth' });
    }
    function mark() {
      var c = current();
      dots.querySelectorAll('i').forEach(function (d, i) { d.classList.toggle('on', i === c); });
    }
    track.addEventListener('scroll', function () { window.requestAnimationFrame(mark); }, { passive: true });
    box.querySelector('[data-dng-prev]').addEventListener('click', function () { go(current() - 1); });
    box.querySelector('[data-dng-next]').addEventListener('click', function () { go(current() + 1); });
    dots.addEventListener('click', function (e) {
      var i = Array.prototype.indexOf.call(dots.children, e.target); if (i > -1) go(i);
    });
    var held = false;
    box.addEventListener('mouseenter', function () { held = true; });
    box.addEventListener('mouseleave', function () { held = false; });
    box.addEventListener('focusin', function () { held = true; });
    box.addEventListener('focusout', function () { held = false; });
    mark();
    setInterval(function () {
      if (!held && !document.hidden) {
        var r = box.getBoundingClientRect();
        if (r.top < window.innerHeight && r.bottom > 0) go(current() + 1);
      }
    }, PAGE_EVERY);
  }

  /* 6. Find a competition in the pin panel. Delegated: the chip row is
     swapped by htmx. */
  document.addEventListener('input', function (e) {
    var box = e.target.closest && e.target.closest('[data-pinx-filter]');
    if (!box) return;
    var panel = box.closest('.pinx'), q = box.value.trim().toLowerCase(), any = false;
    panel.querySelectorAll('[data-pinx-sport]').forEach(function (sec) {
      var shown = 0;
      sec.querySelectorAll('[data-pinx-item]').forEach(function (f) {
        var on = !q || f.getAttribute('data-pinx-item').indexOf(q) > -1;
        f.hidden = !on; if (on) shown++;
      });
      sec.hidden = shown === 0; if (shown) any = true;
    });
    var none = panel.querySelector('[data-pinx-none]'); if (none) none.hidden = any;
  });
  document.addEventListener('toggle', function (e) {
    if (e.target.classList && e.target.classList.contains('crow-pin') && e.target.open) {
      var f = e.target.querySelector('[data-pinx-filter]'); if (f) setTimeout(function () { f.focus(); }, 60);
    }
  }, true);

  function all() { stories(); fitFixtures(); strips(); chipArrows(); newsPages(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', all); else all();
  window.addEventListener('load', fitFixtures);
  window.addEventListener('resize', fitFixtures);
  document.body.addEventListener('htmx:afterSwap', function () {
    setTimeout(function () {
      fitFixtures(); strips(); chipArrows();
      if (keptScroll !== null) {
        var box = document.getElementById('roundFixtures');
        if (box) box.scrollTop = keptScroll;
        keptScroll = null;
      }
    }, 30);
  });
  window.addEventListener('resize', chipArrows);
  document.addEventListener('toggle', function () { setTimeout(fitFixtures, 30); }, true);
  document.addEventListener('click', function (e) {
    if (e.target.closest && e.target.closest('[data-reader-toggle], [data-reader-close]')) setTimeout(fitFixtures, 60);
  });
})();
