/* Submit feedback for forms that do real work server-side.
 *
 * Signup was the case that prompted this: creating an account writes the user,
 * hashes a password, generates a code and hands a message to Postmark. That is
 * a second or more of genuine work, during which the page sat completely still
 * — no spinner, no disabled button, nothing. The only available reading is that
 * the click missed, so people click again, and a second click on a signup form
 * is a duplicate submission of the thing that is already running.
 *
 * Opt in per form:
 *   <form method="post" data-busy="Creating your account">
 *
 * The button reports what is happening and stops accepting clicks; a strip
 * under it shows the work is live. Deliberately indeterminate — the server does
 * not report progress, so anything that looked like a percentage would be made
 * up. Motion here is a status indicator rather than decoration, so it stays
 * under prefers-reduced-motion, where a frozen "please wait" would be worse
 * than none at all.
 *
 * A form can ask for more than a busy button. Marking a container makes the
 * whole panel go translucent behind a spinner instead:
 *
 *   <section class="ap-form" data-busy-scope>
 *
 * That is for screens where the form IS the page — signing in, verifying a
 * code, resetting a password. There, a changed button in the corner is a small
 * signal for what is actually a whole-page wait, and people click again or
 * navigate away. Veiling the panel says "this screen is mid-action" in a way
 * that cannot be missed, and it physically covers the controls so a second
 * submission is not just discouraged but impossible.
 *
 * It stays opt-in. A multi-step form like the competition builder wants its
 * fields readable while a step saves, so those keep the button-only version.
 */
(function () {
  'use strict';

  function wire(form) {
    var label = form.getAttribute('data-busy') || 'Working';
    if (!form.querySelector('[type="submit"]')) return;
    /* Wire once. Forms are re-scanned after every htmx swap, and a second
       listener on the same form would run the whole busy routine twice —
       stashing the already-replaced label as idleLabel, so restoring it on
       back/forward would put "Working" on the button permanently. */
    if (form.dataset.busyWired) return;
    form.dataset.busyWired = '1';

    form.addEventListener('submit', function (e) {
      /* Use the button actually pressed, not the first one in the form. The
         competition builder has Back, Continue and Start again in one form, so
         picking the first submit would put "Saving" on Back while Continue sat
         there looking untouched. submitter is unset for a bare form.submit() or
         an Enter keypress in older browsers, hence the fallback. */
      var btn = e.submitter || form.querySelector('[type="submit"]');
      if (!btn) return;
      /* A button can opt out — Back and Start again are navigation, and
         labelling them "Saving" would describe the wrong thing. */
      if (btn.hasAttribute('data-busy-skip')) return;
      // A form that failed client-side validation never reaches the server, so
      // showing "creating your account" would be a lie that never resolves.
      if (form.hasAttribute('novalidate') ? false : !form.checkValidity()) return;
      if (form.classList.contains('is-busy')) { e.preventDefault(); return; }

      form.classList.add('is-busy');
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');

      /* TWO TREATMENTS, chosen by whether the label was authored.
       *
       * Replacing a button's contents with a word only works when the button
       * was built to hold words. Applied blindly it is destructive: the Wall's
       * delete control is a single "×", and swapping that for "Working" turns
       * a 28px round button into a wide rectangle that shoves the post header
       * out of shape. Icon buttons lose their icon; short buttons jump width
       * mid-click.
       *
       * So the label swap is now reserved for forms that asked for one by
       * name — data-busy="Creating your account" — where an author has looked
       * at the button and decided the words fit. Everything picked up by the
       * blanket pass keeps its own content and gets a spinner laid over it,
       * which cannot change the size of anything. */
      if (form.dataset.busyAuto) {
        btn.classList.add('is-btn-busy');
      } else {
        btn.dataset.idleLabel = btn.innerHTML;
        btn.innerHTML = '<span class="busy-dot busy-run" aria-hidden="true"></span>' + label;
      }

      var scope = form.closest('[data-busy-scope]');
      if (scope) {
        veil(scope, label);
        // No strip as well. It would sit under the veil where nobody can see
        // it, and two indeterminate indicators for one action read as two
        // things happening.
        return;
      }

      if (!form.querySelector('.busy-strip')) {
        var strip = document.createElement('div');
        strip.className = 'busy-strip';
        strip.setAttribute('role', 'status');
        strip.innerHTML = '<i class="busy-run"></i>';
        btn.parentNode.insertBefore(strip, btn.nextSibling);
      }
    });
  }

  function veil(scope, label) {
    if (scope.querySelector('.busy-veil')) return;
    var v = document.createElement('div');
    v.className = 'busy-veil';
    /* role=status rather than a live region on the button: the veil is now the
       only thing describing what is happening, so it is what should be read
       out. Polite, because interrupting whatever the user was told last to say
       "signing you in" is not urgent enough to warrant it. */
    v.setAttribute('role', 'status');
    v.setAttribute('aria-live', 'polite');

    var inner = document.createElement('div');
    inner.className = 'bv-inner';
    var ring = document.createElement('span');
    ring.className = 'bv-ring busy-run';
    ring.setAttribute('aria-hidden', 'true');
    var text = document.createElement('span');
    text.className = 'bv-label';
    text.textContent = label;          // textContent, so a label can never inject markup
    inner.appendChild(ring);
    inner.appendChild(text);
    v.appendChild(inner);

    scope.classList.add('is-busy-scope');
    scope.appendChild(v);
  }

  /* Back/forward restores the page from cache with the button still disabled
     and mid-label, which looks like a hung form. Put it back. */
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    document.querySelectorAll('form.is-busy').forEach(function (form) {
      var btn = form.querySelector('[type="submit"]');
      form.classList.remove('is-busy');
      if (btn) {
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
        btn.classList.remove('is-btn-busy');
        if (btn.dataset.idleLabel) btn.innerHTML = btn.dataset.idleLabel;
      }
      var strip = form.querySelector('.busy-strip');
      if (strip) strip.remove();
    });
    // The veil lives on a container outside the form, so it is not reached by
    // the loop above — and a restored page still wearing it would look frozen.
    document.querySelectorAll('.busy-veil').forEach(function (v) { v.remove(); });
    document.querySelectorAll('.is-busy-scope').forEach(function (s) {
      s.classList.remove('is-busy-scope');
    });
  });

  /* EVERY POST FORM, not only the ones somebody remembered to mark.
   *
   * data-busy started as opt-in and reached about a dozen forms out of forty.
   * The rest — approving a member, saving a profile, editing a post, inviting
   * someone, voting for a charity, entering a round's results — did real work
   * on a remote database and gave no sign at all that the click had landed.
   * Every one of those is a duplicate submission waiting to happen, and the
   * ones that were marked got marked because somebody hit the problem there
   * first, not because they were the only ones that mattered.
   *
   * Opting in one template at a time cannot converge: the next form anybody
   * adds starts silent again. So the default is inverted — a POST form gets
   * feedback unless it says otherwise:
   *
   *   data-busy="Saving"   custom label (unchanged)
   *   data-busy-none       genuinely instant, no feedback wanted
   *
   * Skipped automatically:
   *   - htmx-driven forms, which gt-veil.js already covers. Two indicators for
   *     one action reads as two things happening.
   *   - GET forms. Filters and searches are navigations, and the veil path
   *     handles the ones that need it.
   */
  function autoWire() {
    document.querySelectorAll('form').forEach(function (form) {
      var method = (form.getAttribute('method') || 'get').toLowerCase();
      if (method !== 'post') return;
      if (form.hasAttribute('data-busy')) return;          // already wired
      if (form.hasAttribute('data-busy-none')) return;
      if (form.hasAttribute('hx-post') || form.hasAttribute('data-hx-post')) return;
      // Marked as auto so wire() knows not to rewrite the button's contents —
      // a blanket pass has no idea whether the button it found is a wide
      // "Save changes" or a 28px "×", and only one of those survives having
      // its label replaced.
      form.dataset.busyAuto = '1';
      form.setAttribute('data-busy', 'Working');
      wire(form);
    });
  }

  function init() {
    document.querySelectorAll('form[data-busy]').forEach(wire);
    autoWire();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  /* Forms that arrive in an htmx swap need wiring too — the dashboard's slate
     brings its own, and they would otherwise be the only silent ones left. */
  document.body && document.body.addEventListener('htmx:afterSettle', function () {
    document.querySelectorAll('form[data-busy]').forEach(function (f) {
      if (!f.dataset.busyWired) wire(f);
    });
    autoWire();
  });
})();
