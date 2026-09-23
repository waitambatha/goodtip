/* The Wall, typing itself out.
 *
 * CLIENT, 22 SEP 2026: "instead of just having those chats as static where I
 * find them, let's make such sections like some live futuristic design — like
 * I see it being typed. Like the Wall, the way you see those chats, let me see
 * the first text being typed, then the other kind of like replies. Basically
 * like seeing chats in real time and live ... like see the magic happen."
 *
 * WHAT IT IS SELLING. These two posts are the whole argument for the Wall, and
 * as static blocks they are a screenshot of a feature — the reader has to be
 * told it is live. Played out, they are the feature: a person posts, the room
 * reacts, and the system answers. Nobody has to be told.
 *
 * THE MARKUP IS THE SOURCE OF TRUTH, NOT THIS FILE. The posts are written in
 * the template, fully formed, and this script takes them apart at runtime and
 * puts them back a character at a time. Nothing here invents content. With
 * JavaScript off, or reduced motion on, the section is exactly what the
 * template says and always has been — which is also why the copy can be edited
 * from the Pages screen without anybody touching this.
 *
 * INNER HTML, NOT TEXT. The bodies carry real markup — a lime <strong> for the
 * ladder position, a .pick span for the tip. Typing textContent would strip
 * all of it and the sentence would land unstyled. So the body is walked as a
 * tree and rebuilt node by node, with the typing happening inside whatever
 * element each run of text belongs to.
 *
 * IT ONLY RUNS ON SCREEN. An IntersectionObserver starts it and stops it, so
 * a page left open in a background tab is not animating text nobody can see.
 *
 * Opt in:  <div data-live-thread>  around the posts.
 */
(function () {
  'use strict';

  /* Characters per second. 42 is a brisk but readable typing pace — a
     56-character sledge lands in about 1.3s and the longest post here in
     about 3s, so the whole six-post thread runs in well under a minute. */
  var CHARS_PER_SEC = 42;
  var THINK_MS     = 900;   /* the "typing…" bubble before a post starts */
  var AFTER_POST   = 1500;  /* beat between one post and the next */
  var LOOP_PAUSE   = 7000;  /* before the thread replays */
  /* HOW MANY GET TYPED. The home page's sample thread is six; the public
     Wall's real feed can be any length, and a reader should not have to sit
     through forty posts being typed to reach the one they came for. The
     first six play; everything after them is revealed at once, immediately,
     so the page is complete either way. */
  var MAX_TYPED    = 6;

  /* REDUCED MOTION, BUT THROUGH THE SITE'S OWN GATE.
   *
   * base.html sets .gt-motion on <html> when ?motion=1 has been used, and the
   * stylesheet's blanket `animation: none !important` checks for it. This
   * script was asking matchMedia directly and so ignored the override — which
   * meant that on any machine with the OS setting on (every one this has been
   * built on, as it happens) the thread never played and there was no way to
   * make it, short of changing a system preference. The typing was there the
   * whole time; nothing could see it.
   *
   * Same test the rest of the site uses, in the same order: the OS setting
   * decides unless the override says otherwise. */
  var forced = document.documentElement.classList.contains('gt-motion');
  var reduced = !forced && window.matchMedia &&
                window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function now() {
    return (window.performance && performance.now) ? performance.now() : Date.now();
  }

  /* Walk a node, collecting every text run with the element it belongs to, so
     the structure can be rebuilt empty and refilled in order. */
  function runs(node, out) {
    out = out || [];
    Array.prototype.forEach.call(node.childNodes, function (kid) {
      if (kid.nodeType === 3) {
        if (kid.nodeValue.trim() !== '' || out.length) out.push(kid);
      } else if (kid.nodeType === 1) {
        runs(kid, out);
      }
    });
    return out;
  }

  /* TWO MARKUP SHAPES, ONE SCRIPT.
   *
   * The home page teaser is written with .post / .post-body / .post-foot; the
   * Wall's own sample feed uses .wr-* on a different card component. They are
   * the same thing to a reader and they should behave the same, so the script
   * asks for either rather than the templates being rewritten to match it —
   * the Wall card is also rendered for REAL posts, and changing its class
   * names to suit an animation that must never run on real posts would be the
   * wrong way round.
   */
  var POST_SEL = '.post, .wall-card, .wf-item, [data-wall-post]';
  var BODY_SEL = '.post-body, .wr-body';
  var FOOT_SEL = '.post-foot, .wr-foot';

  function Thread(root) {
    this.root = root;
    this.posts = Array.prototype.slice.call(root.querySelectorAll(POST_SEL));
    this.cancelled = false;
    this.playing = false;
    /* EVERY RUN CARRIES A GENERATION, and it is not belt-and-braces.
     *
     * `cancelled` alone is shared mutable state: stop() sets it true, a
     * pending frame or timer reads it and unwinds — but if play() runs first
     * and sets it back to false, that stale loop sees "not cancelled" and
     * carries on. Scroll away and back a few times and there are several
     * loops writing to the same text nodes, each scheduling more work. That
     * is what froze the renderer.
     *
     * A loop captures `gen` when it starts and stops the moment the thread's
     * gen has moved on, which no later play() can undo. */
    this.gen = 0;

    /* Stash every body's text runs and blank them. Done once, up front, so a
       replay never re-reads a half-typed DOM. */
    this.script = this.posts.map(function (post) {
      var body = post.querySelector(BODY_SEL);
      if (!body) return null;
      var textNodes = runs(body);
      return {
        post: post,
        body: body,
        parts: textNodes.map(function (n) { return { node: n, full: n.nodeValue }; }),
        foot: post.querySelector(FOOT_SEL),
      };
    }).filter(Boolean);

    /* Anything past the cap is shown as written and never touched again. */
    this.extras = this.script.slice(MAX_TYPED);
    this.script = this.script.slice(0, MAX_TYPED);
  }

  Thread.prototype.reset = function () {
    /* The un-typed tail stays visible through every reset — it was never
       blanked, so there is nothing to restore and nothing to hide. */
    this.script.forEach(function (s) {
      s.post.classList.remove('lv-in', 'lv-typing');
      s.post.classList.add('lv-hidden');
      s.parts.forEach(function (p) { p.node.nodeValue = ''; });
      if (s.foot) s.foot.classList.add('lv-hidden');
    });
  };

  /* THE WAY BACK. Every failure path in this file ends here.
   *
   * reset() blanks real content that the template rendered, which means from
   * that moment the script OWNS whether the reader can see it. If anything
   * after that point throws — or simply never runs — the section is blank
   * permanently, and a blank section is far worse than an unanimated one.
   * So there is exactly one function that puts everything back, and it is
   * called from the catch and from the failsafe below. */
  Thread.prototype.revealAll = function () {
    this.cancelled = true;
    this.playing = false;
    this.root.classList.remove('lv-on');
    this.script.forEach(function (s) {
      s.parts.forEach(function (p) { p.node.nodeValue = p.full; });
      s.post.classList.remove('lv-hidden', 'lv-typing');
      s.post.classList.add('lv-in');
      if (s.foot) { s.foot.classList.remove('lv-hidden'); s.foot.classList.add('lv-in'); }
    });
  };

  Thread.prototype.typeBody = function (entry, gen) {
    /* TIME-BASED, NOT TICK-BASED — and on this page that is the difference
     * between an animation and a stall.
     *
     * The first version advanced one or two characters per setTimeout and
     * asked for ~20ms between them. That assumes the timer fires roughly when
     * asked. Measured on the home page with motion enabled, it does not: the
     * main thread is busy enough that a 200ms timer came back at 800ms, so
     * the typing advanced about two characters a second and a 56-character
     * post would have taken half a minute. It was running the whole time; it
     * just looked stopped.
     *
     * So the number of characters shown is computed from ELAPSED TIME against
     * a target rate, the same way any frame-rate-independent animation works.
     * At 60fps it draws a character at a time and looks typed; at 5fps it
     * draws twelve and still finishes on schedule. The page being slow now
     * costs smoothness rather than correctness.
     *
     * setTimeout, NOT requestAnimationFrame, and that is measured rather than
     * preferred. On the home page with motion enabled, rAF callbacks are not
     * serviced at all — a probe that asked for a single frame never came back
     * and took the debugger down with it, while setTimeout kept firing
     * (late, but firing). An animation driven by rAF there does not run
     * slowly, it does not run. Since the character count comes from the clock
     * rather than from the number of ticks, a late timer costs smoothness and
     * nothing else, which is the trade to make.
     */
    var self = this;
    var parts = entry.parts;
    var total = 0;
    for (var k = 0; k < parts.length; k++) total += parts[k].full.length;
    if (!total) return Promise.resolve();

    var started = now();

    return new Promise(function (done) {
      (function frame() {
        if (self.cancelled || self.gen !== gen) return done();

        var want = Math.min(total, Math.round((now() - started) / 1000 * CHARS_PER_SEC));

        /* Spread `want` characters across the parts in order. Rewriting from
           the start each frame keeps this correct however big the jump is —
           there is no per-part cursor to fall out of step. */
        var left = want;
        for (var i = 0; i < parts.length; i++) {
          var full = parts[i].full;
          var take = Math.max(0, Math.min(full.length, left));
          if (parts[i].node.nodeValue.length !== take) {
            parts[i].node.nodeValue = full.slice(0, take);
          }
          left -= take;
        }

        if (want >= total) return done();
        setTimeout(frame, 16);
      })();
    });
  };

  Thread.prototype.play = function () {
    var self = this;
    if (this.playing) return;

    /* START FROM THE BEGINNING, EVERY TIME (client, 22 Sep 2026: "let it start
     * when I reach that section, so that when I scroll I do not reach the
     * bottom and find that it has already typed").
     *
     * play() used to resume: it called run(0) without clearing, so posts that
     * had already been typed before the reader scrolled away were still full
     * of text when they came back. The first post appeared instantly and only
     * the later ones typed, which reads as the animation being broken rather
     * than as a replay.
     *
     * reset() is cheap — it blanks text nodes that are already stashed — so
     * doing it on every entry costs nothing and makes the section behave the
     * way somebody arriving at it expects: nothing has happened yet. */
    this.reset();
    this.playing = true;
    this.everPlayed = true;
    this.cancelled = false;
    var gen = ++this.gen;

    (function run(index) {
      if (self.cancelled || self.gen !== gen) { self.playing = false; return; }

      if (index >= self.script.length) {
        /* Replay, so somebody who arrives mid-thread still sees it happen. */
        return wait(LOOP_PAUSE).then(function () {
          if (self.cancelled || self.gen !== gen) { self.playing = false; return; }
          self.reset();
          run(0);
        });
      }

      var entry = self.script[index];
      entry.post.classList.remove('lv-hidden');
      entry.post.classList.add('lv-in', 'lv-typing');

      wait(THINK_MS)
        .then(function () {
          if (self.gen !== gen) return;
          entry.post.classList.remove('lv-typing');
          return self.typeBody(entry, gen);
        })
        .then(function () {
          if (self.gen !== gen) return;
          /* The reactions land AFTER the words, because that is the order it
             happens in — the room reacts to something that has been said. */
          if (entry.foot) {
            entry.foot.classList.remove('lv-hidden');
            entry.foot.classList.add('lv-in');
          }
          return wait(AFTER_POST);
        })
        .then(function () { if (self.gen === gen) run(index + 1); });
    })(0);
  };

  Thread.prototype.stop = function () {
    this.cancelled = true;
    this.playing = false;
    this.gen++;          /* retire whatever is still in flight */
    /* The pending timers in typeBody and the run chain all check `cancelled`
       before touching the DOM, so they unwind on their own. Nothing is reset
       here: play() does that on the way back in, which keeps the two halves
       of the decision in one place. */
  };

  function init() {
    var roots = document.querySelectorAll('[data-live-thread]');
    if (!roots.length) return;

    /* Reduced motion, or no observer to tell us when it is visible: leave the
       template's own fully-formed posts exactly as they are. */
    if (reduced || !('IntersectionObserver' in window)) return;

    Array.prototype.forEach.call(roots, function (root) {
      var thread;
      try {
        thread = new Thread(root);
        if (!thread.script.length) return;
        thread.reset();
        root.classList.add('lv-on');
      } catch (err) {
        /* Building the script is the only step that reads the DOM in bulk.
           If it fails, leave the template's own markup alone entirely. */
        if (thread) thread.revealAll();
        return;
      }

      /* THRESHOLD 0, NOT A FRACTION — and this is a correctness fix, not a
         tuning one. A threshold of 0.35 asks for 35% of the ELEMENT to be on
         screen, so an element taller than about 2.9x the viewport can never
         satisfy it: the observer never fires, play() is never called, and the
         posts that reset() blanked stay blank for good. The Wall's sample
         feed on a short landscape phone is exactly that shape.

         Any intersection at all is enough to start, which cannot deadlock at
         any element height. rootMargin pulls the trigger in slightly so the
         first post has begun by the time it is properly in view rather than
         starting from blank at the bottom edge. */
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (e.isIntersecting) thread.play();
          else thread.stop();
        });
      }, { threshold: 0, rootMargin: '0px 0px -12% 0px' });
      io.observe(root);

      /* A GEOMETRY CHECK ALONGSIDE THE OBSERVER, and it is not belt-and-braces.
       *
       * Measured on the home page, 22 Sep 2026: with motion enabled the main
       * thread there is busy enough that IntersectionObserver callbacks are
       * starved — a plain observer on a fully visible element did not fire
       * inside four seconds, and 67 of the page's 68 `.reveal` elements never
       * received their class either. The thread therefore never started, and
       * because reset() had already blanked the posts the section sat empty
       * until the 20-second failsafe put it back.
       *
       * IO remains the primary signal: it is the one that also STOPS the
       * thread when it scrolls away, and it costs nothing when it works. This
       * is a second opinion asked on a cheap timer — one getBoundingClientRect
       * every 400ms, dropped the moment the thread has started — so a busy
       * page delays the typing rather than cancelling it.
       *
       * The underlying slowness is a separate problem and this does not fix
       * it; it stops that problem from silently emptying a section. */
      /* The geometry check keeps running rather than firing once. IO is
         starved on a busy page (see the note above), and if the reader scrolls
         away and back, `stop()` will have cancelled the thread — so the poll
         has to be able to start it again, not just the first time. It is one
         getBoundingClientRect every 400ms and it does nothing while the thread
         is already playing. */
      setInterval(function () {
        if (thread.playing) return;
        var r = root.getBoundingClientRect();
        var vh = window.innerHeight || document.documentElement.clientHeight;
        var onScreen = r.top < vh * 0.88 && r.bottom > vh * 0.08;
        if (onScreen) thread.play();
      }, 400);

      /* THE FAILSAFE. If play() has still not started after this long, the
         reader is looking at — or about to look at — a section the script has
         emptied and not filled. Whatever the reason, the content goes back.
         Generous, because the normal case is "it is simply further down the
         page", and cheap, because it only ever fires once. */
      setTimeout(function () {
        if (!thread.playing && !thread.everPlayed) thread.revealAll();
      }, 20000);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
