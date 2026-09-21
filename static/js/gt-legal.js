/* The legal pages: which clause you are reading, and saying so.
 *
 * CLIENT, 20 SEP 2026: "proper flow when I scroll down, all animation that is
 * needed — such as expanding of a card and colour change as I scroll down."
 *
 * The expand is CSS on the way in (see .page-legal .legal-card.reveal in
 * goodtip.css). This is the other half: as a clause enters the band of the
 * screen somebody actually reads from, it takes the green — its left edge
 * fills, its number chip inverts — and the row for it in the contents rail
 * lights to match.
 *
 * WHY A READING BAND AND NOT HOVER. On a document you are not pointing at what
 * you are reading; the pointer is wherever you left it, usually nowhere near
 * the words. Hover answers "what could I click", which is a different question
 * and already has its own, quieter treatment.
 *
 * WHY ONE CLAUSE AND NOT EVERY VISIBLE ONE. Three cards can be on screen at
 * once on a laptop. Lighting all three says nothing; the point of the mark is
 * to answer "where am I", and an answer with three values is not one. The band
 * is a horizontal line a third of the way down the viewport, and the clause
 * crossing it wins — which is roughly where a person's eye sits when they are
 * reading rather than scanning.
 *
 * WHY NOT IntersectionObserver. It reports when an element crosses a boundary,
 * which is the question "has it arrived", not "which one is nearest the line" —
 * answering the second with the first means a stack of thresholds per card and
 * a tie-break anyway. One rect read per card on a throttled scroll is less
 * code, exact, and on a page of eleven cards costs nothing measurable.
 */
(function () {
  'use strict';

  var root = document.querySelector('[data-legal]');
  if (!root) return;

  var cards = Array.prototype.slice.call(root.querySelectorAll('.legal-card'));
  if (!cards.length) return;

  /* Rail rows keyed by the id they point at, so the lookup is not a query per
     card per frame. */
  var links = {};
  root.querySelectorAll('.legal-toc a[href^="#"]').forEach(function (a) {
    links[a.getAttribute('href').slice(1)] = a;
  });

  var current = null;

  function mark() {
    var line = window.innerHeight * 0.33;
    var best = null;
    var bestGap = Infinity;

    for (var i = 0; i < cards.length; i++) {
      var r = cards[i].getBoundingClientRect();
      if (r.bottom < 0 || r.top > window.innerHeight) continue;
      /* Distance from the reading line to the card, zero while the line is
         inside it. A card the line sits within always beats one it does not,
         which is what stops the mark flickering between neighbours at the
         moment one hands over to the next. */
      var gap = (r.top > line) ? r.top - line : (r.bottom < line ? line - r.bottom : 0);
      if (gap < bestGap) { bestGap = gap; best = cards[i]; }
    }

    /* Nothing in view — above the first clause or below the last. The mark is
       left where it was rather than cleared: an empty rail while the reader is
       still inside the document reads as the feature breaking, and "the last
       clause you were in" is the honest answer at the bottom of the page. */
    if (!best || best === current) return;

    if (current) {
      current.classList.remove('is-reading');
      var was = links[current.id];
      if (was) was.classList.remove('on');
    }
    best.classList.add('is-reading');
    var now = links[best.id];
    if (now) {
      /* The rail row takes the CARD's colour, copied across rather than
         worked out again here. Each clause gets its hue from its position in
         the document (nth-child in goodtip.css), so the rail cannot derive it
         from anything it knows about itself — and a second copy of the rule
         is a second thing to keep in step. */
      var cs = getComputedStyle(best);
      now.style.setProperty('--rc', cs.getPropertyValue('--cc').trim());
      now.style.setProperty('--rc-ink', cs.getPropertyValue('--cc-ink').trim());
      now.classList.add('on');
    }
    current = best;
  }

  /* One read per frame at most. Scroll fires far faster than the screen
     redraws, and every one of these calls measures eleven elements. */
  var queued = false;
  function onScroll() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(function () { queued = false; mark(); });
  }

  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', onScroll, { passive: true });
  mark();

  /* Jumping from the rail lands mid-page with no scroll event of its own if
     the target is already where it needs to be. */
  root.addEventListener('click', function (e) {
    if (e.target.closest('.legal-toc a')) setTimeout(mark, 60);
  });
})();
