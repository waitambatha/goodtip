/* GoodTip shared UI enhancements
   - "ddx" elegant dropdowns: progressively replaces native <select> elements
     (skips any select marked data-native) while keeping the native element as
     the source of truth, so existing change-listeners and form posts keep working.
   - Page scenes: side imagery that crossfades behind the cream backdrop. */
(function () {
  'use strict';
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------------- elegant dropdowns ---------------- */
  function closeAll(except) {
    document.querySelectorAll('.ddx.open').forEach(function (d) {
      if (d !== except) d.classList.remove('open');
    });
  }

  function enhanceSelect(sel) {
    if (sel.dataset.ddx || sel.hasAttribute('data-native') || sel.multiple) return;
    sel.dataset.ddx = '1';

    var dd = document.createElement('div');
    dd.className = 'ddx';
    sel.parentNode.insertBefore(dd, sel);
    dd.appendChild(sel);

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ddx-btn';
    btn.setAttribute('aria-haspopup', 'listbox');
    btn.innerHTML = '<span class="ddx-val"></span>' +
      '<svg class="ddx-chev" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    var menu = document.createElement('div');
    menu.className = 'ddx-menu';
    menu.setAttribute('role', 'listbox');
    dd.appendChild(btn);
    dd.appendChild(menu);

    var val = btn.querySelector('.ddx-val');

    function label() {
      var o = sel.options[sel.selectedIndex];
      var txt = o ? o.textContent.trim() : '';
      val.textContent = txt;
      val.classList.toggle('is-placeholder', !sel.value);
    }

    /* menu is rebuilt on every open so dynamically hidden/disabled options
       (e.g. the Good List sub-category filter) always render correctly */
    function build() {
      menu.innerHTML = '';
      [].slice.call(sel.options).forEach(function (o) {
        if (o.hidden || o.disabled) return;
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'ddx-opt' + (o.index === sel.selectedIndex ? ' on' : '');
        b.setAttribute('role', 'option');
        b.textContent = o.textContent.trim();
        b.addEventListener('click', function () {
          dd.classList.remove('open');
          if (sel.selectedIndex !== o.index) {
            sel.selectedIndex = o.index;
            label();
            sel.dispatchEvent(new Event('change', { bubbles: true }));
          }
        });
        menu.appendChild(b);
      });
    }

    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (sel.disabled) return;
      closeAll(dd);
      if (!dd.classList.contains('open')) build();
      dd.classList.toggle('open');
    });
    btn.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') dd.classList.remove('open');
    });
    sel.addEventListener('change', label);
    label();

    function syncDisabled() { btn.classList.toggle('is-disabled', sel.disabled); }
    new MutationObserver(syncDisabled).observe(sel, { attributes: true, attributeFilter: ['disabled'] });
    syncDisabled();
  }

  document.addEventListener('click', function () { closeAll(null); });

  /* ---------------- side scenes crossfade ---------------- */
  function runScenes() {
    document.querySelectorAll('.page-scenes .scene').forEach(function (scene) {
      var shots = scene.querySelectorAll('.shot');
      if (shots.length < 2 || reduce) return;
      var i = 0;
      setInterval(function () {
        shots[i].classList.remove('on');
        i = (i + 1) % shots.length;
        shots[i].classList.add('on');
      }, 6000);
    });
  }

  function init() {
    document.querySelectorAll(
      '.app-main select, .admin-main select, .gl-filterbar select, .mini-form select'
    ).forEach(enhanceSelect);
    runScenes();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
