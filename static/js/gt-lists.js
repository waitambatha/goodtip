/* GoodTip lists — self-scrolling regions with scoped loading state.
 *
 * Two jobs, both about keeping a long list from taking the whole page with it:
 *
 * 1. Scroll affordance. A capped, scrollable list gives no clue that there's
 *    more below, so .gt-listwrap paints a fade at its bottom edge; this removes
 *    the fade once you've reached the end, and hides it entirely when the
 *    content is short enough not to scroll at all.
 *
 * 2. Scoped busy state. htmx's default indicator convention dims whatever you
 *    point it at; here every request that targets a list marks that list
 *    [data-busy] instead, so a filter change or a search spins inside the list
 *    and the header, stats and sidebar stay put and readable.
 */
(function () {
  'use strict';

  function wrapOf(el) {
    return el && el.closest ? el.closest('.gt-listwrap') : null;
  }

  /* ---------------- scroll-end fade ---------------- */
  function syncFade(scroller) {
    var wrap = wrapOf(scroller);
    if (!wrap) return;
    // 2px of slack: sub-pixel layout means scrollTop often lands just shy.
    var atEnd = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2;
    var noScroll = scroller.scrollHeight <= scroller.clientHeight + 2;
    wrap.classList.toggle('at-end', atEnd || noScroll);
  }

  function bind(scroller) {
    if (scroller.dataset.gtBound) return;
    scroller.dataset.gtBound = '1';
    scroller.addEventListener('scroll', function () { syncFade(scroller); }, { passive: true });
    syncFade(scroller);

    // Content can change height without a scroll event — an htmx swap, a
    // details/summary opening, images loading.
    if ('ResizeObserver' in window) {
      new ResizeObserver(function () { syncFade(scroller); }).observe(scroller);
    }
  }

  function scan(root) {
    (root || document).querySelectorAll('.gt-scroll').forEach(bind);
  }

  /* ---------------- scoped busy state ---------------- */
  function markBusy(evt, on) {
    // Prefer the element the response is going to replace; fall back to the
    // element that triggered it, which covers a filter link inside the list.
    var target = evt.detail && (evt.detail.target || evt.detail.elt);
    var wrap = wrapOf(target) || wrapOf(evt.target);
    if (!wrap) return;
    if (on) wrap.setAttribute('data-busy', '');
    else wrap.removeAttribute('data-busy');
  }

  document.addEventListener('htmx:beforeRequest', function (e) { markBusy(e, true); });
  document.addEventListener('htmx:afterRequest', function (e) { markBusy(e, false); });
  document.addEventListener('htmx:afterSwap', function (e) {
    markBusy(e, false);
    scan(e.detail && e.detail.target);
    scan(document);
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { scan(document); });
  } else {
    scan(document);
  }

  window.addEventListener('resize', function () {
    document.querySelectorAll('.gt-scroll').forEach(syncFade);
  });
})();
