/* The two-step "create department" modal.

   State lives in the DOM (which step is active, which org is picked) rather
   than in a variable, so the form posts correctly even if the script is
   interrupted: the hidden inputs are always the source of truth.

   Focus is moved deliberately on every transition. A modal that opens without
   moving focus leaves a keyboard user tabbing through the page behind it. */
(function () {
  'use strict';

  var modal = document.getElementById('deptModal');
  if (!modal) return;

  var panel   = modal.querySelector('.dm-panel');
  var steps   = modal.querySelectorAll('.dm-step');
  var dots    = modal.querySelectorAll('.dm-dot');
  var targetO = document.getElementById('dmTargetOrg');
  var typeIn  = document.getElementById('dmType');
  var ownIn   = document.getElementById('dmOwn');
  var nameIn  = document.getElementById('dmName');
  var lastFocus = null;

  function go(n) {
    steps.forEach(function (s) {
      s.classList.toggle('is-active', s.getAttribute('data-step') === String(n));
    });
    dots.forEach(function (d) {
      d.classList.toggle('on', Number(d.getAttribute('data-dot')) <= n);
    });
    panel.setAttribute('data-step', n);
    // Focus the first thing worth typing into, not the panel itself.
    var focusable = modal.querySelector('.dm-step.is-active button, .dm-step.is-active input');
    if (focusable) setTimeout(function () { focusable.focus(); }, 260);
  }

  function open() {
    lastFocus = document.activeElement;
    modal.hidden = false;
    document.body.classList.add('dept-modal-open');
    requestAnimationFrame(function () { modal.classList.add('is-open'); });
    go(1);
  }

  function close() {
    modal.classList.remove('is-open');
    document.body.classList.remove('dept-modal-open');
    // Wait for the transition so it does not vanish mid-fade.
    setTimeout(function () { modal.hidden = true; }, 220);
    if (lastFocus) lastFocus.focus();
  }

  document.querySelectorAll('[data-dept-open]').forEach(function (b) {
    b.addEventListener('click', open);
  });
  modal.querySelectorAll('[data-dept-close]').forEach(function (b) {
    b.addEventListener('click', close);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !modal.hidden) close();
  });

  /* ---- step 1: organisation ---- */
  var switchBtn = modal.querySelector('[data-dept-switch]');
  var orgList   = modal.querySelector('.dm-orglist');
  if (switchBtn && orgList) {
    switchBtn.addEventListener('click', function () {
      var showing = !orgList.hidden;
      orgList.hidden = showing;
      switchBtn.textContent = showing ? 'Not this one? Pick another' : 'Never mind, keep the current one';
    });
  }
  modal.querySelectorAll('[data-org-option]').forEach(function (opt) {
    opt.addEventListener('click', function () {
      modal.querySelectorAll('[data-org-option]').forEach(function (o) {
        o.classList.remove('is-picked');
      });
      opt.classList.add('is-picked');
      if (targetO) targetO.value = opt.getAttribute('data-org-id');
    });
  });

  var next = modal.querySelector('[data-dept-next]');
  if (next) next.addEventListener('click', function () { go(2); });
  var back = modal.querySelector('[data-dept-back]');
  if (back) back.addEventListener('click', function () { go(1); });

  /* ---- step 2: type ----
     Picking a chip fills the name field too, because "IT" is almost always
     what the department is called. It is left editable: pre-filling saves the
     common case without deciding for anyone. */
  modal.querySelectorAll('.dm-type').forEach(function (chip) {
    chip.addEventListener('click', function () {
      var already = chip.classList.contains('on');
      modal.querySelectorAll('.dm-type').forEach(function (c) {
        c.classList.remove('on'); c.setAttribute('aria-checked', 'false');
      });
      if (already) { typeIn.value = ''; return; }
      chip.classList.add('on');
      chip.setAttribute('aria-checked', 'true');
      typeIn.value = chip.getAttribute('data-type-id');
      if (ownIn) ownIn.value = '';
      if (nameIn && !nameIn.value.trim()) nameIn.value = chip.getAttribute('data-type-name');
    });
  });

  /* Typing your own clears the chosen chip: they are alternatives, and
     leaving both set would store a type the label contradicts. */
  if (ownIn) ownIn.addEventListener('input', function () {
    if (!ownIn.value.trim()) return;
    modal.querySelectorAll('.dm-type').forEach(function (c) {
      c.classList.remove('on'); c.setAttribute('aria-checked', 'false');
    });
    if (typeIn) typeIn.value = '';
    if (nameIn && !nameIn.value.trim()) nameIn.value = ownIn.value;
  });
})();
