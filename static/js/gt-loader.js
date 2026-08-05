/* GoodTip sport loader — the branded splash that covers first paint.
 *
 * Previously each base template dismissed the loader on its own timer: 1200ms
 * on auth, 1550ms on the public site, 3000ms in the app. That was unified to a
 * single 2000ms hold, which fixed the inconsistency and introduced a worse
 * problem: the hold was a floor applied to every page regardless of how fast it
 * was served, so a page ready in 80ms still sat behind the splash for two
 * seconds. The app did not feel polished, it felt slow — and no amount of
 * backend tuning could show through, because the delay was in front of it.
 *
 * The hold is now a flash-guard (400ms) rather than a showcase window, and the
 * bar reports real progress instead of running a fixed 2.9s keyframe to 100%
 * while the page was often still fetching.
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

  var MIN = parseInt(L.getAttribute('data-min') || '400', 10);
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
  var bar = L.querySelector('.loader-bar > i');
  var creep = null;

  function now() {
    return (window.performance && performance.now) ? performance.now() : Date.now();
  }

  /* Move the bar to a real percentage. Nothing else writes to this width, so
     whatever it shows is something that actually happened. */
  function setProgress(pct) {
    if (bar) bar.style.width = Math.max(0, Math.min(100, pct)) + '%';
  }

  /* Between "started" and "load fired" there is no measurable progress to
     report, so the bar eases toward 90% and stops — asymptotic, never
     arriving. It cannot claim 100% before the page is genuinely ready, which
     is the one lie a progress bar must not tell. The curve decelerates, so a
     slow page keeps showing movement without ever looking finished. */
  function startCreep() {
    setProgress(6);
    creep = setInterval(function () {
      var elapsed = now() - started;
      setProgress(90 * (1 - Math.exp(-elapsed / 900)));
    }, 90);
  }

  function stopCreep() {
    if (creep) { clearInterval(creep); creep = null; }
  }

  function dismiss() {
    if (dismissed) return;
    dismissed = true;
    stopCreep();
    setProgress(100);
    L.classList.add('out');
    body.classList.remove('loading');
    body.classList.add('ready');
    // Matches the .loader.out opacity transition, then take it out of the
    // layout so it can never intercept a click.
    setTimeout(function () { L.style.display = 'none'; }, 600);
  }

  /* MIN is a floor against flash, not a target. The old value held every page
     for a flat 2000ms *after* it was ready, so a page served in 80ms still sat
     behind the splash for two seconds and the app felt slow in exactly the way
     an artificial delay makes things feel slow. Now a ready page leaves almost
     at once; the floor only exists so the splash never strobes. */
  /* How long until the ball finishes the shot it is currently taking.
     A page that is ready in 90ms dismisses the splash with the ball a fifth of
     the way through its arc, so the one thing the loader is actually showing
     you — a goal — is the one thing you never see. Waiting out the rest of the
     arc costs at most one cycle, and usually far less, because the longer the
     page took the further along the ball already is.

     Deliberately opt-in per template: it trades a little time for the payoff,
     and that is a call to make per screen rather than everywhere at once. */
  function arcRemaining() {
    if (!L.hasAttribute('data-full-shot') || !ball || !ball.getAnimations) return 0;
    var a = ball.getAnimations()[0];
    if (!a || !a.effect) return 0;
    var d = a.effect.getTiming().duration;
    if (typeof d !== 'number' || !d || typeof a.currentTime !== 'number') return 0;
    var into = a.currentTime % d;
    // Already essentially at the posts — do not wait a whole extra cycle for a
    // sliver of arc nobody would notice was missing.
    return (d - into) > d * 0.94 ? 0 : (d - into);
  }

  function hold() {
    var left = MIN - (now() - started);
    if (left < 0) left = 0;
    var arc = arcRemaining();
    if (arc > left) left = arc;
    // Let the filled bar register before the splash fades, or the last 10%
    // never gets seen.
    setProgress(100);
    setTimeout(dismiss, left > 0 ? left : 120);
  }

  startCreep();

  if (document.readyState === 'complete') {
    hold();
  } else {
    // Hold for the minimum *and* until the page has actually finished loading,
    // whichever is later — but never wait forever on a slow third-party asset.
    window.addEventListener('load', hold);
    setTimeout(dismiss, MIN + 4000);
  }
})();
