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
 *   - 3s dwell per image (GT_SCENE_PERIOD), crossfade handled in CSS.
 *   - Each stack gets a random start offset so stacks on the same page don't
 *     flip in unison, which reads as a page-wide flicker rather than motion.
 *   - Rotation pauses while the tab is hidden and resumes on return, so you
 *     don't come back to a burst of catch-up transitions.
 *   - prefers-reduced-motion: images still change — a crossfade is not the
 *     kind of motion that setting is there to suppress, and the rotation is
 *     the point of the design. What does get dropped is the ken-burns zoom
 *     (gated in CSS on no-preference) and the collage's sliding panes, which
 *     are real movement. The dwell also lengthens to give a calmer pace.
 */
(function () {
  'use strict';

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var PERIOD = window.GT_SCENE_PERIOD || (reduce ? 5000 : 3000);

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

  function show(shots, i) {
    for (var k = 0; k < shots.length; k++) {
      var on = k === i;
      shots[k].classList.toggle('on', on);
      shots[k].classList.toggle('active', on);
    }
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
    show(shots, i);

    // Fetch the images up front. They'd otherwise be requested at the moment
    // each one first becomes visible, which shows as a blank beat mid-fade.
    for (var k = 0; k < shots.length; k++) {
      var url = (shots[k].style.backgroundImage || '').replace(/^url\(['"]?/, '').replace(/['"]?\)$/, '');
      if (url) { var img = new Image(); img.src = url; }
    }

    function step() {
      i = (i + 1) % shots.length;
      show(shots, i);
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

  /* Hero collage: one full image <-> a two-way split, on the same 3s beat.
     While a pane is tucked away it advances to its next image, so no state
     ever shows the same picture twice. */
  function driveCollage(col) {
    var panes = [].slice.call(col.querySelectorAll('.pane'));
    if (panes.length < 2) return;
    col.classList.add('split');
    panes.forEach(function (p) {
      var shots = p.querySelectorAll('.shot');
      if (shots.length) show(shots, startIndex(shots));
    });
    var idx = panes.map(function (p) { return startIndex(p.querySelectorAll('.shot')); });

    function advance(n) {
      var shots = panes[n].querySelectorAll('.shot');
      if (shots.length < 2) return;
      idx[n] = (idx[n] + 1) % shots.length;
      show(shots, idx[n]);
    }

    if (reduce) {
      // Hold the split — the panes sliding open and shut is the movement worth
      // suppressing — but keep each side's pictures changing, staggered so the
      // two halves don't swap on the same beat.
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
      return;
    }

    var states = ['split', 'full-0', 'split', 'full-1'];
    var si = 0;
    var handle = null;

    function step() {
      si = (si + 1) % states.length;
      var st = states[si];
      col.classList.toggle('split', st === 'split');
      panes[0].classList.toggle('closed', st === 'full-1');
      panes[1].classList.toggle('closed', st === 'full-0');
      // Swap the hidden pane's picture only once it's fully tucked away.
      if (st === 'full-0') setTimeout(function () { advance(1); }, PERIOD * 0.4);
      if (st === 'full-1') setTimeout(function () { advance(0); }, PERIOD * 0.4);
    }

    timers.push({
      start: function () { if (!handle) handle = setInterval(step, PERIOD); },
      stop: function () { if (handle) { clearInterval(handle); handle = null; } }
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
