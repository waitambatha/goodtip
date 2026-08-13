/* Background video, loaded politely.

   Three rules, all learned from the fact that these are decoration:

   1. Nothing downloads until the banner is near the viewport. A 3MB loop at
      the foot of a page is 3MB most visitors never scroll to, and it competes
      with the content they did ask for.
   2. Nothing downloads on a metered connection or with data-saver on. The
      Network Information API says so, and a background clip is exactly the
      kind of thing that should be skipped.
   3. Nothing downloads under prefers-reduced-motion. The poster stands in and
      is a perfectly good picture.

   The video fades in only once it can actually play, so it never flashes a
   frame of black over the poster. */
(function () {
  'use strict';

  function allowed() {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return false;
    var c = navigator.connection;
    if (c && (c.saveData || /^(slow-)?2g$/.test(c.effectiveType || ''))) return false;
    return true;
  }

  function start(video) {
    var src = video.getAttribute('data-src');
    if (!src || video.dataset.started) return;
    video.dataset.started = '1';
    video.src = src;
    video.addEventListener('canplay', function () { video.classList.add('ready'); }, { once: true });
    // play() rejects on browsers that block autoplay even when muted. The
    // poster is already showing, so there is nothing to recover from.
    var p = video.play();
    if (p && p.catch) p.catch(function () {});
  }

  function init() {
    var vids = document.querySelectorAll('video[data-src]');
    if (!vids.length || !allowed()) return;
    if (!('IntersectionObserver' in window)) { vids.forEach(start); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { start(e.target); io.unobserve(e.target); }
      });
    }, { rootMargin: '200px' });
    vids.forEach(function (v) { io.observe(v); });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
