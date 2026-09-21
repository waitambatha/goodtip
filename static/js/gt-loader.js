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
 * SIX SCENES, NOT SIX CAPTIONS (client, 20 Sep 2026: "add more loaders, like
 * 6, strictly related to the NRL, NRLW, AFL, AFLW ... so we do not have the
 * one that we know, and they will be loading randomly so we can have all of
 * them"). Until now there was ONE picture — a ball kicked through some posts —
 * and the variety was the word underneath it changing. Now each competition
 * has a scene of a thing that only happens in that competition, in that
 * competition's own colour: see templates/partials/_loader_scenes.html.
 *
 * "Randomly so we can have all of them" is the requirement, and stepping is
 * what actually delivers it. Random gives you AFL three loads running and the
 * netball one never; one step along the list guarantees a person sees all six
 * inside six visits, which is what the sentence is asking for.
 *
 * All this function does is write the key onto #loader. The scene shows
 * because CSS matches [data-scene], and it is coloured because [data-code]
 * inherits the same tokens the rest of the product uses for that competition.
 * Nothing here knows a hex value.
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

  /* The org-creation wizard is one page POSTing to itself on every step, so
     this splash would replay full-screen after every Continue — right after
     gt-busy.js's own small veil already said "checking your code" or
     "saving" for that exact action. Two loaders for one click reads as the
     app being unsure whether it is done, not as two things happening.
     Skipped outright, before any timer starts, rather than dismissed early:
     an early dismiss would still paint one visible frame of the full-screen
     splash on every step. */
  /* no-page-loader: the same early exit, for a page that asks for it. The
     Wall is the first — it works like a chat, and moving between its rooms
     should feel like changing conversation, not like opening a new screen
     (client: "when we click it ... remove that loader"). */
  if (document.body.classList.contains('wiz-page') ||
      document.body.classList.contains('no-page-loader')) {
    L.style.display = 'none';
    return;
  }

  var MIN = parseInt(L.getAttribute('data-min') || '400', 10);
  /* The most data-full-shot may add on top of MIN. See arcRemaining. */
  var MAX_ARC_WAIT = 1100;
  var body = document.body;
  var STORE = 'gt-loader-sport';

  /* THE EIGHT, in the order somebody stepping through them meets them.
   *
   * Client, 20 Sep 2026: "let's only have NRL and AFL ... they will be like 8
   * loaders. For the NRLW, either male or female, on their loader we can add
   * the sign of male or female."
   *
   * [scene, code, sex, label]. The SCENE is the animation, the CODE is the
   * colour, and the SEX is the ♀/♂ mark in the corner — three separate things,
   * which is what keeps this at eight drawings instead of sixteen. An AFLW set
   * shot and an AFL set shot are the same act; what differs is whose
   * competition it is, and that is exactly what a colour and a mark are for.
   *
   * Ordered so the code alternates every load and the sex alternates every
   * two: AFL, NRL, AFLW, NRLW, and round again with the other four scenes.
   * Each of the four competitions therefore comes up twice in a full cycle,
   * wearing a different animation each time — which is the answer to "if I go
   * to NRL again I should see another type of design".
   *
   * WHAT CAME OUT. Super Netball and Super League were in the old six because
   * they are rows in the Series table; neither has a single round or fixture
   * in the database, so the splash was advertising competitions the product
   * does not run — and a round ball dropping through a ring reads as
   * basketball long before it reads as netball. State of Origin went too: it
   * has fixtures, but it is three games a year inside the NRL, and the same
   * client asked a week earlier that it stop being treated as a competition in
   * its own right. */
  var SCENES = [
    ['afl-set',     'afl',  'm', 'AFL'],
    ['nrl-convert', 'nrl',  'm', 'NRL'],
    ['afl-bounce',  'aflw', 'w', 'AFLW'],
    ['nrl-count',   'nrlw', 'w', 'NRLW'],
    ['afl-mark',    'afl',  'm', 'AFL'],
    ['nrl-try',     'nrl',  'm', 'NRL'],
    ['afl-flags',   'aflw', 'w', 'AFLW'],
    ['nrl-pass',    'nrlw', 'w', 'NRLW']
  ];

  var label = L.querySelector('#loaderSport');

  /* THE SLIM BAR HAS NO SCENE TO PAINT. Inside the member app the loader is
     three pixels of progress and nothing else (see .loader-slim), so there is
     no stage, no caption and nothing for the rotation to choose between. It
     still runs the same progress and dismissal below — that half is the same
     loader — it simply skips the part about competitions. */
  var slim = L.classList.contains('loader-slim');

  function paint(scene) {
    L.setAttribute('data-scene', scene[0]);
    L.setAttribute('data-code', scene[1]);
    L.setAttribute('data-sex', scene[2]);
    if (label) label.textContent = scene[3];
  }

  /* One step on from last time, within whichever list we are cycling. */
  function nextFrom(list) {
    var i = -1;
    try {
      var stored = parseInt(window.localStorage.getItem(STORE), 10);
      if (!isNaN(stored)) i = stored;
    } catch (e) { /* private mode — fall through to a random start */ }

    // No history (first visit, or storage blocked): start anywhere, so every
    // visitor does not open on the same scene.
    i = i < 0 ? Math.floor(Math.random() * list.length) : (i + 1) % list.length;

    try { window.localStorage.setItem(STORE, String(i)); } catch (e) { /* ignore */ }
    return list[i];
  }

  /* Inside the member app the CODE is not cycled: the template names the
     organisation's own, and showing an NRL comp the AFL loader on the first
     screen of every page is a small lie repeated all day. The SCENE still
     rotates, within that code — so a member sees their own competition and
     still gets variety, which is what the first version of this got wrong in
     the other direction by pinning them to one picture.

     data-scene-fixed carries a code ("afl" / "nrl"), not a scene. Anything we
     hold no scenes for falls through to the full eight rather than to a blank
     stage. */
  if (!slim) {
    var fixed = L.getAttribute('data-scene-fixed') || '';
    var mine = SCENES.filter(function (s) { return s[1] === fixed; });
    paint(nextFrom(mine.length ? mine : SCENES));
  }

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

  /* MIN IS A FLOOR, AND IT IS NOW A DELIBERATE ONE.
   *
   * It was cut to 400ms because a flat two-second hold on every page made a
   * fast app feel slow — an artificial delay in front of a backend nobody
   * could then tune their way out of. That reasoning still holds for a page
   * somebody is passing THROUGH.
   *
   * What it got wrong is that the splash is also the only branded moment in
   * the product, and at 400ms on a warm cache it was gone before the shot had
   * left the boot. Client, 20 Sep 2026: "also give time for the loaders to
   * load so we can see the loader."
   *
   * So the floor is per template and is set where the trade actually falls:
   * longest on the public site, where the loader is doing marketing work and
   * a visitor has just arrived; shortest inside the member app, where the
   * same person sees it twenty times a day and it is pure cost. Paired with
   * data-full-shot, which waits out the rest of the current animation cycle
   * rather than cutting a goal off half way — the one thing the loader is
   * showing you should not be the one thing you never see. */
  /* How long until the ball finishes the shot it is currently taking.
     A page that is ready in 90ms dismisses the splash with the ball a fifth of
     the way through its arc, so the one thing the loader is actually showing
     you — a goal — is the one thing you never see. Waiting out the rest of the
     arc costs at most one cycle, and usually far less, because the longer the
     page took the further along the ball already is.

     Deliberately opt-in per template: it trades a little time for the payoff,
     and that is a call to make per screen rather than everywhere at once. */
  /* How long until the scene on screen finishes the cycle it is part-way
   * through.
   *
   * IT HAS TO ASK THE SCENE THAT IS SHOWING, and the first version did not.
   * It took the first `.lball` in the document, which is the AFL one on every
   * page, because the five it is not showing are hidden with `display: none`
   * rather than with the `hidden` attribute. Hidden that way they run no
   * animations at all, so the answer was either the wrong scene's timing or —
   * for State of Origin, which has no ball, being a shield that draws itself —
   * nothing at all, and data-full-shot quietly did nothing on five loaders out
   * of six.
   *
   * So: find the scene by the key on #loader, and take the LONGEST remaining
   * cycle across everything animating inside it. Longest rather than first,
   * because these scenes are two and three animations played together — the
   * ball and the flags, the tally and the stroke through it — and cutting at
   * the shortest of them stops the picture half-told.
   */
  function arcRemaining() {
    if (!L.hasAttribute('data-full-shot')) return 0;
    var key = L.getAttribute('data-scene');
    var scene = key && L.querySelector('.lscene[data-lscene="' + key + '"]');
    if (!scene || !scene.querySelectorAll) return 0;

    var longest = 0;
    var nodes = scene.querySelectorAll('*');
    for (var i = 0; i < nodes.length; i++) {
      if (!nodes[i].getAnimations) return 0;   // no Web Animations here
      var list = nodes[i].getAnimations();
      for (var j = 0; j < list.length; j++) {
        var a = list[j];
        if (!a.effect || typeof a.currentTime !== 'number') continue;
        var d = a.effect.getTiming().duration;
        if (typeof d !== 'number' || !d) continue;
        var left = d - (a.currentTime % d);
        // Already essentially finished — do not wait a whole extra cycle for a
        // sliver nobody would notice was missing.
        if (left > d * 0.94) continue;
        if (left > longest) longest = left;
      }
    }
    /* CAPPED, because this is a wait in front of a page that is otherwise
       ready. The scenes run to 2.3 seconds; landing on one a fifth of the way
       through and honouring it in full would hold a finished page for two
       more seconds, which is the exact complaint that got the old flat
       two-second hold removed in the first place. Finish what is nearly
       finished; do not wait out a cycle that has barely started. */
    return longest > MAX_ARC_WAIT ? 0 : longest;
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
