/* Fixture card: the read toggle, and the last-five history dialog.
 *
 * Everything is delegated from the document rather than bound per card. The
 * fixture list is swapped wholesale by htmx while a round is live, and any
 * listener attached to a card would go with it — the cards that came back
 * would look identical and do nothing.
 */
(function () {
  'use strict';

  /* ---- "What does the form say?" ---------------------------------------
   * A press, not a hover: on a phone there is no hover, and open-by-default
   * puts three sentences on every card in the round until people stop
   * reading them. Escape and the × both close it, and focus goes back to the
   * button that opened it so a keyboard user is not dropped at the top.
   */
  function setRead(btn, open) {
    var panel = document.getElementById(btn.getAttribute('aria-controls'));
    if (!panel) return;
    panel.hidden = !open;
    btn.setAttribute('aria-expanded', String(open));
    btn.closest('.fxc-readwrap').classList.toggle('is-open', open);
  }

  document.addEventListener('click', function (e) {
    var toggle = e.target.closest('[data-reader-toggle]');
    if (toggle) {
      setRead(toggle, toggle.getAttribute('aria-expanded') !== 'true');
      return;
    }
    var close = e.target.closest('[data-reader-close]');
    if (close) {
      var wrap = close.closest('.fxc-readwrap');
      var btn = wrap && wrap.querySelector('[data-reader-toggle]');
      if (btn) { setRead(btn, false); btn.focus(); }
    }
  });

  /* ---- last five, with the scores --------------------------------------
   * The rows are already in the page inside a <template> next to the dots,
   * so opening this costs no request and works on a card htmx just swapped
   * in. One dialog is reused for every fixture.
   */
  var dlg = null;
  var opener = null;

  function ensureDialog() {
    if (dlg) return dlg;
    dlg = document.createElement('div');
    dlg.className = 'fxh-modal';
    dlg.hidden = true;
    dlg.innerHTML =
      '<div class="fxh-back" data-hist-dismiss></div>' +
      '<div class="fxh-panel" role="dialog" aria-modal="true" aria-labelledby="fxhTitle">' +
      '  <div class="fxh-head"><h3 id="fxhTitle"></h3>' +
      '    <button type="button" class="fxh-x" data-hist-dismiss aria-label="Close">&times;</button>' +
      '  </div>' +
      '  <div class="fxh-body"></div>' +
      '</div>';
    document.body.appendChild(dlg);
    return dlg;
  }

  function openHistory(btn) {
    var tpl = btn.parentElement.querySelector('[data-hist-body]');
    if (!tpl) return;
    var d = ensureDialog();
    d.querySelector('#fxhTitle').textContent = btn.dataset.team + ' — last five';
    var body = d.querySelector('.fxh-body');
    body.innerHTML = '';
    body.appendChild(tpl.content.cloneNode(true));
    d.hidden = false;
    document.body.classList.add('fxh-open');
    opener = btn;
    var x = d.querySelector('.fxh-x');
    if (x) x.focus();
  }

  function closeHistory() {
    if (!dlg || dlg.hidden) return;
    dlg.hidden = true;
    document.body.classList.remove('fxh-open');
    /* Back to the dots that opened it, not to the top of the document. */
    if (opener && document.contains(opener)) opener.focus();
    opener = null;
  }

  document.addEventListener('click', function (e) {
    var hist = e.target.closest('[data-hist]');
    if (hist) {
      /* Sibling of the pick control, so this never lands on a team. Stopped
       * anyway in case a future layout nests it again. */
      e.preventDefault();
      e.stopPropagation();
      openHistory(hist);
      return;
    }
    if (e.target.closest('[data-hist-dismiss]')) closeHistory();
  });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (dlg && !dlg.hidden) { closeHistory(); return; }
    var open = document.querySelector('.fxc-readwrap.is-open [data-reader-toggle]');
    if (open) { setRead(open, false); open.focus(); }
  });
})();

/* Picking a team on the dashboard.
 *
 * Delegated from the document, not bound per row at load. The dashboard used
 * to walk every .fxc and attach a listener to each control, which meant the
 * binding depended on that inline script running after the fixtures and
 * silently covered nothing added later. Delegation has neither problem: it
 * works for cards htmx swapped in, and for a list re-rendered by a filter.
 *
 * My Tips does not come through here — there the control is a <button> that
 * posts itself via htmx and the server returns the card already showing the
 * pick. This is only the dashboard's collect-then-confirm form.
 */
(function () {
  'use strict';

  function refresh(form) {
    var cards = form.querySelectorAll('.fxc');
    var picked = 0;
    cards.forEach(function (c) {
      /* :checked alone would count the hidden "no tip" member of the group,
         so a slate where everything had been un-picked reported itself full. */
      if (c.querySelector('input[type="radio"]:checked:not([data-no-tip])')) picked++;
    });
    var count = document.getElementById('slipCount');
    var bar = document.getElementById('slipBar');
    if (count) count.textContent = picked;
    if (bar && cards.length) bar.style.width = Math.round(picked / cards.length * 100) + '%';
  }

  /* Draw a card as picked or not. One function for both directions, because
     the two are not symmetrical by accident — every property the pick sets has
     to be the property the un-pick clears, and keeping them apart is how a
     card ends up un-picked but still wearing a green "Picked" chip. */
  function paint(card, team) {
    card.querySelectorAll('.fxc-team').forEach(function (t) { t.classList.remove('sel'); });
    card.classList.toggle('is-picked', !!team);
    if (team) team.classList.add('sel');
    var chip = card.querySelector('.fxc-state');
    if (chip) {
      chip.textContent = team ? 'Picked' : 'Open';
      /* Keep the class in step with the word, or a pick keeps the grey
         "Open" styling while claiming to be chosen — and an un-pick keeps the
         green one while claiming not to be. */
      chip.className = 'fxc-state ' + (team ? 'is-tipped' : 'is-open');
    }
  }

  document.addEventListener('click', function (e) {
    var team = e.target.closest('.fxc-team');
    if (!team) return;
    var input = team.querySelector('input[type="radio"]');
    if (!input || input.disabled) return;      /* locked, or the htmx variant */

    var card = team.closest('.fxc');
    var form = team.closest('form');
    if (!card || !form) return;

    /* Read the BEFORE state from our own class, not from input.checked.
       Clicking a label checks its radio as part of the label's activation
       behaviour, which has already run by the time the frame below fires — so
       input.checked cannot tell us whether this press was a change of mind or
       a second press on the same team. The class can: nothing but this handler
       ever sets it. */
    var undo = team.classList.contains('sel');

    /* The label checks its own radio; this only keeps the drawn state in
       step. Done on the next frame so the browser's own handling has already
       run and input.checked is true even when the click landed on the label
       rather than the input. */
    requestAnimationFrame(function () {
      if (undo) {
        /* A radio group has no "uncheck" — the only way back to none is to
           check a different member, which is what the hidden one is for. It
           also carries the take-back to the server on confirm; clearing
           input.checked on its own would look right and post nothing, and the
           tip would still be there next time the page loaded. */
        var none = card.querySelector('input[data-no-tip]');
        if (none) none.checked = true;
        else input.checked = false;
        paint(card, null);
      } else {
        input.checked = true;
        paint(card, team);
      }
      refresh(form);
    });
  });
})();
