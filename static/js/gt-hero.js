/* The hero types itself out when the page loads: the pill, the headline, the
 * sub-line, both CTAs, the fine print beneath them, a short new line about
 * the women's competitions, then the scoreboard's own captions.
 *
 * CLIENT, 27 SEP 2026: "the pill, the h1, the sub, the CTAs, the scoreboard,
 * when the page loads I want to see them typed, like I can see it happening
 * live... also add a bit more, something short that builds interest, and
 * mention that even the women's sports, like AFLW and NRLW, are part of it."
 *
 * ONCE, ON LOAD — NOT ON SCROLL. gt-founding.js retypes every time its
 * section re-enters view, which is right for something the reader may scroll
 * back to. The hero is the first thing anyone sees; retyping it every time
 * someone scrolls back to the top would be a parlour trick, not a greeting.
 * It plays exactly once per page load.
 *
 * WAITS FOR THE LOADER, same idiom as gt-trailer.js: the sport splash covers
 * the page for its first moments, and typing underneath it would be wasted
 * motion nobody sees start.
 *
 * THE SCOREBOARD'S NUMBERS ARE NOT TOUCHED. Its ring/pips/chips/count-up are
 * already animated by base.html's own IntersectionObserver, independently of
 * this file — only the three small captions under them (.sb-label) are typed,
 * so the two animations run alongside each other rather than one replacing
 * the other. */
(function () {
  'use strict';

  var hero = document.querySelector('[data-hero]');
  if (!hero) return;
  var tyEls = [].slice.call(hero.querySelectorAll('[data-ty]'));
  if (!tyEls.length) return;

  var forced = document.documentElement.classList.contains('gt-motion');
  var reduced = !forced && window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced) return;   /* the template's own full text stands */

  function runs(node, out) {
    out = out || [];
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

  var lines;
  try {
    lines = tyEls.map(function (el) {
      return { el: el, parts: runs(el).map(withRest) };
    });
  } catch (err) { return; }

  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  /* 6 Oct 2026 (client): the hero buttons still shook while typing. The
     hidden remainder holds a line's size only to within a fraction of a
     pixel — split text kerns differently at the join — and the two centred
     buttons shift with every letter of it. So each element is measured once,
     finished, and held at exactly that box until its line is typed. */
  lines.forEach(function (l) {
    /* Measured when typing starts (fonts loaded, the untyped text still laid
       out invisibly in .ty-rest), from the computed size rather than
       getBoundingClientRect: an entrance transform may be mid-flight. */
    l.hold = function () {
      var cs = getComputedStyle(l.el);
      /* Shrink-to-fit boxes — inline ones, and flex/grid items like the two
         buttons (blockified, so their display no longer says inline) — take
         their width from their text; those are the ones to pin. */
      var pd = l.el.parentElement ? getComputedStyle(l.el.parentElement).display : '';
      if (cs.display.indexOf('inline') === 0 || /flex|grid/.test(pd)) { l.el.style.width = cs.width; }
      l.el.style.height = cs.height;
    };
    l.release = function () { l.el.style.width = l.el.style.height = ''; };
  });
  lines.forEach(function (l) { l.parts.forEach(function (p) { put(p, 0); }); });

  async function typeLine(l) {
    for (var k = 0; k < l.parts.length; k++) {
      var p = l.parts[k];
      for (var i = 1; i <= p.full.length; i++) {
        put(p, i);
        var ch = p.full.charAt(i - 1), d = 1000 / 50 * (0.6 + Math.random() * 0.7);
        if (ch === '.' || ch === '?' || ch === '—') d += 160; else if (ch === ',') d += 70;
        await wait(d);
      }
    }
    l.release();
  }

  async function play() {
    if (document.fonts && document.fonts.ready) { try { await document.fonts.ready; } catch (e) {} }
    lines.forEach(function (l) { l.hold(); });
    for (var i = 0; i < lines.length; i++) {
      await typeLine(lines[i]);
      await wait(160);
    }
  }

  /* The sport loader covers the page for its first moments; typing underneath
     it would run unseen, so this waits for it to leave, with a ceiling in
     case it never does — same idiom as gt-trailer.js. */
  function whenReady(cb) {
    var b = document.body, done = false;
    function go() { if (done) return; done = true; if (mo) mo.disconnect(); cb(); }
    if (b.classList.contains('ready') || !b.classList.contains('loading')) return setTimeout(go, 150);
    var mo = window.MutationObserver ? new MutationObserver(function () { if (b.classList.contains('ready')) setTimeout(go, 150); }) : null;
    if (mo) mo.observe(b, { attributes: true, attributeFilter: ['class'] });
    setTimeout(go, 5000);
  }

  whenReady(play);
})();
