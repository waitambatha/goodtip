/* The home page's "Right now / With GoodTip" comparison.
 *
 * Adds .vs-play while the block is on screen and takes it off once it has left
 * completely, so the rows play again every time it is scrolled back to (down,
 * up, and down again). Reduced motion never hides the rows in the first place,
 * see the .vs rules in goodtip.css.
 */
(function () {
  'use strict';
  var vs = document.querySelector('[data-vs]');
  if (!vs) return;
  if (!('IntersectionObserver' in window)) { vs.classList.add('vs-play'); return; }
  new IntersectionObserver(function (entries) {
    var e = entries[entries.length - 1];
    if (e.intersectionRatio >= 0.25) vs.classList.add('vs-play');
    else if (!e.isIntersecting) vs.classList.remove('vs-play');
  }, { threshold: [0, 0.25] }).observe(vs);
})();
