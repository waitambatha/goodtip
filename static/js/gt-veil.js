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

  /* WHEN A VEIL APPEARS, AND HOW LONG IT STAYS.
   *
   * SHOW_AFTER is the flash guard: below it the swap simply happens, which is
   * what "fast" should look like. It came down from 180ms because at that
   * threshold a good connection never saw a scoped loader at all, and the
   * client asked to be able to see them (20 Sep 2026).
   *
   * MIN_SHOW is the other half of the same problem, and without it lowering
   * SHOW_AFTER would make things worse: a response landing at 130ms would put
   * a veil up and take it down two frames later, which reads as a glitch
   * rather than as progress. Once one is up it stays up for long enough to be
   * read as deliberate. */
  var SHOW_AFTER = 110;      // ms before a veil is worth showing at all
  var MIN_SHOW = 520;        // ms it stays once it has appeared
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

  /* ---- FOUR LOADERS PER COMPETITION, AND YOU GET A DIFFERENT ONE EACH TIME
   *
   * CLIENT, 20 SEP 2026, correcting the first attempt: "when you log in we
   * have filter buttons in pages like ladder, leaderboard — some are for
   * organisations, some are for groups, some are for competition. That is when
   * I said let each competition have like 4 types of loaders with its own type
   * of animation, so when I click NRL to see maybe the leaderboard, that small
   * section that is loading should have the different loader. And the reason I
   * said 4 in each competition is that if I have a certain loader I have seen
   * in NRL, if I go to NRL again I should see another type of design and
   * animation."
   *
   * The first pass read "four" as four kinds of DATA — one shape for fixtures,
   * one for a ladder and so on — so pressing NRL twice gave you the same
   * loader twice, which is the one thing the sentence rules out. The four are
   * a ROTATION, and the position is kept per competition: NRL walks kick →
   * climb → score → sweep → kick, and AFLW has its own place in the cycle.
   *
   * WHICH SHAPE and WHICH COLOUR are separate questions, which is what keeps
   * this at four shapes rather than twenty-four. The shape is the rotation;
   * the colour is the competition's own token, read off the [data-code] the
   * pressed control already carries. Add a competition tomorrow and it has all
   * four the day it gets a colour.
   *
   * Stepping rather than random, for the same reason the splash steps: random
   * gives you the same one three presses running, and three identical loaders
   * in a row is exactly what the client is complaining about.
   *
   * Every animated node carries `busy-run` on the ELEMENT. The blanket
   * reduced-motion rule kills animation on anything without it, and a class on
   * a wrapper does not reach the children inside it.
   */
  var SHAPES = ['kick', 'climb', 'score', 'sweep'];
  var SHAPE_STORE = 'gt-veil-shape:';

  function nextShape(code) {
    var key = SHAPE_STORE + (code || 'all');
    var i = -1;
    try {
      var stored = parseInt(window.localStorage.getItem(key), 10);
      if (!isNaN(stored)) i = stored;
    } catch (e) { /* storage blocked — fall through to a random start */ }
    i = i < 0 ? Math.floor(Math.random() * SHAPES.length) : (i + 1) % SHAPES.length;
    try { window.localStorage.setItem(key, String(i)); } catch (e) { /* ignore */ }
    return SHAPES[i];
  }

  function shapeNode(shape) {
    var wrap = document.createElement('span');
    wrap.setAttribute('aria-hidden', 'true');

    if (shape === 'climb') {
      /* Four bars finding their order. Deliberately NOT four bars growing in
         step — a ladder is about which is above which, so they overtake. A row
         pulsing together is an equaliser, and an equaliser says "audio". */
      wrap.className = 'bv-climb';
      wrap.innerHTML =
        '<i class="busy-run"></i><i class="busy-run"></i>' +
        '<i class="busy-run"></i><i class="busy-run"></i>';
      return wrap;
    }
    if (shape === 'score') {
      /* A scoreline settling: two blocks turning over with the dash between
         them held still, so the eye has one fixed point. Scaling on Y rather
         than sliding digits — a number you cannot read beats a wrong one. */
      wrap.className = 'bv-score';
      wrap.innerHTML =
        '<i class="bv-score-n busy-run"></i><b>&ndash;</b>' +
        '<i class="bv-score-n bv-score-b busy-run"></i>';
      return wrap;
    }
    if (shape === 'sweep') {
      /* Four chevrons chasing across, each picking up where the last left
         off. The only one of the four that travels, which is what makes it
         read as different from the kick rather than as a smaller version. */
      wrap.className = 'bv-sweep';
      wrap.innerHTML =
        '<i class="busy-run"></i><i class="busy-run"></i>' +
        '<i class="busy-run"></i><i class="busy-run"></i>';
      return wrap;
    }
    /* kick — a ball over a set of posts, in miniature. The default, and the
       one that ties the small loaders to the splash. */
    wrap.className = 'bv-kick';
    wrap.innerHTML =
      '<i class="bv-kick-posts busy-run"></i><b class="bv-kick-ball busy-run"></b>';
    return wrap;
  }

  /* Which competition's colour this veil wears.
   *
   * data-veil-code is the explicit answer, for a control that is not itself
   * inside the thing it is fetching. Otherwise the nearest [data-code] above
   * the trigger — which on the filter chips IS the trigger. Nothing found
   * means no code, and the veil keeps the app's own green, which is right for
   * "all competitions": it is not a competition and should not borrow one. */
  function codeFor(el) {
    var named = el.getAttribute && el.getAttribute('data-veil-code');
    if (named) return named;
    var host = el.closest && el.closest('[data-code]');
    return host ? host.getAttribute('data-code') : '';
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

  function show(scope, text, shape, code) {
    if (!scope || scope.querySelector(':scope > .busy-veil')) return;

    var veil = document.createElement('div');
    veil.className = 'busy-veil bv-scoped';
    veil.setAttribute('role', 'status');
    veil.setAttribute('aria-live', 'polite');
    /* The competition's token, on the veil itself. Everything inside draws in
       var(--code), so one attribute colours the whole thing and no shape ever
       names a colour. */
    if (code) veil.setAttribute('data-code', code);

    /* A SMALL REGION GETS THE SHAPE AND NOT THE SENTENCE.
     *
     * The veil carries a caption because on a fixture list or a ladder it is
     * worth saying what is being fetched. On a reaction chip it is not: the
     * scope is forty pixels across, and "LOADING" laid over it is a word
     * wider than the thing it is describing. Below the threshold the label is
     * dropped and the animation alone reports the wait, which is all the room
     * there is and all the information a toggle needs. */
    var at = scope.getBoundingClientRect();
    var tight = at.width < 220 || at.height < 90;
    if (tight) veil.setAttribute('data-veil-compact', '');

    var inner = document.createElement('div');
    inner.className = 'bv-inner';
    inner.appendChild(shapeNode(shape));

    if (!tight) {
      var caption = document.createElement('span');
      caption.className = 'bv-label';
      caption.textContent = text;        // textContent — a label can never inject markup
      inner.appendChild(caption);
    }

    veil.appendChild(inner);
    veil.dataset.shownAt = String(Date.now());
    scope.classList.add('is-busy-scope');
    scope.appendChild(veil);
  }

  function clear(scope) {
    if (!scope) return;
    var veil = scope.querySelector(':scope > .busy-veil');
    if (!veil) {
      if (!scope.querySelector('.busy-veil')) scope.classList.remove('is-busy-scope');
      return;
    }
    /* Held for the rest of MIN_SHOW where the answer beat it. See the note on
       the constant: a veil that appears and vanishes inside a couple of frames
       is read as a glitch, not as a load. */
    var shown = parseInt(veil.dataset.shownAt || '0', 10);
    var left = MIN_SHOW - (Date.now() - shown);
    var drop = function () {
      veil.remove();
      if (!scope.querySelector('.busy-veil')) scope.classList.remove('is-busy-scope');
    };
    if (left > 0) setTimeout(drop, left); else drop();
  }

  document.body.addEventListener('htmx:beforeRequest', function (evt) {
    var el = evt.detail && evt.detail.elt;
    if (!el || isPoll(el) || (el.closest && el.closest('[data-veil-skip]'))) return;

    var scope = scopeFor(evt);
    if (!scope) return;

    var code = codeFor(el);
    /* Advanced at REQUEST time, not at paint time. A fast round trip never
       paints a veil at all, and if it did not move the cycle on, a run of
       quick presses would leave the next slow one showing the shape you last
       actually saw. */
    var shape = nextShape(code);
    var timer = setTimeout(function () { show(scope, label(el), shape, code); }, SHOW_AFTER);
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
      /* A veil whose scope was replaced by the swap is already off the screen
         — the new content is in its place — so it goes at once. Holding it for
         MIN_SHOW would be holding a node nobody can see.

         One still attached to a live region is a different thing: it IS what
         the reader is looking at, and it gets the same minimum as every other
         veil rather than being yanked a frame after it appeared. */
      if (!scope || !document.body.contains(scope)) {
        v.remove();
        if (scope) scope.classList.remove('is-busy-scope');
        return;
      }
      clear(scope);
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
    var code = codeFor(link);
    var shape = nextShape(code);
    setTimeout(function () { show(scope, label(scope), shape, code); }, SHOW_AFTER);
  });

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || htmxDriven(form)) return;
    if (form.method && form.method.toLowerCase() !== 'get') return;
    var scope = navScope(form);
    if (!scope) return;
    var fcode = codeFor(form);
    var fshape = nextShape(fcode);
    setTimeout(function () { show(scope, label(scope), fshape, fcode); }, SHOW_AFTER);
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
