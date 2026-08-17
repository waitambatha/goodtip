/* Scoped loading veils for htmx requests.
 *
 * The whole-page splash (gt-loader.js) is right for a navigation — you are
 * leaving one page and arriving at another. It is wrong for everything else the
 * app does. Filtering to NRL, stepping to the next round, confirming a slate of
 * tips and creating a group all leave you exactly where you were and change one
 * region of the screen, and covering the entire site to redraw a table tells
 * you the wrong thing about what is happening.
 *
 * What made it worse is that most of these actions previously showed NOTHING.
 * The competition filter fires an htmx request against a remote database ~370ms
 * away; on a slow round trip the page sat completely still, so the only
 * available reading was that the click missed. People click again, and a second
 * click is a second request for work already in flight.
 *
 * So: veil the region that is actually changing, and only that region.
 *
 * HOW THE REGION IS CHOSEN
 * Largest sensible box first, narrowing only if nothing matches:
 *
 *   1. [data-veil] on or above the element that triggered the request — an
 *      explicit opt-in for "this is the box that is busy", used where the
 *      natural target is smaller than the thing a person perceives as loading.
 *      Filtering a ladder swaps the rows; what you watch is the whole table.
 *   2. the htmx target itself, resolved through hx-target.
 *   3. the triggering element.
 *
 * WHY NOT VEIL EVERY REQUEST
 * Two kinds are excluded because a veil would be noise or an active nuisance:
 *   - polling (hx-trigger containing "every"), which is the live score refresh.
 *     Veiling a score card every thirty seconds forever is strobing, not
 *     feedback.
 *   - anything marked [data-veil-skip], for a single tip button that saves
 *     itself and already shows its own tick.
 *
 * A DELAY BEFORE IT APPEARS
 * Nothing shows for the first 180ms. A request that returns in 90ms would
 * otherwise produce a veil that flashes on and straight back off, which reads
 * as a glitch rather than as progress. Below that threshold the swap simply
 * happens, which is what "fast" should look like.
 *
 * The markup and classes are shared with gt-busy.js — same .busy-veil, same
 * .is-busy-scope — so form submissions and htmx requests look identical. There
 * is one loading state in this app, not two that drifted.
 */
(function () {
  'use strict';

  var SHOW_AFTER = 180;      // ms before a veil is worth showing at all
  var pending = new WeakMap();

  /* A ball with a progress ring sweeping around it. The splash uses a ball, so
     the app keeps one visual language for "working" rather than a footy on one
     screen and a plain spinner on the next — but the ball alone only bounced
     in place, with nothing to read as progress. The ring is that part.

     The <i> is the ball; the ring is drawn by the wrapper's pseudo-elements,
     which is why the child is needed at all. */
  function spinner() {
    var wrap = document.createElement('span');
    wrap.className = 'bv-ball busy-run';
    wrap.setAttribute('aria-hidden', 'true');
    wrap.appendChild(document.createElement('i'));
    return wrap;
  }

  function label(el) {
    return el.getAttribute('data-veil-label') || 'Loading';
  }

  /* The box a person would say is loading. */
  function scopeFor(evt) {
    var el = evt.detail && evt.detail.elt;
    if (!el || !el.closest) return null;

    /* data-veil-for wins over everything, including the htmx target.
     *
     * The round arrows and competition chips REPLACE the whole slate panel —
     * status bar, filter, navigator and fixtures — because that is what has to
     * stay consistent. But the thing a reader is watching is the fixture list,
     * and veiling the panel greys out the very controls they just pressed,
     * including the round they are trying to read. Pointing the veil at the
     * fixtures alone keeps the navigator legible while its results load. */
    var named = el.getAttribute && el.getAttribute('data-veil-for');
    if (named) {
      var target = document.querySelector(named);
      if (target) return target;
    }

    var explicit = el.closest('[data-veil]');
    if (explicit) return explicit;

    var target = evt.detail.target;
    // htmx resolves hx-target for us; fall back to the element when it does not
    // (hx-swap="outerHTML" on the trigger itself, for instance).
    if (target && target.nodeType === 1 && target !== document.body) return target;
    return el;
  }

  function isPoll(el) {
    var trigger = el && el.getAttribute && el.getAttribute('hx-trigger');
    return !!trigger && trigger.indexOf('every') !== -1;
  }

  function show(scope, text) {
    if (!scope || scope.querySelector(':scope > .busy-veil')) return;

    var veil = document.createElement('div');
    veil.className = 'busy-veil bv-scoped';
    veil.setAttribute('role', 'status');
    veil.setAttribute('aria-live', 'polite');

    var inner = document.createElement('div');
    inner.className = 'bv-inner';
    inner.appendChild(spinner());

    var caption = document.createElement('span');
    caption.className = 'bv-label';
    caption.textContent = text;          // textContent — a label can never inject markup
    inner.appendChild(caption);

    veil.appendChild(inner);
    scope.classList.add('is-busy-scope');
    scope.appendChild(veil);
  }

  function clear(scope) {
    if (!scope) return;
    var veil = scope.querySelector(':scope > .busy-veil');
    if (veil) veil.remove();
    if (!scope.querySelector('.busy-veil')) scope.classList.remove('is-busy-scope');
  }

  document.body.addEventListener('htmx:beforeRequest', function (evt) {
    var el = evt.detail && evt.detail.elt;
    if (!el || isPoll(el) || (el.closest && el.closest('[data-veil-skip]'))) return;

    var scope = scopeFor(evt);
    if (!scope) return;

    var timer = setTimeout(function () { show(scope, label(el)); }, SHOW_AFTER);
    pending.set(el, { timer: timer, scope: scope });
  });

  function finish(evt) {
    var el = evt.detail && evt.detail.elt;
    var state = el && pending.get(el);
    if (!state) return;
    clearTimeout(state.timer);
    clear(state.scope);
    pending.delete(el);
  }

  /* afterRequest covers success and error alike; the veil must never outlive
     the request that raised it, and a failed request is exactly when someone
     needs the controls back. swapError and timeout are separate events that do
     not always imply afterRequest, so they are caught too. */
  document.body.addEventListener('htmx:afterRequest', finish);
  document.body.addEventListener('htmx:responseError', finish);
  document.body.addEventListener('htmx:sendError', finish);
  document.body.addEventListener('htmx:timeout', finish);

  /* A swap can replace the very node holding the veil, orphaning the class on a
     detached element and leaving a fresh one veiled forever. Sweeping after
     each settle is cheap and covers every such case. */
  document.body.addEventListener('htmx:afterSettle', function () {
    document.querySelectorAll('.bv-scoped').forEach(function (v) {
      var scope = v.parentElement;
      v.remove();
      if (scope && !scope.querySelector('.busy-veil')) {
        scope.classList.remove('is-busy-scope');
      }
    });
  });

  /* ---- same-page navigation ------------------------------------------
   *
   * Not everything that reloads a region is htmx. The competition filter and
   * the round navigator are ordinary links and a GET form, deliberately: they
   * are addressable URLs that work without JavaScript, survive a bookmark and
   * can be opened in a new tab. That is worth keeping.
   *
   * What they lacked was any acknowledgement of the press. The click fires,
   * the browser goes quiet for the length of a round trip — against a database
   * ~370ms away, long enough to doubt it — and only then does the page change.
   * Veiling the region the moment it is pressed says "heard you" without
   * giving up the plain-link behaviour.
   *
   * The veil is not removed on a timer. The navigation replaces the document,
   * which disposes of it; leaving it up until then is the point.
   */
  /* Which region a same-page navigation should veil.
   *
   * data-veil-for is for the common case where the CONTROL is not inside the
   * thing it reloads: the ladder's competition picker sits in the page header
   * while the table it rebuilds is further down. Without it the only reachable
   * scope is the picker itself, so a two-second table rebuild would veil a
   * dropdown and leave the stale table looking perfectly current.
   */
  function navScope(el) {
    var sel = el.getAttribute('data-veil-for');
    if (sel) return document.querySelector(sel);
    return el.closest('[data-veil]');
  }

  /* An htmx-driven control must NOT also go through this path.
   *
   * The round arrows carry both an href (so they work without JavaScript, and
   * can be opened in a new tab) and an hx-get. Running both handlers is not
   * merely redundant, it leaks: the htmx path tracks its veil in `pending` and
   * clears it on afterRequest, while this path fires a bare timeout. A
   * response that lands inside SHOW_AFTER clears nothing — because nothing is
   * showing yet — and the timeout then paints a veil that no event will ever
   * take down, freezing the panel it covers.
   */
  function htmxDriven(el) {
    return el.hasAttribute('hx-get') || el.hasAttribute('hx-post') ||
           el.hasAttribute('data-hx-get') || el.hasAttribute('data-hx-post');
  }

  document.addEventListener('click', function (e) {
    var link = e.target.closest && e.target.closest('a[href]');
    if (!link || htmxDriven(link)) return;
    // Modified clicks open elsewhere — this document is not going anywhere, so
    // veiling it would leave a panel greyed out with nothing coming.
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || link.target === '_blank') return;
    if (link.hasAttribute('data-veil-skip')) return;

    var scope = navScope(link);
    if (!scope) return;
    setTimeout(function () { show(scope, label(scope)); }, SHOW_AFTER);
  });

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || htmxDriven(form)) return;
    if (form.method && form.method.toLowerCase() !== 'get') return;
    var scope = navScope(form);
    if (!scope) return;
    setTimeout(function () { show(scope, label(scope)); }, SHOW_AFTER);
  });

  /* Back/forward can restore a page from cache with a veil still painted on
     it, which looks like a load that never finished. */
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    document.querySelectorAll('.bv-scoped').forEach(function (v) {
      var scope = v.parentElement;
      v.remove();
      if (scope) scope.classList.remove('is-busy-scope');
    });
  });
})();
