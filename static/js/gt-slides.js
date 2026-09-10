/* Slideshows inside a story — the reader's half.
 *
 * The client's note: "under paragraph one I want an image — a max of 10 — but
 * it will show as one and auto slide in 3 seconds, or have the manual option
 * as well: it auto slides and I want to go back. And have the hold: when I
 * hold the image, no auto slide is done. And not only images but also a
 * video, less than 45 seconds, then slide 2 and whatever images or even
 * another video, but 10 slides max."
 *
 * The story editor writes a slideshow into the body as plain markup —
 *
 *   <figure class="gt-slides" data-slides>
 *     <div class="gs-track">
 *       <div class="gs-slide"><img src alt></div>
 *       <div class="gs-slide"><video src muted playsinline></video></div>
 *     </div>
 *   </figure>
 *
 * — which on its own is a row of pictures you can swipe along (see
 * .gt-slides in goodtip.css). This turns it into the thing asked for:
 *
 *   * ONE AT A TIME, turning every 3 seconds. A video slide plays, muted,
 *     and the show moves on when it ends rather than cutting it off at 3s.
 *   * BACK AND FORWARD by hand — arrows, dots, a swipe, or the arrow keys —
 *     and every manual step restarts the clock for the slide you landed on.
 *   * HOLD TO STOP. Pressing and holding anywhere on the picture freezes it,
 *     video included, and letting go carries on from where it was. The bar
 *     along the top freezes with it, so the hold is visible.
 *   * NOTHING PLAYS OFF SCREEN. The clock only runs while the slideshow is
 *     in view, and not at all in a hidden tab.
 *   * REDUCED MOTION is respected: nothing turns or plays on its own, and
 *     the arrows and dots are the way through.
 */
(function () {
  'use strict';

  var IMAGE_MS = 3000;
  var SWIPE_PX = 40;
  var still = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var ICON = {
    prev: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 5-7 7 7 7"/></svg>',
    next: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 7 7-7 7"/></svg>',
    muted: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9h4l5-4v14l-5-4H4z"/><path d="m17 9 5 6M22 9l-5 6"/></svg>',
    sound: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M17 8.5a5 5 0 0 1 0 7M19.5 6a8.5 8.5 0 0 1 0 12"/></svg>'
  };

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html) n.innerHTML = html;
    return n;
  }

  function build(fig) {
    var slides = Array.prototype.slice.call(fig.querySelectorAll('.gs-slide'));
    var n = slides.length;
    if (!n || fig.classList.contains('is-live')) return;
    fig.classList.add('is-live');
    fig.setAttribute('role', 'region');
    fig.setAttribute('aria-roledescription', 'slideshow');
    if (!fig.getAttribute('aria-label')) fig.setAttribute('aria-label', 'Slideshow, ' + n + (n === 1 ? ' item' : ' items'));
    fig.tabIndex = 0;

    var videos = slides.map(function (s) { return s.querySelector('video'); });
    videos.forEach(function (v) {
      if (!v) return;
      v.muted = true;
      v.playsInline = true;
      v.setAttribute('playsinline', '');
      v.preload = 'metadata';
      v.removeAttribute('controls');
      // With motion turned off nothing plays itself, so the reader gets the
      // browser's own controls to start it.
      if (still) v.controls = true;
    });

    var bar = el('div', 'gs-bar'), fill = el('span', 'gs-fill');
    bar.appendChild(fill);
    fig.appendChild(bar);

    var sound = el('button', 'gs-sound', ICON.muted);
    sound.type = 'button';
    sound.setAttribute('aria-label', 'Turn sound on');
    fig.appendChild(sound);

    var dots = [], count = null;
    if (n > 1) {
      var prev = el('button', 'gs-nav gs-prev', ICON.prev);
      var next = el('button', 'gs-nav gs-next', ICON.next);
      prev.type = next.type = 'button';
      prev.setAttribute('aria-label', 'Previous');
      next.setAttribute('aria-label', 'Next');
      fig.appendChild(prev);
      fig.appendChild(next);
      prev.addEventListener('click', function () { show(at - 1); });
      next.addEventListener('click', function () { show(at + 1); });

      var dotRow = el('div', 'gs-dots');
      slides.forEach(function (_, k) {
        var d = el('button', 'gs-dot');
        d.type = 'button';
        d.setAttribute('aria-label', 'Show ' + (k + 1) + ' of ' + n);
        d.addEventListener('click', function () { show(k); });
        dots.push(d);
        dotRow.appendChild(d);
      });
      fig.appendChild(dotRow);
      count = el('span', 'gs-count');
      fig.appendChild(count);
    }

    var at = 0, timer = null, started = 0, remaining = IMAGE_MS;
    var held = false, inView = false;

    function current() { return videos[at]; }

    function paintFill(ms) {
      // Restart the bar's fill animation for `ms`, from wherever `remaining`
      // says it is — a release after a hold carries on, it does not refill.
      fill.style.animation = 'none';
      void fill.offsetWidth;
      if (ms == null) return;
      fill.style.setProperty('--gs-from', (1 - ms / IMAGE_MS).toFixed(3));
      fill.style.animation = 'gs-fill ' + ms + 'ms linear forwards';
    }

    function clear() { if (timer) { clearTimeout(timer); timer = null; } }

    function running() { return n > 1 && !still && !held && inView && !document.hidden; }

    function tick() {
      clear();
      var v = current();
      sound.hidden = !v;
      if (v) {
        fill.style.animation = 'none';
        if (!still && !held && inView && !document.hidden) {
          var p = v.play();
          if (p && p.catch) p.catch(function () { /* autoplay refused */ });
        } else {
          v.pause();
        }
        return;  // a video moves the show on from its own `ended`
      }
      if (!running()) { paintFill(null); return; }
      started = Date.now();
      paintFill(remaining);
      timer = setTimeout(function () { show(at + 1); }, remaining);
    }

    function show(k) {
      var old = current();
      if (old) { old.pause(); }
      at = (k + n) % n;
      slides.forEach(function (s, i) {
        var on = i === at;
        s.classList.toggle('on', on);
        s.setAttribute('aria-hidden', on ? 'false' : 'true');
      });
      dots.forEach(function (d, i) {
        d.classList.toggle('on', i === at);
        if (i === at) d.setAttribute('aria-current', 'true'); else d.removeAttribute('aria-current');
      });
      if (count) count.textContent = (at + 1) + ' / ' + n;
      var v = current();
      if (v) { try { v.currentTime = 0; } catch (e) { /* not loaded yet */ } }
      remaining = IMAGE_MS;
      tick();
    }

    videos.forEach(function (v, i) {
      if (!v) return;
      v.addEventListener('ended', function () {
        if (i !== at) return;
        if (n > 1 && !still) show(at + 1);
        else { v.currentTime = 0; if (!still) v.play().catch(function () {}); }
      });
      v.addEventListener('timeupdate', function () {
        if (i !== at || !v.duration) return;
        fill.style.animation = 'none';
        fill.style.transform = 'scaleX(' + (v.currentTime / v.duration).toFixed(3) + ')';
      });
      // A file that will not play (a codec this browser lacks) must not
      // stall the show on a black slide.
      v.addEventListener('error', function () {
        if (i === at && n > 1) setTimeout(function () { if (i === at) show(at + 1); }, IMAGE_MS);
      });
    });

    sound.addEventListener('click', function (e) {
      e.stopPropagation();
      var v = current();
      if (!v) return;
      v.muted = !v.muted;
      // One setting for the whole slideshow: turning sound on for the first
      // clip should not mean turning it on again for the second.
      videos.forEach(function (o) { if (o) o.muted = v.muted; });
      sound.innerHTML = v.muted ? ICON.muted : ICON.sound;
      sound.setAttribute('aria-label', v.muted ? 'Turn sound on' : 'Turn sound off');
    });

    /* HOLD, and SWIPE, from one set of pointer events. A press that moves
       more than a few pixels sideways is a swipe; one that stays put is a
       hold for as long as it lasts. Controls are excluded, or pressing an
       arrow would count as holding the picture. */
    var downX = null, moved = false;
    fig.addEventListener('pointerdown', function (e) {
      if (e.target.closest('button')) return;
      downX = e.clientX; moved = false;
      hold(true);
    });
    fig.addEventListener('pointermove', function (e) {
      if (downX !== null && Math.abs(e.clientX - downX) > 8) moved = true;
    });
    function release(e) {
      if (downX === null) return;
      var dx = e.clientX - downX;
      downX = null;
      hold(false);
      if (moved && n > 1 && Math.abs(dx) > SWIPE_PX) show(dx < 0 ? at + 1 : at - 1);
    }
    fig.addEventListener('pointerup', release);
    fig.addEventListener('pointercancel', release);
    fig.addEventListener('pointerleave', release);
    // A long press on a phone would otherwise open the save-image menu over
    // the very picture being held still to look at.
    fig.addEventListener('contextmenu', function (e) { if (held) e.preventDefault(); });

    function hold(on) {
      if (on === held) return;
      held = on;
      fig.classList.toggle('is-held', on);
      var v = current();
      if (on) {
        if (timer) { remaining = Math.max(200, remaining - (Date.now() - started)); }
        clear();
        fill.style.animationPlayState = 'paused';
        if (v) v.pause();
      } else {
        fill.style.animationPlayState = '';
        tick();
      }
    }

    fig.addEventListener('keydown', function (e) {
      if (n < 2) return;
      if (e.key === 'ArrowRight') { e.preventDefault(); show(at + 1); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); show(at - 1); }
    });

    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          inView = en.isIntersecting;
          if (!inView) { hold(false); clear(); var v = current(); if (v) v.pause(); paintFill(null); }
          else tick();
        });
      }, { threshold: 0.35 }).observe(fig);
    } else {
      inView = true;
    }
    document.addEventListener('visibilitychange', function () { remaining = IMAGE_MS; tick(); });

    show(0);
  }

  function init() { document.querySelectorAll('[data-slides]').forEach(build); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
