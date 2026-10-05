/* Pricing: one large, permanently static membership card (see
   /home/mbatha_waita/Documents/card1.png) captioned by copy on the left.
   home.html and goodtip.css already draw the section exactly as the
   reference does; all this script does is keep the card from being frozen
   forever on one plan — every few seconds it cross-fades the card's own
   numbers, and the "Team — $299/year..." line beside it, through the four
   real plans printed from billing/pricing.py (kept in the DOM, visually
   hidden, purely as this script's data source).

   REBUILT 29 SEP 2026. The client's reference image has no room for a typed
   form or a flip-to-a-receipt — it is one card — so the typing/cursor/flip
   sequence that used to live here (and the "cursor picks a card" step before
   that) is gone with it. With reduced motion (the site's own gate, so
   ?motion=1 overrides it), the cycle never starts and the card stays on
   whichever plan the markup already shows.

   30 SEP 2026: client wanted the card itself back in motion — "turn in a
   nice animated way and show the plan" — so each cycle now also adds
   .pf-flip to the card for one 700ms turn (see the matching @keyframes
   pfFlip in goodtip.css) and swaps its plan badge and team-size cell at
   the turn's midpoint, when the card is edge-on and nothing swapping is
   visible — the same "swap while invisible" idiom gt-founding.js uses for
   its flipping calendar page.

   Same client, follow-up message: the badge should show the price as well
   as the plan, each plan should stand for about 5 seconds before the next
   turn, and the card itself should change colour with the plan — "red,
   blue, purple and pink". COLORS below is that cycle, in the same order as
   the four .prz-cN cards (Starter, Team, Workplace, Organisation); the
   class it swaps to is what goodtip.css's .pf-red/.pf-blue/.pf-purple/
   .pf-pink rules read to repaint the card, its border and its corner
   swoosh.

   Third message, on the "Showing now" panel specifically: "let it also
   change the colors here" — recolourLive() puts the same class on that
   panel, timed to the crossfade's own swap (200ms) rather than the card's
   flip (350ms), since the panel has no turn of its own to hide the change
   inside. */
(function () {
  'use strict';

  var sec = document.querySelector('[data-prz]');
  if (!sec) return;
  var cards = [].slice.call(sec.querySelectorAll('.price-card'));
  if (cards.length < 2) return;

  var forced = document.documentElement.classList.contains('gt-motion');
  if (!forced && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  var planEl = sec.querySelector('[data-live-plan]');
  var priceEl = sec.querySelector('[data-live-price]');
  var seatEls = [].slice.call(sec.querySelectorAll('[data-seats], [data-live-seats]'));
  var pfCard = sec.querySelector('.pf-card');
  var pfPlanEl = sec.querySelector('[data-plan]');
  var liveEl = sec.querySelector('[data-live]');
  var COLORS = ['pf-red', 'pf-blue', 'pf-purple', 'pf-pink'];

  function read(card) {
    return {
      label: card.querySelector('.price-tier').textContent.trim(),
      price: card.querySelector('.price-num').textContent.trim(),
      seats: card.querySelector('.price-size').textContent.trim().replace(/\s*people\s*$/, '')
    };
  }

  function colorOf(card) {
    for (var n = 0; n < COLORS.length; n++) {
      if (card.classList.contains('prz-c' + (n + 1))) return COLORS[n];
    }
    return COLORS[0];
  }

  var i = cards.findIndex(function (c) { return c.classList.contains('prz-c2'); });
  if (i < 0) i = 0;

  function crossfade(el, value) {
    if (!el || el.textContent === value) return;
    el.style.transition = 'opacity .2s ease';
    el.style.opacity = '0';
    setTimeout(function () {
      el.textContent = value;
      el.style.opacity = '1';
    }, 200);
  }

  function recolourLive(card) {
    if (!liveEl) return;
    setTimeout(function () {
      liveEl.classList.remove.apply(liveEl.classList, COLORS);
      liveEl.classList.add(colorOf(card));
    }, 200);
  }

  function flip(card, p) {
    if (!pfCard) return;
    pfCard.classList.add('pf-flip');
    setTimeout(function () {
      if (pfPlanEl) pfPlanEl.textContent = p.label + ' plan · ' + p.price + '/year';
      pfCard.classList.remove.apply(pfCard.classList, COLORS);
      pfCard.classList.add(colorOf(card));
    }, 350);
    pfCard.addEventListener('animationend', function done() {
      pfCard.removeEventListener('animationend', done);
      pfCard.classList.remove('pf-flip');
    });
  }

  function step() {
    i = (i + 1) % cards.length;
    var card = cards[i];
    var p = read(card);
    crossfade(planEl, p.label);
    crossfade(priceEl, p.price);
    seatEls.forEach(function (e) { crossfade(e, p.seats); });
    recolourLive(card);
    flip(card, p);
  }

  // client, 30 Sep 2026: "it stays in a while for like 5 seconds" — each
  // plan holds the screen for ~5s (4.3s still + the .7s turn) before the
  // next one comes round.
  setInterval(function () { if (!document.hidden) step(); }, 5000);
})();
