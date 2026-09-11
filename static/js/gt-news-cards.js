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
 * A TURN IS HALF A FLIP, NOT A FULL ONE. The card is a front face and a back
 * face on one element. The next story goes on the back, the element turns 180
 * degrees, and the back — a different story — is what faces the reader when
 * it lands; the faces then swap roles so the next turn goes the same way
 * round. The version before this turned a full 360 and had to swap the front
 * face behind the reader's back at the half-way point for anything to have
 * changed at all, which is a lot of machinery to end up facing the way it
 * started.
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
 * A reader whose system asks for reduced motion gets no flip and no automatic
 * picture change — that setting is a request not to be moved at, and a deck
 * that spins anyway is the thing it was switched on to stop.
 *
 * What they get instead is a CROSS-FADE on the dots and the arrows, rather
 * than the instant node swap the old version did. An instant swap plus two
 * faces sharing a grid cell was read, reasonably, as the card "just getting
 * longer": the cell takes the height of the taller face, so the only visible
 * result of a turn was the card changing size.
 *
 * WORTH KNOWING WHEN NOTHING MOVES ON YOUR OWN MACHINE: this is a browser and
 * OS setting, not a site one. Chrome follows the desktop's "reduce motion"
 * switch, and DevTools can force it (Rendering ▸ Emulate CSS media feature
 * prefers-reduced-motion). If the deck is sitting still where you expected it
 * to turn, check that before this file.
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
    if (!still) queue();
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
    var FLIP_MS = 900, STAGGER = 110;

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
                el: el, index: i,
                cards: queue ? Array.prototype.slice.call(queue.content.children) : []
              };
            }
          )
        };
      }
    );
    if (!rows.length) return;

    var dots = Array.prototype.slice.call(deck.querySelectorAll('[data-news-dot]'));
    var at = 0, turn = 0, gen = 0, timer = null, holding = false;

    function cardFor(place, n) {
      if (!place.cards.length) return null;
      return place.cards[n % place.cards.length].cloneNode(true);
    }

    /* Which face is toward the reader. It alternates, so the element turns
     * the same way every time instead of winding back and forth. */
    function faces(place) {
      var spinner = place.el.querySelector('.nd-spinner');
      var flipped = spinner && spinner.classList.contains('is-flipped');
      return {
        spinner: spinner,
        front: place.el.querySelector(flipped ? '.nd-back' : '.nd-front'),
        back: place.el.querySelector(flipped ? '.nd-front' : '.nd-back'),
        flipped: flipped
      };
    }

    function flip(place, n, g) {
      var f = faces(place);
      var next = cardFor(place, n);
      if (!f.spinner || !f.back || !next) return;
      setTimeout(function () {
        if (g !== gen) return;
        // The arriving face is the one that will be facing the reader, so it
        // stops being aria-hidden and the leaving one starts.
        f.back.replaceChildren(next);
        f.back.removeAttribute('aria-hidden');
        f.front.setAttribute('aria-hidden', 'true');
        startShotsIn(f.back);
        f.spinner.classList.toggle('is-flipped');
        setTimeout(function () {
          if (g !== gen) return;
          // Empty the face that is now pointing away, so the deck holds six
          // cards rather than twelve.
          f.front.replaceChildren();
        }, FLIP_MS + 80);
      }, place.index * STAGGER);
    }

    function fade(place, n) {
      var f = faces(place);
      var next = cardFor(place, n);
      if (!f.front || !next) return;
      f.front.replaceChildren(next);
      f.front.classList.remove('nd-fade');
      void f.front.offsetWidth;
      f.front.classList.add('nd-fade');
      startShotsIn(f.front);
    }

    /* One row, one step on. `which` is the row; `n` is the page of stories. */
    function turnRow(row, n) {
      var g = ++gen;
      row.places.forEach(function (place) {
        if (still) fade(place, n);
        else flip(place, n, g);
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
      turnRow(rows[which], at);
      turn++;
      markDots(at);
    }

    /* A dot means "show me page two", so it moves every row at once. */
    function go(n) {
      at = (n + turns) % turns;
      turn = 0;
      rows.forEach(function (row) { turnRow(row, at); });
      markDots(at);
    }

    function stop() { if (timer) { clearInterval(timer); timer = null; } }
    function start() {
      stop();
      if (still || holding || document.hidden || turns < 2) return;
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
