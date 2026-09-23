/* Public forms: submit in place, say thank you, then come back.
 *
 * CLIENT, 22 SEP 2026: "for any other forms — be it enquiry or contact form or
 * this one that Ian has generated — make sure when all is entered, first of
 * all we will have a nice loader, not the whole page but just that form
 * section, and it will also say thank you or something to show whatever they
 * were doing is successful. Then after like 3 seconds the form returns."
 *
 * WHAT WAS THERE BEFORE, and why it did not do this. Every public form was an
 * ordinary synchronous POST: the browser left the page, the server saved the
 * row, and it redirected back with ?sent=1 so the template could render a
 * confirmation instead of the form. That works and it is what still happens
 * with JavaScript off — but it costs a full page load, it throws away the
 * reader's scroll position on a page that is thousands of pixels long, and the
 * confirmation is then permanent until they navigate again. There is no moment
 * where the form "comes back", because there is no state to come back from.
 *
 * So this intercepts the submit and does the same work over fetch. Three
 * states, in one place on the page, none of which move the reader:
 *
 *   1. BUSY   — a veil over the form's own shell, with the label the form
 *               names in data-busy. Scoped, because the client asked for the
 *               section and not the page, and because veiling the page to
 *               save a row is a lie about how much is happening.
 *   2. DONE   — the thank-you, which REPLACES the form. Leaving a filled-in
 *               form on screen under a success message is the most reliable
 *               way to get the same person submitted twice.
 *   3. BACK   — after data-return-after ms, a clean empty form again.
 *
 * PROGRESSIVE ENHANCEMENT IS NOT DECORATION HERE. These forms carry the only
 * leads this business gets. If fetch is missing, if the network drops, if the
 * server answers with something that is not JSON — the handler steps aside and
 * lets the browser submit the form the old way. A lead lost to a clever
 * submit handler is worse than a page reload.
 *
 * Opt in:
 *   <div data-form-shell>
 *     <form data-ajax data-busy="Sending your message" data-return-after="3500">
 */
(function () {
  'use strict';

  var RETURN_AFTER = 3500;   /* "like 3 seconds" — plus time to read it */
  var MIN_BUSY = 480;        /* a veil that flashes reads as a glitch */

  if (!window.fetch || !window.FormData) return;

  function shellFor(form) {
    return form.closest('[data-form-shell]') || form.parentElement;
  }

  /* ---------------------------------------------------------------- veil -- */

  function showVeil(shell, label) {
    var veil = document.createElement('div');
    veil.className = 'gtf-veil';
    veil.setAttribute('role', 'status');
    veil.setAttribute('aria-live', 'polite');
    veil.innerHTML =
      '<div class="gtf-veil-in">' +
        '<span class="gtf-spin" aria-hidden="true"><i></i><i></i><i></i></span>' +
        '<span class="gtf-veil-label"></span>' +
      '</div>';
    veil.querySelector('.gtf-veil-label').textContent = label || 'Sending';
    shell.appendChild(veil);
    /* Next frame, so the transition has a start value to animate from. */
    requestAnimationFrame(function () { veil.classList.add('on'); });
    return veil;
  }

  function hideVeil(veil) {
    if (!veil) return;
    veil.classList.remove('on');
    setTimeout(function () { if (veil.parentNode) veil.remove(); }, 260);
  }

  /* -------------------------------------------------------------- states -- */

  function showDone(shell, form, data) {
    var panel = document.createElement('div');
    panel.className = 'gtf-done';
    panel.setAttribute('role', 'status');
    panel.innerHTML =
      '<span class="gtf-tick" aria-hidden="true">' +
        '<svg viewBox="0 0 24 24"><path d="M4 12.5 9.5 18 20 6.5"/></svg>' +
      '</span>' +
      '<h3></h3><p></p>';
    panel.querySelector('h3').textContent = data.title || 'Thank you.';
    panel.querySelector('p').textContent = data.message || 'That came through.';

    form.classList.add('gtf-leaving');
    setTimeout(function () {
      form.hidden = true;
      shell.appendChild(panel);
      requestAnimationFrame(function () { panel.classList.add('on'); });
    }, 180);
    return panel;
  }

  function restore(shell, form, panel, pristine) {
    panel.classList.remove('on');
    setTimeout(function () {
      if (panel.parentNode) panel.remove();
      /* A FRESH form, not the one they filled in. reset() leaves :invalid
         styling and any server-set values behind; the stashed clone is the
         form as the page first rendered it. */
      var fresh = pristine.cloneNode(true);
      fresh.hidden = false;
      fresh.classList.remove('gtf-leaving', 'gtf-entering');
      form.replaceWith(fresh);
      wire(fresh);
      fresh.classList.add('gtf-entering');
      requestAnimationFrame(function () { fresh.classList.remove('gtf-entering'); });
    }, 260);
  }

  function showError(form, message) {
    var box = form.querySelector('[data-form-error]');
    if (!box) {
      box = document.createElement('p');
      box.className = 'gtf-error';
      box.setAttribute('data-form-error', '');
      box.setAttribute('role', 'alert');
      form.insertBefore(box, form.firstElementChild);
    }
    box.textContent = message;
    box.hidden = false;
  }

  /* --------------------------------------------------------------- wire --- */

  function wire(form) {
    if (form.dataset.gtfWired) return;
    form.dataset.gtfWired = '1';

    /* Stashed BEFORE anybody types into it. */
    var pristine = form.cloneNode(true);
    delete pristine.dataset.gtfWired;

    form.addEventListener('submit', function (e) {
      /* Let the browser do its own required/type checking first — its
         messages are localised and attached to the right field, which is
         more than a rolled one would be. */
      if (form.noValidate === false && !form.checkValidity()) return;

      e.preventDefault();

      var shell = shellFor(form);
      var startedAt = Date.now();
      var veil = showVeil(shell, form.getAttribute('data-busy'));
      var existing = form.querySelector('[data-form-error]');
      if (existing) existing.hidden = true;

      fetch(form.action || window.location.pathname, {
        method: (form.method || 'post').toUpperCase(),
        body: new FormData(form),
        headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' },
        credentials: 'same-origin',
      })
        .then(function (r) {
          if (!r.ok && r.status >= 500) throw new Error('server');
          return r.json();
        })
        .then(function (data) {
          /* Hold the veil to MIN_BUSY. A local server answers in 30ms and a
             veil that appears and vanishes inside one frame reads as the page
             flickering rather than as work being done. */
          var wait = Math.max(0, MIN_BUSY - (Date.now() - startedAt));
          setTimeout(function () {
            hideVeil(veil);
            if (data && data.ok) {
              var panel = showDone(shell, form, data);
              var after = parseInt(form.getAttribute('data-return-after'), 10);
              setTimeout(function () {
                restore(shell, form, panel, pristine);
              }, isNaN(after) ? RETURN_AFTER : after);
            } else {
              showError(form, (data && data.error) || 'That did not go through — try again.');
            }
          }, wait);
        })
        .catch(function () {
          /* THE LEAD MATTERS MORE THAN THE ANIMATION. Anything unexpected —
             offline, a proxy returning HTML, a 500 — and we hand the form
             back to the browser, which will post it the ordinary way and
             follow the redirect. The visitor sees a page load instead of a
             thank-you panel, and their message still arrives. */
          hideVeil(veil);
          form.dataset.gtfWired = '';
          form.submit();
        });
    });
  }

  function scan(root) {
    (root || document).querySelectorAll('form[data-ajax]').forEach(wire);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { scan(); });
  } else {
    scan();
  }
  /* Forms that arrive in an htmx swap. */
  document.body && document.body.addEventListener('htmx:afterSettle', function (e) {
    scan(e.target);
  });
})();
