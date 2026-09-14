/* The dashboard's news deck, and the pictures turning inside its cards.
 *
 * Two things that have to know about each other, which is why they are one
 * file: a deck turn CLONES a card out of a <template> and puts it on the
 * page, and the clone's own slideshow has to start itself. A script that ran
 * once over the six cards that happened to be there at load would leave every
 * card after the first turn frozen on its first picture.
 *
 * ---------------------------------------------------------------------------
 * THE DECK
 * ---------------------------------------------------------------------------
 * Six places in two rows. One clock; each tick turns ONE row, and the rows
 * take it in turns — top, bottom, top — so each row changes every two ticks
 * and something on the deck is always moving without all six cards moving at
 * once. The client: "so the user turns, then the lower one turns, so it
 * should happen alternating".
 *
 * FIVE WAYS TO TURN (Sep 2026, client: "lets have like 5 ways or design that
 * the cards will be changing"). Each place is two faces in one grid cell; the
 * one showing carries .is-up. A turn puts the next story on the other face
 * and plays a pair of keyframe animations, one leaving and one arriving,
 * chosen by data-turn on the place: flip, tumble, slide, zoom, iris. The deck
 * takes them in order, one per tick, so half a minute of watching shows every
 * one. The movements live in goodtip.css under "the five turns"; LASTS below
 * has to agree with their durations, because it is when the leaving face is
 * emptied.
 *
 * Four things it still has to get right:
 *
 *   * IT MUST STOP WHEN SOMEBODY IS READING IT. Hover and keyboard focus both
 *     hold it. There is no pause button ("it should all be automatic"), so
 *     these are the only brakes — and focus matters twice over, because a turn
 *     replaces card nodes and would take the focused link out from under the
 *     keyboard.
 *
 *   * IT MUST NOT MOVE WHEN THE TAB IS HIDDEN, or the reader comes back to a
 *     deck that has turned a few hundred times behind their back.
 *
 *   * IT MUST RESPECT prefers-reduced-motion — see REDUCED MOTION below.
 *
 *   * A TURN STARTED WHILE ANOTHER IS STILL LANDING MUST WIN. Every turn bumps
 *     `gen`, and the delayed steps of an older turn check it and stand down.
 *
 * ---------------------------------------------------------------------------
 * REDUCED MOTION
 * ---------------------------------------------------------------------------
 * The deck STILL CHANGES every five seconds for a reader whose system asks
 * for reduced motion, and the pictures inside a card still change too. It
 * used to stop dead instead, and on a machine with that setting on — the
 * client's own development machine among them — the deck never moved at all,
 * which read as the feature not existing. Changing what a card shows is not
 * motion; what the setting asks to be spared is things travelling across the
 * screen. So under it every turn is a short cross-fade ("fade" below) and
 * nothing flips, slides or zooms: the rule the site's other rotating
 * pictures already follow.
 *
 * WORTH KNOWING WHEN YOU ONLY EVER SEE A FADE: that is this setting. Chrome
 * follows the desktop's "reduce motion" switch, and DevTools can force it
 * either way (Rendering ▸ Emulate CSS media feature prefers-reduced-motion).
 */
(function () {
  'use strict';

  var still = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------------------------------------------------------------------
   * The pictures inside one card
   * ------------------------------------------------------------------ */

  /* Turning every three seconds, per the client: "in that place we show the
   * images it must be changing in like 3 seconds". A video frame plays muted
   * and is given its own length rather than being cut off at three seconds —
   * up to a ceiling, because a card is not where a 45-second clip should hold
   * the deck up. */
  var VIDEO_MAX_MS = 12000;

  function startShots(box) {
    if (!box || box.dataset.gtLive === '1') return;
    var shots = Array.prototype.slice.call(box.querySelectorAll('[data-shot]'));
    if (shots.length < 2) return;
    box.dataset.gtLive = '1';

    var pips = Array.prototype.slice.call(box.querySelectorAll('.ndc-pips i'));
    var every = parseInt(box.getAttribute('data-every'), 10) || 3000;
    var at = 0, timer = null, dead = false;

    function show(n) {
      at = (n + shots.length) % shots.length;
      shots.forEach(function (s, k) {
        var on = k === at;
        s.classList.toggle('on', on);
        var v = s.querySelector('video');
        if (!v) return;
        // Only the frame on screen plays. Six cards each running every clip
        // they hold would be a dashboard decoding thirty videos at once.
        if (on) { var q = v.play(); if (q && q.catch) q.catch(function () {}); }
        else { try { v.pause(); v.currentTime = 0; } catch (e) {} }
      });
      pips.forEach(function (p, k) { p.classList.toggle('on', k === at); });
    }

    function next() {
      if (dead) return;
      show(at + 1);
      queue();
    }

    function queue() {
      clearTimeout(timer);
      if (dead) return;
      var v = shots[at].querySelector('video');
      var wait = every;
      if (v && isFinite(v.duration) && v.duration > 0) {
        wait = Math.min(v.duration * 1000 + 250, VIDEO_MAX_MS);
      }
      timer = setTimeout(next, wait);
    }

    /* A card that has been cloned away and dropped stops on its own. Without
     * this every turn would leave another six timers running against nodes
     * that are no longer in the document, for as long as the tab is open. */
    function reap() {
      if (box.isConnected) return;
      dead = true;
      clearTimeout(timer);
      clearInterval(watch);
    }
    var watch = setInterval(reap, 5000);

    show(0);
    // Under reduced motion as well — see REDUCED MOTION above. The frames
    // cross-fade in place; the slow zoom is switched off in the CSS.
    queue();
  }

  function startShotsIn(root) {
    if (!root) return;
    if (root.matches && root.matches('[data-card-shots]')) startShots(root);
    (root.querySelectorAll ? root.querySelectorAll('[data-card-shots]') : [])
      .forEach(startShots);
  }

  /* ---------------------------------------------------------------------
   * The deck
   * ------------------------------------------------------------------ */

  function deckUp(deck) {
    var turns = parseInt(deck.getAttribute('data-turns'), 10) || 1;
    var every = parseInt(deck.getAttribute('data-interval'), 10) || 5000;
    var STAGGER = 110;
    var STYLES = ['flip', 'tumble', 'slide', 'zoom', 'iris'];
    // How long each turn runs, in ms — must match goodtip.css.
    var LASTS = { flip: 860, tumble: 860, slide: 700, zoom: 750, iris: 800, fade: 400 };

    var rows = Array.prototype.map.call(
      deck.querySelectorAll('[data-news-row]'),
      function (rowEl) {
        return {
          el: rowEl,
          places: Array.prototype.map.call(
            rowEl.querySelectorAll('[data-news-place]'),
            function (el, i) {
              var queue = el.querySelector('template[data-news-queue]');
              return {
                el: el, index: i, gen: 0,
                cards: queue ? Array.prototype.slice.call(queue.content.children) : []
              };
            }
          )
        };
      }
    );
    if (!rows.length) return;

    var dots = Array.prototype.slice.call(deck.querySelectorAll('[data-news-dot]'));
    var at = 0, turn = 0, styleAt = 0, timer = null, holding = false;

    function cardFor(place, n) {
      if (!place.cards.length) return null;
      return place.cards[n % place.cards.length].cloneNode(true);
    }

    /* Finish whatever turn is still landing, at once. A dot pressed half way
     * through a turn must not leave two stories showing, and the face that is
     * down is emptied so the deck holds six cards rather than twelve. */
    function settle(place) {
      Array.prototype.forEach.call(place.el.querySelectorAll('.nd-face'), function (f) {
        f.classList.remove('is-leaving', 'is-arriving');
        if (!f.classList.contains('is-up')) f.replaceChildren();
      });
      place.el.classList.remove('is-turning');
    }

    /* One place, one story on, in one of the five movements. */
    function turnPlace(place, n, style, delay) {
      var next = cardFor(place, n);
      if (!next) return;
      // A newer turn of this place stands the older one down.
      var g = ++place.gen;
      setTimeout(function () {
        if (g !== place.gen) return;
        settle(place);
        var up = place.el.querySelector('.nd-face.is-up');
        var down = place.el.querySelector('.nd-face:not(.is-up)');
        if (!up || !down) return;
        down.replaceChildren(next);
        startShotsIn(down);
        place.el.setAttribute('data-turn', style);
        place.el.classList.add('is-turning');
        // The arriving face is the one that will be facing the reader, so it
        // stops being aria-hidden and the leaving one starts.
        up.classList.remove('is-up');
        up.classList.add('is-leaving');
        up.setAttribute('aria-hidden', 'true');
        down.classList.add('is-up', 'is-arriving');
        down.removeAttribute('aria-hidden');
        setTimeout(function () { if (g === place.gen) settle(place); }, LASTS[style] + 60);
      }, delay);
    }

    function nextStyle() {
      var style = STYLES[styleAt % STYLES.length];
      styleAt++;
      return style;
    }

    /* One row, one step on, every place in it the same movement, a beat
     * apart. `n` is the page of stories. */
    function turnRow(row, n, style) {
      row.places.forEach(function (place) {
        turnPlace(place, n, still ? 'fade' : style, place.index * STAGGER);
      });
    }

    function markDots(n) {
      dots.forEach(function (d, k) {
        d.classList.toggle('on', k === n);
        if (k === n) d.setAttribute('aria-current', 'true');
        else d.removeAttribute('aria-current');
      });
    }

    /* THE ALTERNATION. Each tick moves one row on to the next story. The page
     * number only advances once every row has caught up with it, so the dots
     * keep meaning "which page the deck is showing" rather than getting ahead
     * of half the cards on screen. */
    function tick() {
      var which = turn % rows.length;
      if (which === 0) at = (at + 1) % turns;
      turnRow(rows[which], at, nextStyle());
      turn++;
      markDots(at);
    }

    /* A dot means "show me page two", so it moves every row at once. */
    function go(n) {
      at = (n + turns) % turns;
      turn = 0;
      var style = nextStyle();
      rows.forEach(function (row) { turnRow(row, at, style); });
      markDots(at);
    }

    function stop() { if (timer) { clearInterval(timer); timer = null; } }
    function start() {
      stop();
      // Not `still`: the deck changes under reduced motion too, as a fade.
      if (holding || document.hidden || turns < 2) return;
      timer = setInterval(tick, every);
    }

    dots.forEach(function (d) {
      d.addEventListener('click', function () {
        go(parseInt(d.getAttribute('data-news-dot'), 10) || 0);
        /* A fresh interval after a press, or the next turn could arrive a
           moment after the one that was asked for. */
        start();
      });
    });
    ['mouseenter', 'focusin'].forEach(function (ev) {
      deck.addEventListener(ev, function () { holding = true; stop(); });
    });
    ['mouseleave', 'focusout'].forEach(function (ev) {
      deck.addEventListener(ev, function (e) {
        // focusout fires moving between two cards inside the deck, too.
        if (ev === 'focusout' && e.relatedTarget && deck.contains(e.relatedTarget)) return;
        holding = false; start();
      });
    });
    document.addEventListener('visibilitychange', start);
    start();
  }

  function boot() {
    startShotsIn(document);
    var deck = document.querySelector('[data-news-deck]');
    if (deck) deckUp(deck);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
