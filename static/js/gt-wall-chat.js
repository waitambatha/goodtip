/* The Wall's chat box and room list (client, 9 Nov 2026).
 *
 *   1. EMOJI      the same 1,870-emoji picker as Messages (static/data/emoji.json),
 *                 tabs and search, inserted at the caret
 *   2. PICTURE    the picture button opens the file chooser; a thumbnail shows
 *                 above the line until it is sent or removed
 *   3. PICK       "share a pick" lists this round's picks; choosing one puts a
 *                 chip above the line
 *   4. ROOM MENU  right-click a competition, hold it on a phone, or press its
 *                 dots: Pin to the top, Add to favourites, Open
 *   5. FILTER     All or Favourites, remembered on this device
 *   6. REACTIONS  are in the press menu of a post now; choosing one closes it
 *
 * Everything is delegated: a room change swaps the whole Wall in by htmx.
 */
(function () {
  'use strict';

  function composer() { return document.querySelector('[data-wl-composer]'); }

  /* ---------------- 1. EMOJI ---------------- */
  var emojiData = null;
  function loadEmoji(url) {
    if (emojiData) return emojiData;
    emojiData = fetch(url, { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .catch(function (err) { emojiData = null; throw err; });
    return emojiData;
  }
  function esc(t) { return String(t).replace(/"/g, '&quot;'); }
  function paint(menu, data, key, term) {
    var grid = menu.querySelector('[data-emoji-grid]');
    var note = menu.querySelector('[data-emoji-note]');
    var rows = [];
    if (term) {
      for (var g = 0; g < data.groups.length && rows.length < 240; g++) {
        var list = data.groups[g].emoji;
        for (var i = 0; i < list.length && rows.length < 240; i++) {
          if (list[i][1].indexOf(term) !== -1) rows.push(list[i]);
        }
      }
    } else {
      var group = data.groups.filter(function (x) { return x.key === key; })[0];
      rows = group ? group.emoji : [];
    }
    grid.innerHTML = rows.map(function (r) {
      return '<button type="button" title="' + esc(r[1]) + '">' + r[0] + '</button>';
    }).join('');
    if (note) { note.hidden = rows.length !== 0; note.textContent = term ? 'Nothing matches that.' : ''; }
    menu.querySelectorAll('[data-emoji-tab]').forEach(function (t) {
      t.classList.toggle('on', !term && t.getAttribute('data-emoji-tab') === key);
    });
  }
  function fill(menu) {
    if (menu.hasAttribute('data-emoji-ready')) return;
    menu.setAttribute('data-emoji-ready', '');
    var note = menu.querySelector('[data-emoji-note]');
    loadEmoji(menu.getAttribute('data-emoji-src')).then(function (data) {
      menu.querySelector('[data-emoji-tabs]').innerHTML = data.groups.map(function (g) {
        return '<button type="button" class="cc-emoji-tab" data-emoji-tab="' + g.key +
               '" title="' + esc(g.label) + '" aria-label="' + esc(g.label) + '">' + g.tab + '</button>';
      }).join('');
      menu.setAttribute('data-emoji-key', data.groups[0].key);
      paint(menu, data, data.groups[0].key, '');
    }).catch(function () {
      menu.removeAttribute('data-emoji-ready');
      if (note) { note.hidden = false; note.textContent = 'Could not load the emoji. Try again.'; }
    });
  }
  document.addEventListener('toggle', function (e) {
    if (!e.target.matches || !e.target.matches('[data-wl-composer] .cc-emoji[open]')) return;
    var menu = e.target.querySelector('[data-emoji-menu]');
    if (menu) fill(menu);
  }, true);
  document.addEventListener('click', function (e) {
    var tab = e.target.closest && e.target.closest('[data-wl-composer] [data-emoji-tab]');
    if (tab) {
      e.preventDefault(); e.stopPropagation();
      var menu = tab.closest('[data-emoji-menu]');
      var key = tab.getAttribute('data-emoji-tab');
      var box = menu.querySelector('[data-emoji-search]'); if (box) box.value = '';
      menu.setAttribute('data-emoji-key', key);
      if (emojiData) emojiData.then(function (d) { paint(menu, d, key, ''); });
      return;
    }
    var btn = e.target.closest && e.target.closest('[data-wl-composer] [data-emoji-grid] button');
    if (!btn) return;
    var form = btn.closest('[data-wl-composer]');
    var ta = form.querySelector('textarea[name="body"]');
    var at = ta.selectionStart == null ? ta.value.length : ta.selectionStart;
    var to = ta.selectionEnd == null ? at : ta.selectionEnd;
    var ch = btn.textContent;
    ta.value = ta.value.slice(0, at) + ch + ta.value.slice(to);
    ta.selectionStart = ta.selectionEnd = at + ch.length;
    ta.focus();
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    /* Stays open, as WhatsApp's does: people add three faces, not one. */
  }, true);
  document.addEventListener('input', function (e) {
    if (!e.target.matches || !e.target.matches('[data-wl-composer] [data-emoji-search]')) return;
    var menu = e.target.closest('[data-emoji-menu]');
    var term = e.target.value.trim().toLowerCase();
    if (emojiData) emojiData.then(function (d) { paint(menu, d, menu.getAttribute('data-emoji-key'), term); });
  });

  /* Popovers in the box close on a press elsewhere, and on Escape. */
  function closePops(except) {
    document.querySelectorAll('[data-wl-composer] details[open]').forEach(function (d) {
      if (!except || !d.contains(except)) d.open = false;
    });
  }
  document.addEventListener('click', function (e) { closePops(e.target); });

  /* ---------------- 2. PICTURE  /  3. PICK ---------------- */
  function refreshAttached(form) {
    var img = form.querySelector('[data-wl-imgprev]');
    var pick = form.querySelector('[data-wl-pickprev]');
    form.querySelector('[data-wl-attached]').hidden = img.hidden && pick.hidden;
  }
  document.addEventListener('click', function (e) {
    var t = e.target.closest && e.target.closest('[data-wl-pickimage]');
    if (t) { t.closest('[data-wl-composer]').querySelector('[data-wl-image]').click(); return; }

    var p = e.target.closest && e.target.closest('[data-wl-pick]');
    if (p) {
      var form = p.closest('[data-wl-composer]');
      form.querySelector('[data-wl-tip]').value = p.getAttribute('data-wl-pick');
      form.querySelector('[data-wl-picktext]').textContent = p.textContent.replace(/^\s*★\s*/, '');
      form.querySelector('[data-wl-pickprev]').hidden = false;
      refreshAttached(form);
      p.closest('details').open = false;
      return;
    }

    var c = e.target.closest && e.target.closest('[data-wl-clear]');
    if (c) {
      var f = c.closest('[data-wl-composer]');
      if (c.getAttribute('data-wl-clear') === 'image') {
        f.querySelector('[data-wl-image]').value = '';
        f.querySelector('[data-wl-imgprev]').hidden = true;
      } else {
        f.querySelector('[data-wl-tip]').value = '';
        f.querySelector('[data-wl-pickprev]').hidden = true;
      }
      refreshAttached(f);
    }
  });
  document.addEventListener('change', function (e) {
    if (!e.target.matches || !e.target.matches('[data-wl-image]')) return;
    var form = e.target.closest('[data-wl-composer]');
    var file = e.target.files && e.target.files[0];
    var prev = form.querySelector('[data-wl-imgprev]');
    if (!file) { prev.hidden = true; refreshAttached(form); return; }
    if (file.size > 8 * 1024 * 1024) {
      e.target.value = '';
      prev.hidden = true; refreshAttached(form);
      form.querySelector('textarea[name="body"]').placeholder = 'That picture is over 8 MB. Try a smaller one.';
      return;
    }
    prev.querySelector('img').src = URL.createObjectURL(file);
    prev.hidden = false;
    refreshAttached(form);
    form.querySelector('textarea[name="body"]').placeholder = 'Add a caption…';
  });

  /* A post can be only a picture or only a pick: the box must not refuse to
     send because the line is empty. gt-chat.js stops an empty Enter. */
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' || e.shiftKey) return;
    var ta = e.target;
    if (!ta.matches || !ta.matches('[data-wl-composer] textarea[name="body"]')) return;
    var form = ta.closest('[data-wl-composer]');
    if (ta.value.trim()) return;
    var hasImg = form.querySelector('[data-wl-image]').files.length;
    var hasPick = form.querySelector('[data-wl-tip]').value;
    if (hasImg || hasPick) {
      e.preventDefault(); e.stopImmediatePropagation();
      if (form.requestSubmit) form.requestSubmit(); else form.submit();
    }
  }, true);

  /* ---------------- 4. ROOM MENU ---------------- */
  var open = null, home = null, holdTimer = null;
  function closeMenu() {
    if (!open) return;
    open.hidden = true;
    if (home && document.body.contains(home)) home.appendChild(open); else open.remove();
    open = null; home = null;
  }
  function openMenu(li, x, y) {
    var menu = li.querySelector('[data-gwr-menu]');
    if (!menu) return;
    closeMenu();
    home = li; open = menu;
    document.body.appendChild(menu);       /* out of the scrolling list, so it can sit anywhere */
    menu.hidden = false;
    var b = menu.getBoundingClientRect();
    menu.style.left = Math.max(10, Math.min(x, window.innerWidth - b.width - 10)) + 'px';
    menu.style.top = Math.max(10, Math.min(y, window.innerHeight - b.height - 10)) + 'px';
    var first = menu.querySelector('button, a'); if (first) first.focus({ preventScroll: true });
  }
  document.addEventListener('click', function (e) {
    var more = e.target.closest && e.target.closest('[data-gwr-more]');
    if (more) {
      e.preventDefault(); e.stopPropagation();
      var r = more.getBoundingClientRect();
      openMenu(more.closest('.gwr-room'), r.right - 210, r.bottom + 4);
      return;
    }
    if (open && !open.contains(e.target)) closeMenu();
  });
  document.addEventListener('contextmenu', function (e) {
    var li = e.target.closest && e.target.closest('.gwr-room');
    if (!li || !li.querySelector('[data-gwr-menu]')) return;
    e.preventDefault();
    openMenu(li, e.clientX, e.clientY);
  });
  document.addEventListener('touchstart', function (e) {
    var li = e.target.closest && e.target.closest('.gwr-room');
    if (!li || !li.querySelector('[data-gwr-menu]')) return;
    var t = e.touches[0], x = t.clientX, y = t.clientY;
    holdTimer = setTimeout(function () {
      holdTimer = null;
      li.dataset.held = '1';
      openMenu(li, x, y);
    }, 450);
  }, { passive: true });
  ['touchend', 'touchmove', 'touchcancel'].forEach(function (ev) {
    document.addEventListener(ev, function () { if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; } }, { passive: true });
  });
  /* The press that opened the menu must not also open the room. */
  document.addEventListener('click', function (e) {
    var li = e.target.closest && e.target.closest('.gwr-room');
    if (li && li.dataset.held) { delete li.dataset.held; e.preventDefault(); e.stopPropagation(); }
  }, true);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { closeMenu(); closePops(); } });
  document.addEventListener('scroll', closeMenu, true);

  /* ---------------- 5. FILTER ---------------- */
  var KEY = 'gt-wall-filter';
  function applyFilter(which) {
    var nav = document.querySelector('.gwr .gtm-nav'); if (!nav) return;
    nav.classList.toggle('is-favs', which === 'fav');
    nav.querySelectorAll('[data-gwr-filter]').forEach(function (b) {
      var on = b.getAttribute('data-gwr-filter') === which;
      b.classList.toggle('on', on); b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    var none = nav.querySelector('[data-gwr-nofav]');
    if (none) none.hidden = !(which === 'fav' && !nav.querySelector('.gwr-room.is-fav'));
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-gwr-filter]');
    if (!b) return;
    var which = b.getAttribute('data-gwr-filter');
    try { localStorage.setItem(KEY, which); } catch (err) { /* private mode */ }
    applyFilter(which);
  });
  function restoreFilter() {
    var which = 'all';
    try { which = localStorage.getItem(KEY) || 'all'; } catch (err) { /* ignore */ }
    applyFilter(which);
  }
  restoreFilter();
  document.body.addEventListener('htmx:afterSettle', restoreFilter);

  /* A reply's quote jumps to what it answers, and lights it up for a moment. */
  document.addEventListener('click', function (e) {
    var q = e.target.closest && e.target.closest('[data-wl-jump]');
    if (!q) return;
    var t = document.getElementById(q.getAttribute('data-wl-jump'));
    if (!t) return;                      /* older than this page: the link loads it */
    e.preventDefault();
    t.scrollIntoView({ behavior: 'smooth', block: 'center' });
    t.classList.remove('wl-lit'); void t.offsetWidth; t.classList.add('wl-lit');
  });

  /* A long thread shows its first three replies; the rest on request. */
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-wl-morereplies]');
    if (!b) return;
    b.closest('.wl-thread').classList.add('is-open');
    b.remove();
  });

  /* "Show all 10 games" on a recap. */
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-grc-more]');
    if (!b) return;
    b.closest('.grc-results').classList.add('is-open');
    b.remove();
  });

  /* ---------------- 7. MATCH BY MATCH ---------------- */
  /* The recap's games, swiped or stepped through, with a dot for where you are. */
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-grc-step]');
    if (b) {
      var track = b.closest('[data-grc-carousel]').querySelector('[data-grc-track]');
      var card = track.querySelector('.grc-card');
      track.scrollBy({ left: (card ? card.offsetWidth + 12 : 300) * parseInt(b.getAttribute('data-grc-step'), 10), behavior: 'smooth' });
      return;
    }
    var t = e.target.closest && e.target.closest('[data-grc-table]');
    if (t) { t.closest('.grc-board').classList.add('is-open'); t.remove(); return; }
    var more = e.target.closest && e.target.closest('[data-wl-more]');
    if (more) {
      var p = more.previousElementSibling;
      var open = p.classList.toggle('is-open');
      more.textContent = open ? 'Show less' : 'Read more';
    }
  });
  function dots(track) {
    var box = track.closest('[data-grc-carousel]');
    var all = box.querySelectorAll('.grc-dots i');
    var card = track.firstElementChild; if (!card || !all.length) return;
    var gap = parseFloat(getComputedStyle(track).columnGap) || 0;
    var i = Math.round(track.scrollLeft / (card.offsetWidth + gap));
    all.forEach(function (d, n) { d.classList.toggle('on', n === i); });
  }
  document.addEventListener('scroll', function (e) {
    if (e.target.matches && e.target.matches('[data-grc-track]')) dots(e.target);
  }, true);

  /* Long posts: a sensible height, and "Read more" when there is more. */
  function clampAll() {
    document.querySelectorAll('[data-wl-clamp]:not([data-clamped])').forEach(function (p) {
      p.setAttribute('data-clamped', '');
      if (p.scrollHeight > p.clientHeight + 4) {
        var b = document.createElement('button');
        b.type = 'button'; b.className = 'wl-more'; b.setAttribute('data-wl-more', ''); b.textContent = 'Read more';
        p.insertAdjacentElement('afterend', b);
      }
    });
    document.querySelectorAll('[data-grc-track]').forEach(dots);
    /* Final Whistle stories keep a standard height; "Read more" opens one. */
    document.querySelectorAll('[data-fw-fold]:not([data-folded])').forEach(function (f) {
      f.setAttribute('data-folded', '');
      if (f.scrollHeight > f.clientHeight + 8) {
        f.classList.add('has-more');
        var b = document.createElement('button');
        b.type = 'button'; b.className = 'fw-more'; b.textContent = 'Read more';
        b.addEventListener('click', function () {
          var open = f.classList.toggle('is-open');
          b.textContent = open ? 'Show less' : 'Read more';
        });
        f.insertAdjacentElement('afterend', b);
      }
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', clampAll); else clampAll();
  document.body.addEventListener('htmx:afterSettle', clampAll);

  /* ---------------- 6. REACTIONS ---------------- */
  /* Picking a reaction in a post's menu is the whole act: close the menu the
     way a press outside it would (gt-chat.js owns that menu). */
  document.body.addEventListener('htmx:afterRequest', function (e) {
    if (e.detail.elt && e.detail.elt.matches && e.detail.elt.matches('[data-wl-react]')) {
      document.body.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    }
  });
})();
