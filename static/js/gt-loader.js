/* GoodTip sport loader — the branded splash that covers first paint.
 *
 * Previously each base template dismissed the loader on its own timer: 1200ms
 * on auth, 1550ms on the public site, 3000ms in the app. The fill bar animation
 * runs 2.9s, so on two of the three the loader was torn away roughly halfway
 * through its own animation — you'd catch a flash of goal posts and nothing
 * more. This gives every page one honest minimum window (2.9s, matching the
 * bar).
 *
 * One code per load, not a carousel. An earlier version cycled through three
 * or four sports inside the single hold window, which read as flicker — you
 * couldn't settle on any of them. Instead each load shows exactly one sport and
 * the next load shows the next one along, so the variety plays out across
 * visits. The position is kept in localStorage and advanced on load, which
 * beats picking at random: random repeats itself often enough that two or three
 * loads in a row can look identical.
 *
 * The hold applies under prefers-reduced-motion too. A splash screen that sits
 * still isn't the kind of motion that setting exists to suppress, and cutting
 * it to a quarter-second (what this used to do) meant anyone with the setting
 * on never saw the loader at all. The ball bounce and bar fill are already
 * disabled in CSS under that query, so what's left is a still splash.
 *
 * Opt-in attributes on #loader:
 *   data-min="2000"      how long to hold, in ms
 *   data-cycle-sports    pick the code for this load from the list below. Left
 *                        off in the app, where the label is the org's own code
 *                        and showing a different one would be a lie.
 */
(function () {
  'use strict';

  var L = document.getElementById('loader');
  if (!L) return;

  var MIN = parseInt(L.getAttribute('data-min') || '2000', 10);
  var body = document.body;
  var STORE = 'gt-loader-sport';

  var SPORTS = [
    ['oval', 'Aussie Rules'],
    ['oval', 'Rugby League'],
    ['round', 'Football'],
    ['oval', 'NRLW']
  ];

  var ball = L.querySelector('.ball');
  var label = L.querySelector('#loaderSport');

  function paint(sport) {
    if (ball) { ball.classList.remove('oval', 'round'); ball.classList.add(sport[0]); }
    if (label) label.textContent = sport[1];
  }

  /* Which code this load gets: one step on from last time. */
  function nextSport() {
    var i = -1;
    try {
      var stored = parseInt(window.localStorage.getItem(STORE), 10);
      if (!isNaN(stored)) i = stored;
    } catch (e) { /* private mode — fall through to random */ }

    // No history (first visit, or storage blocked): start anywhere so every
    // visitor doesn't open on Aussie Rules.
    i = i < 0 ? Math.floor(Math.random() * SPORTS.length) : (i + 1) % SPORTS.length;

    try { window.localStorage.setItem(STORE, String(i)); } catch (e) { /* ignore */ }
    return SPORTS[i];
  }

  if (L.hasAttribute('data-cycle-sports')) paint(nextSport());

  body.classList.add('loading');

  var started = (window.performance && performance.now) ? performance.now() : Date.now();
  var dismissed = false;

  function dismiss() {
    if (dismissed) return;
    dismissed = true;
    L.classList.add('out');
    body.classList.remove('loading');
    body.classList.add('ready');
    // Matches the .loader.out opacity transition, then take it out of the
    // layout so it can never intercept a click.
    setTimeout(function () { L.style.display = 'none'; }, 600);
  }

  function hold() {
    var now = (window.performance && performance.now) ? performance.now() : Date.now();
    var left = MIN - (now - started);
    setTimeout(dismiss, left > 0 ? left : 0);
  }

  if (document.readyState === 'complete') {
    hold();
  } else {
    // Hold for the minimum *and* until the page has actually finished loading,
    // whichever is later — but never wait forever on a slow third-party asset.
    window.addEventListener('load', hold);
    setTimeout(dismiss, MIN + 4000);
  }
})();
