/* Form focus: a form comes to the middle of the screen and everything else
 * dims, so filling it in is not competing with the rest of the page.
 *
 * CLIENT, 27 SEP 2026, on the waiting list and on "Get in touch": "when I
 * press the form or enter my cursor on the places where I am supposed to
 * type, that form should come through and be at the center and the other
 * content becomes kinda translucent so I can type and fill the form ...
 * make sure when I am to do anything with that forms it comes to the center
 * and other things become translucent, I like that style and vibe."
 * Named once, applied to both: this is a page behaviour, not a per-form one.
 *
 * OPT IN IS data-form-shell, WHICH ALREADY EXISTS. Both forms this was asked
 * for — the waiting list's .tmw-card and the contact partial's #contact-form
 * — are already wrapped in data-form-shell for gt-forms.js's busy veil and
 * thank-you swap. Reusing it means any future data-form-shell form gets this
 * for free, and there is one flag to opt a form out of it (data-no-focus)
 * rather than a second attribute to remember to add everywhere.
 *
 * WHY THE SHELL, NOT THE <form>. gt-forms.js REPLACES the <form> element on
 * submit (restore() clones a pristine copy and swaps it in) — a listener kept
 * on the form itself would be talking to a node that no longer exists after
 * one submission. The shell is never replaced, so everything here is bound to
 * it once and stays correct through busy → done → back.
 *
 * THE GEOMETRY IS FLIP, NOT A JUMP TO position:fixed. Fixed positioning takes
 * the shell out of the page's flow, which would otherwise leave a hole where
 * it was and yank everything below it upward. A placeholder the same size is
 * left in its place for as long as the shell is lifted out, and the shell
 * itself is pinned at its own on-screen position first — in pixels, not the
 * centred spot — so the one style change that follows (adding .ff-centered,
 * clearing the pixel pin) is what the browser actually transitions.
 *
 * STAYS OPEN THROUGH ITS OWN SUBMIT. Clicking "Send" moves focus off the
 * button the instant gt-forms.js hides the form for its veil, which reads as
 * "focus left the shell" a few milliseconds into an action that is still very
 * much in progress. Closing the panel out from under a message that is
 * mid-send would undo the point of centring it in the first place, so a shell
 * showing the busy veil or the thank-you panel is left alone regardless of
 * where focus is, and only re-evaluated once both are gone. */
(function () {
  'use strict';

  var OPEN_MS = 60;     /* one frame plus a little, before the centred class is added */
  var CLOSE_MS = 480;   /* matches the CSS transition; the DOM tidy-up waits for it */

  var active = null;    /* the one open shell, if any */

  function forced() { return document.documentElement.classList.contains('gt-motion'); }
  function reduced() {
    return !forced() && window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function backdrop() {
    var b = document.querySelector('.ff-backdrop');
    if (!b) {
      b = document.createElement('div');
      b.className = 'ff-backdrop';
      b.setAttribute('aria-hidden', 'true');
      document.body.appendChild(b);
      b.addEventListener('click', function () { if (active) close(active.shell); });
    }
    return b;
  }

  function busyOrDone(shell) {
    return !!shell.querySelector('.gtf-veil, .gtf-done');
  }

  function open(shell) {
    if (shell.dataset.noFocus != null || (active && active.shell === shell)) return;
    if (active) close(active.shell, true);

    var r = shell.getBoundingClientRect();
    var placeholder = document.createElement('div');
    placeholder.className = 'ff-placeholder';
    placeholder.style.width = r.width + 'px';
    placeholder.style.height = r.height + 'px';
    var cs = getComputedStyle(shell);
    placeholder.style.marginTop = cs.marginTop;
    placeholder.style.marginBottom = cs.marginBottom;
    shell.parentNode.insertBefore(placeholder, shell);

    var close2 = document.createElement('button');
    close2.type = 'button';
    close2.className = 'ff-close';
    close2.setAttribute('aria-label', 'Close');
    close2.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>';
    close2.addEventListener('click', function () { close(shell); });
    shell.insertBefore(close2, shell.firstChild);

    var bd = backdrop();

    active = { shell: shell, placeholder: placeholder, close: close2 };

    if (reduced()) {
      shell.classList.add('ff-active', 'ff-centered', 'ff-instant');
      bd.classList.add('on', 'ff-instant');
      document.body.classList.add('ff-lock');
      return;
    }

    /* Pin the shell exactly where it already is, in pixels, with no
       transition wired yet — this is the FLIP "first" frame. */
    shell.style.left = r.left + 'px';
    shell.style.top = r.top + 'px';
    shell.style.width = r.width + 'px';
    shell.classList.add('ff-active');
    document.body.classList.add('ff-lock');
    void shell.offsetWidth;   /* commit the pin before the next change */

    bd.classList.add('on');
    setTimeout(function () {
      if (!active || active.shell !== shell) return;
      shell.classList.add('ff-centered');
      shell.style.left = '';
      shell.style.top = '';
      shell.style.width = '';
    }, OPEN_MS);
  }

  function close(shell, immediate) {
    if (!active || active.shell !== shell) return;
    var entry = active;
    active = null;

    var bd = document.querySelector('.ff-backdrop');
    if (bd) bd.classList.remove('on');
    document.body.classList.remove('ff-lock');
    if (entry.close.parentNode) entry.close.remove();

    var done = function () {
      shell.classList.remove('ff-active', 'ff-centered', 'ff-instant');
      shell.style.left = ''; shell.style.top = ''; shell.style.width = '';
      if (entry.placeholder.parentNode) entry.placeholder.remove();
    };

    if (immediate || reduced()) { done(); return; }

    /* Return to wherever the placeholder actually is now — the page may have
       grown or shrunk above it (typed copy elsewhere, a loaded image) while
       the shell was lifted out, so its reserved spot is the source of truth,
       not the rect captured on the way in. */
    var pr = entry.placeholder.getBoundingClientRect();
    shell.style.left = pr.left + 'px';
    shell.style.top = pr.top + 'px';
    shell.style.width = pr.width + 'px';
    shell.classList.remove('ff-centered');
    setTimeout(done, CLOSE_MS);
  }

  document.addEventListener('focusin', function (e) {
    var shell = e.target.closest('[data-form-shell]');
    if (shell) open(shell);
  });

  document.addEventListener('focusout', function (e) {
    var shell = e.target.closest('[data-form-shell]');
    if (!shell || !active || active.shell !== shell) return;
    var to = e.relatedTarget;
    setTimeout(function () {
      if (!active || active.shell !== shell) return;
      if (busyOrDone(shell)) return;                       /* mid-submit; leave it be */
      var lives = shell.contains(document.activeElement);
      if (!lives && (!to || !shell.contains(to))) close(shell);
    }, 0);
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && active) close(active.shell);
  });
})();
