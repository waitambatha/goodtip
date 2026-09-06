/* GoodTip shared UI enhancements
   - "ddx" elegant dropdowns: progressively replaces native <select> elements
     (skips any select marked data-native) while keeping the native element as
     the source of truth, so existing change-listeners and form posts keep working.
   Image rotation lives in gt-scenes.js. */
(function () {
  'use strict';

  /* ---------------- elegant dropdowns ---------------- */
  function closeAll(except) {
    document.querySelectorAll('.ddx.open').forEach(function (d) {
      if (d !== except) d.classList.remove('open');
    });
  }

  function enhanceSelect(sel) {
    if (sel.dataset.ddx || sel.hasAttribute('data-native') || sel.multiple) return;
    sel.dataset.ddx = '1';

    /* Tell the floating-label field it no longer holds a native select. The
       label is positioned over the control and floats up when the select has
       focus or a value — neither of which happens any more, because the select
       is now a hidden 1px element and the visible control is a button beside
       it. Without this the label sits on top of the button's text. */
    var field = sel.closest('.field');
    if (field) field.classList.add('has-ddx');

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

  function init() {
    /* The public contact form was missing from this list, so its "I'm
       interested in…" field was the one raw native select left on the site —
       an OS-drawn popup in the middle of a designed dark panel. */
    document.querySelectorAll(
      '.app-main select, .admin-main select, .gl-filterbar select, ' +
      '.mini-form select, .contact-shell select'
    ).forEach(enhanceSelect);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();

/* Nav "Manage" dropdown. Click to open, click-away / Escape to close. */
(function () {
  'use strict';
  document.querySelectorAll('[data-anmenu]').forEach(function (menu) {
    var btn = menu.querySelector('.an-menu-btn');
    var panel = menu.querySelector('.an-menu-panel');
    if (!btn || !panel) return;

    function close() {
      panel.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
    }
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var wasHidden = panel.hidden;
      panel.hidden = !wasHidden;
      btn.setAttribute('aria-expanded', String(wasHidden));
      // Re-measure this panel's scroll lists now that they're actually
      // visible — see the gt:rescan listener in gt-lists.js for why.
      if (wasHidden) {
        document.dispatchEvent(new CustomEvent('gt:rescan', { detail: panel }));
      }
    });
    document.addEventListener('click', function (e) {
      if (!menu.contains(e.target)) close();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') close();
    });
  });
})();

/* Switch-organisation filter — only rendered once an account has more than a
   handful of orgs (see .an-ctx-filter-wrap in app_base.html). Everything is
   already in the DOM, so this is a plain text match, no fetch. */
(function () {
  'use strict';
  document.querySelectorAll('[data-anctx-filter]').forEach(function (input) {
    var list = input.closest('.an-ctx').querySelector('[data-anctx-list]');
    if (!list) return;
    var rows = Array.prototype.slice.call(list.querySelectorAll('[data-anctx-row-form]'));
    input.addEventListener('input', function () {
      var q = input.value.trim().toLowerCase();
      rows.forEach(function (form) {
        var name = form.querySelector('.mi-txt b').textContent.toLowerCase();
        form.hidden = q.length > 0 && name.indexOf(q) === -1;
      });
    });
    input.addEventListener('click', function (e) { e.stopPropagation(); });
  });
})();

/* Hover (or focus, for keyboard use) an organisation row in the nav switcher
   to preview ITS groups beside it, without first switching into it. Every
   org's groups block is already in the DOM (app_base.html, data-org-groups)
   — this only ever toggles which one is hidden, no fetch, so the preview is
   instant. Each block's own forms already point at that org's switch_org /
   switch_group, so clicking a group works whichever org's block is showing. */
(function () {
  'use strict';
  document.querySelectorAll('.an-ctx').forEach(function (menu) {
    var groupsCol = menu.querySelector('[data-anctx-groups-col]');
    if (!groupsCol) return;
    var blocks = groupsCol.querySelectorAll('[data-org-groups]');
    var timer = null;

    function show(orgId) {
      var matched = null;
      blocks.forEach(function (b) {
        var on = b.getAttribute('data-org-groups') === orgId;
        b.hidden = !on;
        if (on) matched = b;
      });
      // The block just un-hidden was measured at 0 height while hidden (see
      // the gt:rescan listener in gt-lists.js) — it needs a fresh look now.
      if (matched) document.dispatchEvent(new CustomEvent('gt:rescan', { detail: matched }));
    }

    menu.querySelectorAll('[data-anctx-org-row]').forEach(function (row) {
      var orgId = row.getAttribute('data-org-id');
      function preview() {
        clearTimeout(timer);
        timer = setTimeout(function () { show(orgId); }, 80);
      }
      row.addEventListener('mouseenter', preview);
      row.addEventListener('focus', preview);
    });
  });
})();

/* Recap conversation starters.
 *
 * A starter is a suggestion, not a message. Tapping one drops the line into
 * that member's own reply box and puts the cursor at the end of it, so the
 * thing that lands on the Wall is something they chose to send and could
 * edit first. Nothing here posts, and nothing on the Wall is ever written in
 * a member's voice.
 *
 * Delegated from the document so it survives an htmx swap of the feed.
 */
(function () {
  'use strict';
  document.addEventListener('click', function (e) {
    var chip = e.target.closest('[data-recap-starter]');
    if (!chip) return;
    var card = document.getElementById('post-' + chip.dataset.post);
    var box = card && card.querySelector('.gpt-replybox textarea');
    if (!box) return;
    box.value = chip.textContent.trim();
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
    /* the reply box grows with its content (data-grow) */
    box.dispatchEvent(new Event('input', { bubbles: true }));
    box.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  });
})();

/* ---------------------------------------------------------------------------
 * CARRY: AN ORGANISATION AND THE GROUPS INSIDE IT
 *
 * partials/_carry_rooms.html renders each organisation as one master checkbox
 * over a disclosure of its rooms. The master carries no `name` — the rooms are
 * what post — so this is the whole of the connection between them:
 *
 *   master toggled   → every room in that organisation follows it
 *   a room toggled   → the master shows all / none / some
 *
 * "Some" is the indeterminate state rather than a third class, because that is
 * what the property is for and it is what a screen reader announces as "mixed".
 *
 * Delegated from the document, and it has to be: this markup is injected into
 * the confirm sheet with innerHTML after a fetch, so nothing bound at load
 * would ever see it. The same handler serves the standalone review page, where
 * the markup is there from the start.
 * ------------------------------------------------------------------------- */
(function () {
  'use strict';

  function children(org) {
    return Array.prototype.slice.call(org.querySelectorAll('[data-cs-child]'));
  }

  function paintMaster(org) {
    var master = org.querySelector('[data-cs-master]');
    if (!master) return;
    var kids = children(org);
    if (!kids.length) return;
    var on = kids.filter(function (k) { return k.checked; }).length;
    master.checked = on === kids.length;
    master.indeterminate = on > 0 && on < kids.length;
    /* The card dims when nothing under it is going anywhere, so "off" reads
       as a state of the organisation rather than only of a small box. */
    org.classList.toggle('is-off', on === 0);
  }

  document.addEventListener('change', function (e) {
    var box = e.target;
    if (!box || box.type !== 'checkbox') return;
    var org = box.closest ? box.closest('[data-cs-org]') : null;
    if (!org) return;

    if (box.hasAttribute('data-cs-master')) {
      /* Clicking a mixed master means "all of it" — the alternative is a
         control whose next state you cannot predict from looking at it. */
      var want = box.indeterminate ? true : box.checked;
      box.indeterminate = false;
      box.checked = want;
      children(org).forEach(function (k) { k.checked = want; });
      org.classList.toggle('is-off', !want);
    } else if (box.hasAttribute('data-cs-child')) {
      paintMaster(org);
    }
  });

  /* Every organisation on screen starts consistent, including ones that
     arrive later inside the sheet. */
  function paintAll(root) {
    (root || document).querySelectorAll('[data-cs-org]').forEach(paintMaster);
  }
  document.addEventListener('DOMContentLoaded', function () { paintAll(); });
  document.body && paintAll();
  document.addEventListener('gt:carry-rendered', function () { paintAll(); });
})();

/* ---------------------------------------------------------------------------
 * THE CARD STRIP THAT SWITCHES A PANEL
 *
 * Members and Charities are a row of cards over one box: pressing a card swaps
 * the box (hx-target=".ppanel") rather than reloading the screen, so the
 * sheet's loader never plays for a change to one region of a page already on
 * screen — "it should not be the whole page loading when it's an action where
 * we have a loading loader".
 *
 * htmx swaps the panel; it cannot know which card should now look pressed,
 * because the response is the panel and the panel does not know what pointed at
 * it. That is this. Read off the anchor, set before the request, so the card
 * lights the moment it is pressed rather than when the bytes land.
 * ------------------------------------------------------------------------- */
(function () {
  'use strict';

  document.addEventListener('click', function (e) {
    var card = e.target.closest && e.target.closest('[data-panel-link]');
    if (!card) return;
    var strip = card.closest('.pcards');
    if (!strip) return;
    strip.querySelectorAll('.pcard').forEach(function (c) {
      c.classList.toggle('on', c === card);
    });
  });

  /* The panel being fetched is the one that dims — htmx puts .htmx-request on
     the element that MADE the request, which is a card, so without this the
     card would flicker and the box it is fetching would sit still. */
  document.body.addEventListener('htmx:beforeRequest', function (e) {
    var t = e.detail && e.detail.elt;
    if (!t || !t.closest || !t.closest('[data-panel-link]')) return;
    var panel = document.querySelector('.ppanel');
    if (panel) panel.classList.add('is-swapping');
  });
  document.body.addEventListener('htmx:afterSwap', function () {
    var panel = document.querySelector('.ppanel');
    if (panel) panel.classList.remove('is-swapping');
  });
})();
