/* Auto-submit for the emailed verification code.
 *
 * The client's report: "when I enter the last code it should automatically
 * start logging me in — no need to press enter or confirm." The code has a
 * known, fixed length, so the moment the sixth digit lands there is nothing
 * left to ask and the Verify button is a second action for no reason.
 *
 * Progressive enhancement, not a replacement: the form still posts normally
 * with JS off, and the button stays on the page for anyone who reaches it by
 * keyboard or who pastes a code with the field already full.
 */
(function () {
  'use strict';

  function digitsOf(value) {
    return (value || '').replace(/\D+/g, '');
  }

  function wire(host) {
    // The attribute goes on the field wrapper, not the input — the input is
    // rendered by the Django widget and the template does not build its tag.
    var input = host.matches('input') ? host : host.querySelector('input');
    if (!input) return;
    var form = input.closest('form');
    if (!form) return;

    var want = parseInt(host.getAttribute('data-otp-length'), 10) || 6;
    // One shot. Without this, the `input` event that fires while the browser
    // is already navigating away — an autofilled SMS/email code arriving in
    // two events, say — would post the form a second time.
    var fired = false;

    function submitNow() {
      if (fired) return;
      fired = true;
      // requestSubmit, not submit(): it runs validation and fires the submit
      // event, which is what gt-busy.js listens for to show "Checking your
      // code". form.submit() would skip both and the page would sit there
      // looking like nothing happened.
      if (form.requestSubmit) form.requestSubmit();
      else form.submit();
    }

    input.addEventListener('input', function () {
      var digits = digitsOf(input.value);

      // Trim anything past the code's length so a stray keystroke cannot push
      // a correct code out of range — and so the count below is honest.
      if (digits.length > want) digits = digits.slice(0, want);

      // Normalise as they type: people paste "123 456" out of the email, and
      // phone keyboards add a trailing space. clean_code strips these
      // server-side anyway; doing it here is what lets the length test fire.
      if (input.value !== digits) {
        input.value = digits;
      }

      if (digits.length === want) {
        // A tick of delay so the last digit is painted before the page starts
        // moving — otherwise the field looks like it never took the keystroke.
        input.blur();
        window.setTimeout(submitNow, 60);
      }
    });

    // A wrong code re-renders this page with the field refilled. Clearing it
    // means the next attempt starts from empty and the auto-submit can fire
    // again, rather than the member having to select-all first.
    if (form.querySelector('.fld-err') || document.querySelector('.flash.error')) {
      input.value = '';
    }

    if (input.autofocus || input.hasAttribute('autofocus')) input.focus();
  }

  function init() {
    document.querySelectorAll('[data-otp-length]').forEach(wire);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
