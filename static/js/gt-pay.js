/* The pay page (client, 9 and 10 Oct 2026): "whatever happens in GoodTip
 * stays in GoodTip" and, straight after, "let me see some kind of a loader
 * ... a nice animation that says the card has declined ... a nice
 * successful message".
 *
 * Stripe's embedded Checkout (the first version of this page) drew its own
 * button and handled every outcome inside its own iframe, with no event the
 * parent page could see — there was nowhere to hang GoodTip's own loader or
 * a decline animation. A Payment Element confirmed with
 * stripe.confirmSetup() hands the outcome back as an ordinary promise, so
 * this file owns the whole sequence instead: busy the moment Subscribe is
 * pressed, a decline overlay with the real Stripe message and a ten-second
 * countdown back to the form, a success overlay once the subscription is
 * genuinely real (confirmed server-side, not just "the browser says so")
 * that counts down to the billing page.
 */
(function () {
  'use strict';

  var form = document.getElementById('pay-form');
  if (!form) return;

  var key = form.getAttribute('data-key');
  var secret = form.getAttribute('data-secret');
  var confirmUrl = form.getAttribute('data-confirm-url');
  var successUrl = form.getAttribute('data-success-url');
  var csrf = form.getAttribute('data-csrf');

  var btn = form.querySelector('[data-pay-btn]');
  var busy = document.querySelector('[data-pay-busy]');
  var bad = document.querySelector('[data-pay-bad]');
  var badMsg = document.querySelector('[data-pay-bad-msg]');
  var badCount = document.querySelector('[data-pay-count]');
  var badBar = document.querySelector('[data-pay-bar]');
  var retryBtn = document.querySelector('[data-pay-retry]');
  var good = document.querySelector('[data-pay-good]');
  var goodCount = document.querySelector('[data-pay-good-count]');

  function show(el) { if (el) { el.hidden = false; el.setAttribute('aria-hidden', 'false'); } }
  function hide(el) { if (el) { el.hidden = true; el.setAttribute('aria-hidden', 'true'); } }

  function fail(why) {
    form.innerHTML = '<p class="pay-fallback">' + why + '</p>';
  }
  if (!window.Stripe || !key || !secret) {
    return fail('The payment form could not be loaded. Check your connection and reload the page.');
  }

  var stripe = Stripe(key);
  var elements = stripe.elements({
    clientSecret: secret,
    appearance: {
      theme: 'stripe',
      variables: {
        colorPrimary: '#2A7337',
        colorText: '#0F2E22',
        colorDanger: '#B23434',
        fontFamily: 'Archivo, Arial, sans-serif',
        borderRadius: '12px',
      },
    },
  });
  var paymentElement = elements.create('payment');
  paymentElement.mount('#payment-element');

  var declineInterval = null, declineTimer = null;
  function clearDeclineTimers() {
    clearInterval(declineInterval); clearTimeout(declineTimer);
    declineInterval = declineTimer = null;
  }
  function hideBad() {
    clearDeclineTimers();
    hide(bad);
    btn.disabled = false;
  }
  function showBad(message) {
    hide(busy); hide(good);
    badMsg.textContent = message || "Your card couldn't be used for this payment.";
    clearDeclineTimers();
    var n = 10;
    badCount.textContent = n;
    if (badBar) badBar.style.width = '0%';
    show(bad);
    declineInterval = setInterval(function () {
      n -= 1;
      badCount.textContent = Math.max(n, 0);
      if (badBar) badBar.style.width = ((10 - n) * 10) + '%';
      if (n <= 0) hideBad();
    }, 1000);
  }
  if (retryBtn) retryBtn.addEventListener('click', hideBad);

  function showGood() {
    hide(busy); hide(bad);
    show(good);
    var n = 3;
    goodCount.textContent = n;
    var iv = setInterval(function () {
      n -= 1;
      goodCount.textContent = Math.max(n, 0);
      if (n <= 0) { clearInterval(iv); window.location.href = successUrl; }
    }, 1000);
  }

  function setBusy(on) {
    btn.disabled = on;
    if (on) { hide(bad); hide(good); show(busy); } else { hide(busy); }
  }

  function confirmOnServer(setupIntentId) {
    return fetch(confirmUrl, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'X-CSRFToken': csrf, 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'setup_intent_id=' + encodeURIComponent(setupIntentId),
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; });
    }).catch(function () {
      return { ok: false, error: "Couldn't reach GoodTip to finish the subscription." };
    });
  }

  function pay() {
    setBusy(true);
    stripe.confirmSetup({
      elements: elements,
      // Used only if Stripe genuinely has to leave the page (rare — most
      // extra bank verification runs in a modal over this one); checked for
      // on load, below.
      confirmParams: { return_url: window.location.href },
      redirect: 'if_required',
    }).then(function (result) {
      if (result.error) {
        setBusy(false);
        showBad(result.error.message);
        return;
      }
      var intent = result.setupIntent;
      if (!intent || intent.status !== 'succeeded') {
        setBusy(false);
        showBad('That card needs another step to be verified. Please try again.');
        return;
      }
      return confirmOnServer(intent.id).then(function (data) {
        if (!data.ok) {
          setBusy(false);
          showBad(data.error || 'Something went wrong finishing your subscription.');
          return;
        }
        showGood();
      });
    }, function () {
      setBusy(false);
      showBad('Something went wrong talking to Stripe. Please try again.');
    });
  }

  form.addEventListener('submit', function (e) { e.preventDefault(); pay(); });

  /* A card that genuinely needed a redirect (not just a modal) lands back
     here with these in the query string — pick the conversation back up
     rather than showing a blank, re-armed form. */
  var qs = new URLSearchParams(window.location.search);
  var redirectStatus = qs.get('redirect_status');
  var siFromUrl = qs.get('setup_intent');
  if (redirectStatus && siFromUrl) {
    window.history.replaceState(null, '', window.location.pathname);
    if (redirectStatus === 'succeeded') {
      setBusy(true);
      confirmOnServer(siFromUrl).then(function (data) {
        if (data.ok) showGood();
        else { setBusy(false); showBad(data.error || 'Something went wrong finishing your subscription.'); }
      });
    } else {
      showBad("That card wasn't verified in time. Please try again.");
    }
  }
})();
