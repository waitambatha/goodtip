/* Confirmation dialogs, in the product's own voice.
 *
 * Replaces window.confirm(), which renders as an unstyled browser chrome box
 * with the site's URL in it, cannot be themed, and — because it blocks the main
 * thread — freezes everything behind it until dismissed. It also reads as a
 * security warning rather than as a question the app is asking, which is the
 * wrong tone for "remove this reply?".
 *
 * Opt in on any form or button:
 *
 *   <form data-confirm="This cannot be undone."
 *         data-confirm-title="Remove this post?"
 *         data-confirm-ok="Remove"
 *         data-confirm-danger>
 *
 * Works on <form> (intercepts submit) and on <button>/<a> (intercepts click),
 * so a link that deletes something is covered too.
 *
 * Falls back to window.confirm if this script somehow does not run — the guard
 * is the data attribute, so an un-enhanced page still asks before destroying
 * something rather than silently going ahead.
 */
(function () {
  'use strict';

  var dialog = null;
  var pending = null;      // {el, kind} awaiting an answer
  var lastFocus = null;

  function build() {
    if (dialog) return dialog;
    dialog = document.createElement('div');
    dialog.className = 'gt-confirm';
    dialog.setAttribute('hidden', '');
    dialog.innerHTML =
      '<div class="gtc-scrim" data-gtc-cancel></div>' +
      '<div class="gtc-panel" role="alertdialog" aria-modal="true" aria-labelledby="gtcTitle" aria-describedby="gtcBody">' +
        '<div class="gtc-ic" aria-hidden="true">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">' +
            '<path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>' +
          '</svg>' +
        '</div>' +
        '<h2 id="gtcTitle"></h2>' +
        '<p id="gtcBody"></p>' +
        '<div class="gtc-actions">' +
          '<button type="button" class="gtc-btn gtc-cancel" data-gtc-cancel>Cancel</button>' +
          '<button type="button" class="gtc-btn gtc-ok" data-gtc-ok>Confirm</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(dialog);

    dialog.addEventListener('click', function (e) {
      if (e.target.closest('[data-gtc-cancel]')) close(false);
      else if (e.target.closest('[data-gtc-ok]')) close(true);
    });
    return dialog;
  }

  function open(el) {
    build();
    var title = el.getAttribute('data-confirm-title') || 'Are you sure?';
    var body = el.getAttribute('data-confirm') || '';
    var ok = el.getAttribute('data-confirm-ok') || 'Confirm';

    dialog.querySelector('#gtcTitle').textContent = title;
    dialog.querySelector('#gtcBody').textContent = body;
    dialog.querySelector('#gtcBody').hidden = !body;
    var okBtn = dialog.querySelector('.gtc-ok');
    okBtn.textContent = ok;
    dialog.classList.toggle('is-danger', el.hasAttribute('data-confirm-danger'));

    lastFocus = document.activeElement;
    dialog.hidden = false;
    document.body.classList.add('gtc-open');
    // Focus Cancel, not Confirm: the destructive action should never be one
    // stray Enter away.
    dialog.querySelector('.gtc-cancel').focus();
  }

  function close(confirmed) {
    if (!dialog || dialog.hidden) return;
    dialog.hidden = true;
    document.body.classList.remove('gtc-open');
    if (lastFocus && lastFocus.focus) lastFocus.focus();

    var p = pending;
    pending = null;
    if (!p || !confirmed) return;

    // Re-fire the original action with the guard removed, so the browser does
    // exactly what it would have done unprompted.
    var el = p.el;
    el.removeAttribute('data-confirm');
    if (p.kind === 'submit') {
      if (typeof el.requestSubmit === 'function') el.requestSubmit(p.submitter || undefined);
      else el.submit();
    } else {
      el.click();
    }
  }

  document.addEventListener('keydown', function (e) {
    if (!dialog || dialog.hidden) return;
    if (e.key === 'Escape') close(false);
    if (e.key === 'Tab') {
      // Keep focus inside the dialog while it is up.
      var focusables = dialog.querySelectorAll('button');
      var first = focusables[0], last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.hasAttribute || !form.hasAttribute('data-confirm')) return;
    e.preventDefault();
    /* AND STOP IT GOING ANY FURTHER.
     *
     * preventDefault alone cancels the navigation but the event carries on to
     * every other listener, and gt-busy.js has one on the form. So a guarded
     * form was marked busy by the submit this dialog had just cancelled: the
     * button became "Sending…", disabled, with a running strip under it — for
     * a request that had not been made and would not be until somebody
     * pressed Confirm.
     *
     * Then it never could be. gt-busy's own re-entry guard reads
     * `if (form.classList.contains('is-busy')) { e.preventDefault(); return; }`
     * so when this dialog re-fired the submit, gt-busy cancelled it as a
     * double-click. The form was stuck saying "Sending" and nothing was ever
     * posted — which is exactly what "Email members" on a story did, and what
     * every other confirm-guarded form in the product did too.
     *
     * The guarded submit is not a submission. Nothing else should see it.
     */
    e.stopPropagation();
    if (e.stopImmediatePropagation) e.stopImmediatePropagation();
    pending = {el: form, kind: 'submit', submitter: e.submitter};
    open(form);
  }, true);

  document.addEventListener('click', function (e) {
    var el = e.target.closest('[data-confirm]');
    if (!el || el.tagName === 'FORM') return;
    // A submit button inside a guarded form is handled by the submit listener.
    if (el.form && el.form.hasAttribute('data-confirm')) return;
    e.preventDefault();
    e.stopPropagation();               /* same reasoning as the submit guard */
    if (e.stopImmediatePropagation) e.stopImmediatePropagation();
    pending = {el: el, kind: 'click'};
    open(el);
  }, true);
})();

/* Auto-toasts: a server-rendered nudge that shows itself and leaves.
 *
 * Some notices are worth saying once and not worth a permanent strip in the
 * page — "your election isn't set up" is true on every load, so as a banner it
 * pushed the real content down forever and stopped being read after the second
 * visit. Rendered as a hidden element with data attributes, it becomes a toast
 * that auto-dismisses, while still carrying its action.
 *
 *   <div data-auto-toast data-toast-title="…" data-toast-msg="…"
 *        data-toast-cta="Set it up" data-toast-url="/…"
 *        data-toast-icon="ic-vote" data-toast-ms="6000" hidden></div>
 */
(function () {
  'use strict';

  function show(spec) {
    var host = document.getElementById('gtToasts');
    if (!host) return;

    var el = document.createElement('div');
    el.className = 'gt-toast';
    el.innerHTML =
      '<span class="gtt-ic"><svg><use href="#' + (spec.icon || 'ic-bell') + '"/></svg></span>' +
      '<span class="gtt-body">' +
        '<span class="gtt-eyebrow"></span>' +
        '<span class="gtt-title"></span>' +
        '<span class="gtt-msg"></span>' +
        (spec.url ? '<a class="gtt-cta"></a>' : '') +
      '</span>' +
      '<button type="button" class="gtt-x" aria-label="Dismiss">&times;</button>';

    el.querySelector('.gtt-eyebrow').textContent = spec.eyebrow || 'GoodTip';
    el.querySelector('.gtt-title').textContent = spec.title || '';
    el.querySelector('.gtt-msg').textContent = spec.msg || '';
    if (spec.url) {
      var cta = el.querySelector('.gtt-cta');
      cta.href = spec.url;
      cta.textContent = spec.cta || 'Open';
    }

    var timer = null;
    function drop() {
      if (timer) clearTimeout(timer);
      el.classList.remove('in');
      el.classList.add('out');
      setTimeout(function () { el.remove(); }, 320);
    }
    el.querySelector('.gtt-x').addEventListener('click', drop);
    // Reading the message should not be a race — hovering holds it open, and
    // the countdown restarts when the pointer leaves.
    el.addEventListener('mouseenter', function () { if (timer) clearTimeout(timer); });
    el.addEventListener('mouseleave', function () { timer = setTimeout(drop, spec.ms); });

    host.appendChild(el);
    requestAnimationFrame(function () { el.classList.add('in'); });
    timer = setTimeout(drop, spec.ms);
  }

  function init() {
    document.querySelectorAll('[data-auto-toast]').forEach(function (node, i) {
      var spec = {
        eyebrow: node.getAttribute('data-toast-eyebrow'),
        title: node.getAttribute('data-toast-title'),
        msg: node.getAttribute('data-toast-msg'),
        cta: node.getAttribute('data-toast-cta'),
        url: node.getAttribute('data-toast-url'),
        icon: node.getAttribute('data-toast-icon'),
        ms: parseInt(node.getAttribute('data-toast-ms') || '5000', 10)
      };
      // Stagger, so two notices do not land on top of each other.
      setTimeout(function () { show(spec); }, 400 + i * 500);
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
