/* The programme: the coming-soon pages' menu, full screen
   (templates/public/partials/_holding_programme.html).
   Opens from the bar's "Menu", closes on Close, Escape, a click on the dark
   margin, or choosing a place. While it is open the page behind does not
   scroll and Tab stays inside it. Pointing at (or tabbing to) a place puts
   its picture in the frame — the only thing that changes without a click,
   and only because the visitor moved to it. */
(function () {
  'use strict';
  var hm = document.querySelector('[data-hm]');
  var opener = document.querySelector('[data-hm-open]');
  if (!hm) return;

  /* Pages not open yet. A real page let through during holding (About,
     Pricing, the blog...) still carries its own links to the rest of the
     site; those go nowhere — the link stays put and says so — instead of
     bouncing the visitor back to the trailer. The server still redirects
     them, for anyone who types the address. */
  var openPaths = (hm.getAttribute('data-hm-paths') || '').split(' ').filter(Boolean);
  function isOpen(a) {
    if (a.origin !== location.origin || !/^https?:$/.test(a.protocol)) return true;
    if (a.hasAttribute('download')) return true;
    var p = a.pathname;
    // "/" is the trailer; "/#start" and the like are sections of the
    // launched home page, which the trailer does not have.
    if (p === '/') return !a.hash || location.pathname === '/';
    if (p === location.pathname) return true;
    return openPaths.some(function (o) { return p.indexOf(o) === 0; });
  }
  var tip = null, tipTimer = 0;
  function sayComingSoon(a) {
    if (!tip) { tip = document.createElement('div'); tip.className = 'hm-soon-tip'; tip.setAttribute('role', 'status'); document.body.appendChild(tip); }
    var r = a.getBoundingClientRect();
    tip.textContent = 'Coming soon';
    tip.style.left = Math.min(Math.max(r.left + r.width / 2, 70), window.innerWidth - 70) + 'px';
    tip.style.top = Math.max(r.top - 8, 44) + 'px';
    tip.classList.add('is-on');
    clearTimeout(tipTimer);
    tipTimer = setTimeout(function () { tip.classList.remove('is-on'); }, 1600);
  }
  if (openPaths.length) Array.prototype.forEach.call(document.querySelectorAll('a[href]'), function (a) {
    if (!isOpen(a)) { a.classList.add('hm-soon'); a.setAttribute('aria-disabled', 'true'); a.title = 'Coming soon'; }
  });
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href]');
    if (!a || !openPaths.length || isOpen(a)) return;
    e.preventDefault(); e.stopPropagation();
    sayComingSoon(a);
  }, true);

  if (!opener) return;

  var items = Array.prototype.slice.call(hm.querySelectorAll('[data-hm-item]'));
  var pics = Array.prototype.slice.call(hm.querySelectorAll('[data-hm-pic]'));
  var url = hm.querySelector('[data-hm-url]');
  var capN = hm.querySelector('[data-hm-capn]'), capT = hm.querySelector('[data-hm-capt]');
  var lastFocus = null;

  function pad(n) { return (n < 9 ? '0' : '') + (n + 1); }
  function feature(i) {
    pics.forEach(function (p, k) { p.classList.toggle('is-on', k === i); });
    items.forEach(function (a, k) { a.classList.toggle('is-lit', k === i); });
    var p = pics[i];
    if (url && p) url.textContent = p.getAttribute('data-url');
    if (capN) capN.textContent = pad(i);
    if (capT && items[i]) capT.textContent = items[i].querySelector('.hm-label').textContent;
  }

  function focusables() {
    return Array.prototype.slice.call(hm.querySelectorAll('a[href], button:not([disabled])'))
      .filter(function (el) { return el.offsetParent !== null; });
  }

  function open() {
    if (!hm.hidden) return;
    lastFocus = document.activeElement;
    hm.hidden = false;
    void hm.offsetWidth;
    hm.classList.add('is-open');
    document.documentElement.classList.add('hm-lock');
    opener.setAttribute('aria-expanded', 'true');
    var here = items.filter(function (a) { return a.classList.contains('is-current'); })[0];
    feature(here ? items.indexOf(here) : 0);
    var first = here || items[0];
    if (first) first.focus({ preventScroll: true });
  }
  function close(returnFocus) {
    if (hm.hidden) return;
    hm.classList.remove('is-open');
    hm.hidden = true;
    document.documentElement.classList.remove('hm-lock');
    opener.setAttribute('aria-expanded', 'false');
    if (returnFocus !== false && lastFocus && lastFocus.focus) lastFocus.focus({ preventScroll: true });
  }

  opener.addEventListener('click', open);
  Array.prototype.forEach.call(hm.querySelectorAll('[data-hm-close]'), function (b) {
    b.addEventListener('click', function () { close(); });
  });
  hm.addEventListener('click', function (e) {
    if (e.target === hm) { close(); return; }
    // Choosing a place: let the link do its work (a real page, a tour's hash,
    // or the waiting-list pop-up), with the programme out of its way.
    var a = e.target.closest('a[href]');
    if (a) close(false);
  });
  items.forEach(function (a, i) {
    a.addEventListener('mouseenter', function () { feature(i); });
    a.addEventListener('focus', function () { feature(i); });
  });
  document.addEventListener('keydown', function (e) {
    if (hm.hidden) return;
    if (e.key === 'Escape') { e.preventDefault(); close(); return; }
    if (e.key === 'Tab') {
      var f = focusables();
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
})();
