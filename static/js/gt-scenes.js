/* GoodTip scenes — the single image-rotation engine for the whole site.
 *
 * Every stack of photos on the site (hero backdrops, the side scene strips,
 * the auth brand panel, image bands, the Wall header, dashboard banner) is
 * markup shaped the same way: a container holding two or more `.shot`
 * children, each with a background-image. This file rotates all of them.
 *
 * It replaced eight near-identical setInterval blocks that had drifted apart —
 * different periods (5s / 6s / 9s) and two different "which shot is showing"
 * class conventions. Rather than pick one convention and rewrite the CSS, the
 * showing shot gets BOTH `on` and `active`, so the existing rules for
 * `.page-scenes .shot.on` and `.hero-bg .shot.active` are both satisfied.
 *
 * Behaviour:
 *   - 15s dwell per image (GT_SCENE_PERIOD), crossfade handled in CSS.
 *   - The showing image zooms for the whole dwell, and the direction flips on
 *     every swap: push in for 15s, change at the wide end, pull back for 15s,
 *     change at rest. Each image therefore starts at exactly the scale the
 *     last one finished on, which is what makes the sequence read as one
 *     continuous camera move rather than a series of resets.
 *   - Each stack gets a random start offset so stacks on the same page don't
 *     flip in unison, which reads as a page-wide flicker rather than motion.
 *   - Rotation pauses while the tab is hidden and resumes on return, so you
 *     don't come back to a burst of catch-up transitions.
 *   - prefers-reduced-motion: images still change, on the same dwell, and the
 *     zoom still runs at a much shorter travel (6% rather than 14%). Neither a
 *     crossfade nor a slow scale is the kind of movement that setting exists
 *     to suppress, and the rotation is the point of the design. What does get
 *     dropped is the collage's sliding panes — that is real travel across the
 *     hero — along with the slide/zoom/wipe entrance effects.
 */
(function () {
  'use strict';

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  /* 15s per image, everywhere, reduced motion included. The zoom runs for the
     whole dwell, so this is also the length of one push or pull — the slower it
     is, the more it reads as a camera drifting and the less as an image being
     resized. Reduced motion used to get its own longer dwell; one number for
     the whole site is simpler to reason about and the calm variant already
     differs where it matters, in how far the image travels. */
  var PERIOD = window.GT_SCENE_PERIOD || 15000;

  /* Stacks driven by this engine. Collage panes are deliberately absent: the
     collage state machine below advances its own panes while they're tucked
     away, and a second driver would fight it. */
  var STACK_SEL = [
    '.page-scenes .scene',
    '.page-backdrop',
    '.hero-bg:not(.collage)',
    '.image-band',
    '.auth-brand .ab-bg',
    '.auth-aside .aa-bg',
    '.dash-banner .bg',
    '.gwh-shots',
    '[data-shots]'
  ].join(',');

  var timers = [];

  /* The four transition styles. A stack can name one in its markup
     (class="fx-wipe"); anything that doesn't gets one assigned by position, so
     the site shows variety without every template having to opt in. Assignment
     is by index rather than at random so a given stack keeps its style across
     reloads — a hero that fades on one visit and wipes on the next reads as a
     glitch, not as range. */
  var FX = ['fx-fade', 'fx-slide', 'fx-zoom', 'fx-wipe'];

  function assignFx(stack, index) {
    for (var f = 0; f < FX.length; f++) {
      if (stack.classList.contains(FX[f])) return;
    }
    stack.classList.add(FX[index % FX.length]);
  }

  /* Alternating zoom. Each shot breathes for its whole dwell rather than
     animating briefly on entry, and the direction flips every step: one image
     pulls back, the next pushes in, the one after pulls back again.

     The flip is what makes it read as continuous. A shot that has just zoomed
     OUT ends at rest, so the next one starts at rest and zooms IN; that one
     ends wide, so the following starts wide and zooms out. No image ever jumps
     to a scale the previous one was not already sitting at. */
  function show(shots, i, dir) {
    for (var k = 0; k < shots.length; k++) {
      var on = k === i;
      shots[k].classList.toggle('on', on);
      shots[k].classList.toggle('active', on);
      if (!on) shots[k].classList.remove('zoom-in', 'zoom-out');
    }
    var cur = shots[i];
    if (!cur) return;
    // Restart the animation even when the direction is unchanged — without
    // this the browser keeps the finished run and the image sits still.
    cur.classList.remove('zoom-in', 'zoom-out');
    void cur.offsetWidth;
    cur.classList.add(dir ? 'zoom-in' : 'zoom-out');
  }

  /* Which shot the server marked as the starting one, so the first transition
     moves forward from what's already painted instead of jumping to index 0. */
  function startIndex(shots) {
    for (var k = 0; k < shots.length; k++) {
      if (shots[k].classList.contains('on') || shots[k].classList.contains('active')) return k;
    }
    return 0;
  }

  function drive(stack, index) {
    var shots = stack.querySelectorAll('.shot');
    if (shots.length < 2) return;
    assignFx(stack, index || 0);

    var i = startIndex(shots);
    // The first image you see pushes IN. That is the opening beat of the
    // cycle — magnify, swap at the wide end, pull back, swap at rest — and
    // starting every stack on the same one keeps a page with several of them
    // in step on first paint rather than each doing its own thing.
    var dir = true;
    stack.style.setProperty('--dwell', PERIOD + 'ms');
    show(shots, i, dir);

    // Fetch the images up front. They'd otherwise be requested at the moment
    // each one first becomes visible, which shows as a blank beat mid-fade.
    for (var k = 0; k < shots.length; k++) {
      var url = (shots[k].style.backgroundImage || '').replace(/^url\(['"]?/, '').replace(/['"]?\)$/, '');
      if (url) { var img = new Image(); img.src = url; }
    }

    function step() {
      i = (i + 1) % shots.length;
      dir = !dir;              // out, in, out, in …
      show(shots, i, dir);
    }

    // Random offset up to one full period keeps sibling stacks out of phase.
    var offset = Math.random() * PERIOD;
    var handle = null;
    timers.push({
      start: function () {
        if (handle) return;
        handle = setTimeout(function () {
          step();
          handle = setInterval(step, PERIOD);
        }, offset);
        // After the first hop the offset has done its job.
        offset = 0;
      },
      stop: function () {
        if (!handle) return;
        clearTimeout(handle); clearInterval(handle); handle = null;
      }
    });
  }

  /* Hero collage: a two-way split that periodically opens out to let one half
     take the whole frame — split, then image A full, then split, then image B
     full — while both halves keep rotating their pictures throughout.

     Two timers, deliberately not the same one:

       - Pictures change on the ordinary PERIOD, staggered half a beat apart,
         exactly like every other stack on the site. This used to be welded to
         the layout state machine, which reached a given pane's "now swap it"
         state only once every four steps — so the two halves changed picture
         once every sixteen seconds while everything else moved on four.

       - The layout reframes once per beat, offset by a quarter of one so a
         reframe never lands on the same tick as a crossfade. One state per
         beat is the original intent, and at a 15s beat it is a calm hold —
         it only read as restless back when a beat was three seconds. The
         offset still matters: reframing is a much bigger gesture than a
         picture change, and landing both on the same tick makes each read as
         noise rather than as two separate things happening.

     A closed pane keeps rotating while it is tucked away, which is what the
     original was reaching for with its deferred swap: by the time a half opens
     back up it is showing something you have not just been looking at. */
  function driveCollage(col) {
    var panes = [].slice.call(col.querySelectorAll('.pane'));
    if (panes.length < 2) return;
    col.classList.add('split');
    col.style.setProperty('--dwell', PERIOD + 'ms');
    // Direction per pane, so each half breathes independently: one pushes in
    // while the other pulls back, instead of both scaling in lockstep — which
    // would read as the whole hero pulsing rather than as two live images.
    var dirs = panes.map(function (_, n) { return n % 2 === 0; });
    panes.forEach(function (p, n) {
      var shots = p.querySelectorAll('.shot');
      if (shots.length) show(shots, startIndex(shots), dirs[n]);
    });
    var idx = panes.map(function (p) { return startIndex(p.querySelectorAll('.shot')); });

    function advance(n) {
      var shots = panes[n].querySelectorAll('.shot');
      if (shots.length < 2) return;
      idx[n] = (idx[n] + 1) % shots.length;
      dirs[n] = !dirs[n];
      show(shots, idx[n], dirs[n]);
    }

    panes.forEach(function (_, n) {
      var handle = null;
      var offset = n * (PERIOD / panes.length);
      timers.push({
        start: function () {
          if (handle) return;
          handle = setTimeout(function () {
            advance(n);
            handle = setInterval(function () { advance(n); }, PERIOD);
          }, offset);
          offset = 0;
        },
        stop: function () {
          if (!handle) return;
          clearTimeout(handle); clearInterval(handle); handle = null;
        }
      });
    });

    // Reduced motion holds the split open. Panes sliding across the hero is
    // real travel — the thing the setting exists to suppress — where a
    // crossfade is not, so the pictures carry on changing.
    if (reduce) return;

    /* split, A full, split, B full … built from the panes rather than hard
       coded, so a third pane in the markup just extends the rotation. */
    var states = [];
    panes.forEach(function (_, n) { states.push('split', n); });
    var si = 0;

    function reframe() {
      si = (si + 1) % states.length;
      var st = states[si];
      var isSplit = st === 'split';
      col.classList.toggle('split', isSplit);
      panes.forEach(function (p, n) {
        p.classList.toggle('closed', !isSplit && st !== n);
      });
    }

    var LAYOUT = PERIOD;
    var wait = PERIOD / 4;          // land between crossfades, not on one
    var lh = null;
    timers.push({
      start: function () {
        if (lh) return;
        lh = setTimeout(function () {
          reframe();
          lh = setInterval(reframe, LAYOUT);
        }, wait);
        // Coming back to a hidden tab should not reframe on arrival; give it a
        // full beat so the hero is settled while you find your place again.
        wait = LAYOUT;
      },
      stop: function () {
        if (!lh) return;
        clearTimeout(lh); clearInterval(lh); lh = null;
      }
    });
  }

  function init() {
    document.querySelectorAll(STACK_SEL).forEach(drive);
    document.querySelectorAll('.hero-bg.collage').forEach(driveCollage);

    timers.forEach(function (t) { t.start(); });

    document.addEventListener('visibilitychange', function () {
      timers.forEach(function (t) { document.hidden ? t.stop() : t.start(); });
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
