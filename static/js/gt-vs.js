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

  /* Phones (22 Nov 2026): the block is five screens of stacked pairs, so a
     whole-block stagger left the reader scrolling past blank space. There
     each pair plays on its own as it comes up the screen (.vs-in), and plays
     again if it is scrolled away and back. */
  if (window.matchMedia('(max-width: 760px)').matches) {
    vs.classList.add('vs-rows');
    var rowIO = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting && e.intersectionRatio >= 0.35) e.target.classList.add('vs-in');
        else if (!e.isIntersecting) e.target.classList.remove('vs-in');
      });
    }, { threshold: [0, 0.35] });
    vs.querySelectorAll('.vs-row').forEach(function (r) { rowIO.observe(r); });
    return;
  }
  new IntersectionObserver(function (entries) {
    var e = entries[entries.length - 1];
    if (e.intersectionRatio >= 0.25) vs.classList.add('vs-play');
    else if (!e.isIntersecting) vs.classList.remove('vs-play');
  }, { threshold: [0, 0.25] }).observe(vs);
})();
