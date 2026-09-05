/* GoodTip messages — the three-panel screen.
 *
 * Everything on this page that is not a link, a form or an htmx attribute.
 * Five jobs, and they are separate on purpose:
 *
 *   1. LISTS       the four sidebar lists, the tabs that switch them, and the
 *                  one search box over the top
 *   2. LEVELS      the phone flow: list -> conversation, and back
 *   3. SCROLL      keep an open conversation pinned to its newest message,
 *                  including across the twelve-second poll
 *   4. COMPOSE     emoji at the caret, mic-or-send, Enter to send
 *   5. RECORD      handed to gt-voice.js, which owns the microphone
 *
 * Delegated from the document wherever the element can be swapped. The
 * message list replaces itself every twelve seconds (hx-swap="outerHTML" on
 * #gtmChatList), so nothing inside it may be bound once at load. The composer
 * deliberately sits OUTSIDE that swap — see _room_stream.html for why — but is
 * delegated too, because a room opened from the sidebar arrives with a fresh
 * one and per-element binding would quietly cover only the first.
 */
(function () {
  'use strict';

  var root = document.querySelector('[data-gtm]');
  if (!root) return;

  /* ======================================================================
     1. THE FOUR LISTS, AND THE ONE SEARCH BOX OVER THEM
     ======================================================================
     The tabs used to filter one list. They now SWITCH BETWEEN FOUR — recent
     chats, your organisations, your groups, and everybody you could write to —
     because an organisation you have never messaged has no conversation to
     filter and was therefore in none of them.

     The search box still sits above all four and belongs to whichever one is
     showing. For three of them that is a filter over rows already on the page:
     they are lists of things you belong to, so they are short by definition and
     a round trip to hide a few rows would be slower than the keystroke that
     asked for it.

     PEOPLE IS THE EXCEPTION and asks the server, because it is every member of
     every organisation you are in — the client's own worst case is "an
     organisation that has about 1000 people", and forty of them are on the page
     at a time. Debounced, or a name typed at speed fires eight searches whose
     answers can arrive in any order. */

  var tabs = Array.prototype.slice.call(root.querySelectorAll('[data-gtm-tab]'));
  var panes = Array.prototype.slice.call(root.querySelectorAll('[data-gtm-list-pane]'));
  var box = root.querySelector('[data-gtm-filter]');
  var nomatch = root.querySelector('[data-gtm-nomatch]');
  var contacts = document.getElementById('gtmContacts');
  var face = 'all';
  var contactsUrl = contacts && contacts.getAttribute('data-url');
  var typing = null;

  function currentPane() {
    for (var i = 0; i < panes.length; i++) {
      if (panes[i].getAttribute('data-gtm-list-pane') === face) return panes[i];
    }
    return null;
  }

  /* Every row in every pane carries data-find, so one function filters all
     four and there is no per-pane knowledge here to keep in step. */
  function applyFilter() {
    var term = (box && box.value || '').trim().toLowerCase();
    var pane = currentPane();
    if (!pane) return;
    var rows = pane.querySelectorAll('[data-find]');
    var shown = 0;
    Array.prototype.forEach.call(rows, function (row) {
      var on = !term || (row.getAttribute('data-find') || '').indexOf(term) !== -1;
      /* The conversation rows are <li> wrappers round a link; the directory
         rows are the link itself. Hiding whichever one carries data-find is
         right in both cases. */
      row.hidden = !on;
      if (on) shown++;
    });
    /* "Nothing here matches that" only makes sense when there was something to
       match. An empty list already carries its own empty state, and showing
       both reads as two different problems. */
    if (nomatch) nomatch.hidden = shown !== 0 || rows.length === 0 || !term;
  }

  /* The People tab's rows come from the server, so its search does too. The
     term goes with the request rather than filtering what happens to be
     loaded — otherwise searching a thousand-member organisation would only
     ever look at the first forty. */
  function searchContacts() {
    if (!contacts || !contactsUrl || !window.htmx) return;
    var term = (box && box.value || '').trim();
    window.htmx.ajax('GET', contactsUrl + (term ? '?q=' + encodeURIComponent(term) : ''), {
      target: '#gtmContacts', swap: 'innerHTML',
    });
  }

  function onSearchInput() {
    applyFilter();
    if (face !== 'person') return;
    clearTimeout(typing);
    typing = setTimeout(searchContacts, 280);
  }

  function showPane(next) {
    face = next;
    panes.forEach(function (p) {
      p.hidden = p.getAttribute('data-gtm-list-pane') !== face;
    });
    tabs.forEach(function (t) {
      var on = t.getAttribute('data-gtm-tab') === face;
      t.classList.toggle('on', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    applyFilter();
    /* Coming back to People with something still typed: the rows on screen are
       the answer to a different question until this asks again. */
    if (face === 'person') searchContacts();
  }

  tabs.forEach(function (tab) {
    tab.addEventListener('click', function () { showPane(tab.getAttribute('data-gtm-tab')); });
  });
  if (box) {
    box.addEventListener('input', onSearchInput);
    /* Escape clears rather than blurs. In a search box that is filtering what
       is on screen, "get me back to everything" is the thing you want, and
       losing focus does not do it. */
    box.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && box.value) { box.value = ''; onSearchInput(); e.stopPropagation(); }
    });
  }

  /* ======================================================================
     2. LEVELS — the phone flow
     ======================================================================
     One attribute on the wrapper says which of the two panels is on screen; the
     stylesheet does the rest, so there is no measuring here and nothing to
     recompute on resize. On a desktop both are visible and the attribute is
     inert. The details panel is not a third level — it covers the conversation
     wherever it is opened, and its own close button is the way out. */

  function setView(v) { root.setAttribute('data-view', v); }

  document.addEventListener('click', function (e) {
    var back = e.target.closest && e.target.closest('[data-gtm-back]');
    if (back) { setView(back.getAttribute('data-gtm-back')); return; }
  });

  /* ======================================================================
     3. SCROLL
     ======================================================================
     A conversation opens at its newest message, and stays there as new ones
     arrive — UNLESS the reader has scrolled up, in which case being yanked
     back to the bottom every twelve seconds would make reading history
     impossible. "Near the bottom" is a threshold rather than an exact match
     because a half-pixel of sub-pixel rounding must not count as "they have
     scrolled away". */

  var STICK = 120;

  function pane() { return root.querySelector('[data-gtm-scroll]'); }
  function atBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight < STICK;
  }
  function toBottom(el, smooth) {
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
  }
  toBottom(pane(), false);

  /* ONLY THE POLL RE-PINS THE SCROLL.
     Both the twelve-second refresh and a reaction swap fire htmx events from
     inside the conversation, and they want opposite things: the refresh
     should follow new messages down, a reaction must not move the page at
     all. Testing "is this inside the chat pane" catches both — so the test is
     "is this the scrolling list itself", which only the poll's swap is. */
  function isStreamSwap(e) {
    var t = e.target;
    if (!t || t.nodeType !== 1) return false;
    return (t.matches && t.matches('[data-gtm-scroll]')) ||
           (t.querySelector && !!t.querySelector('[data-gtm-scroll]'));
  }
  /* Measured BEFORE the swap and restored after, because the element the
     measurement came from no longer exists by then. */
  var wasAtBottom = true;
  document.body.addEventListener('htmx:beforeSwap', function (e) {
    if (!isStreamSwap(e)) return;
    var el = pane();
    wasAtBottom = !el || atBottom(el);
  });
  document.body.addEventListener('htmx:afterSettle', function (e) {
    if (!isStreamSwap(e)) return;
    var el = pane();
    if (el && wasAtBottom) toBottom(el, false);
  });

  /* The pinned-message card and the reply quotes both link to a bubble by id.
     A plain anchor works, but inside a scrolling pane it jumps the whole page
     as well; this scrolls the pane and marks the bubble so the eye can find
     it, which is the point of following the link. */
  document.addEventListener('click', function (e) {
    var jump = e.target.closest && e.target.closest('[data-jump]');
    if (!jump) return;
    var row = document.getElementById('msg-' + jump.getAttribute('data-jump'));
    if (!row) return;
    e.preventDefault();
    row.scrollIntoView({ block: 'center', behavior: 'smooth' });
    row.classList.add('is-found');
    setTimeout(function () { row.classList.remove('is-found'); }, 1600);
  });

  /* ======================================================================
     4. THE COMPOSER
     ====================================================================== */

  function bodyOf(form) { return form && form.querySelector('[data-chat-body]'); }

  /* Mic when there is nothing to send, Send when there is. Both are in the
     markup; this decides which one is on screen. Rendered the other way round
     (Send visible, mic hidden) so that with no JavaScript the button that
     works is the one you see. */
  function refreshSendState(form) {
    var mic = form.querySelector('[data-rec-start]');
    var send = form.querySelector('.cc-send');
    var text = bodyOf(form);
    var files = form.querySelector('[data-files]');
    if (!mic || !send) return;
    var hasSomething =
      (text && text.value.trim().length > 0) ||
      (files && files.files && files.files.length > 0);
    /* No microphone in this browser (or no permission API at all) means the
       mic button can never do anything, so Send stays whatever the state. */
    var canRecord = !!(navigator.mediaDevices && window.MediaRecorder);
    mic.hidden = hasSomething || !canRecord;
    send.hidden = !hasSomething && canRecord;
    var hint = form.querySelector('[data-mic-hint]');
    if (hint) hint.hidden = !canRecord;
  }

  /* Grows with what is typed, up to a ceiling, then scrolls. A textarea that
     grows without limit eats the conversation it belongs to. */
  function autosize(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 160) + 'px';
  }

  document.addEventListener('input', function (e) {
    var form = e.target.closest && e.target.closest('[data-gtm-composer]');
    if (!form) return;
    if (e.target.matches('[data-chat-body]')) autosize(e.target);
    refreshSendState(form);
  });
  document.addEventListener('change', function (e) {
    var form = e.target.closest && e.target.closest('[data-gtm-composer]');
    if (form) refreshSendState(form);
  });

  /* Enter sends, Shift+Enter is a new line. Not bound on the textarea at load:
     the composer is inside the pane the poll replaces. */
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' || e.shiftKey) return;
    var box = e.target.closest && e.target.closest('[data-chat-body]');
    if (!box) return;
    var form = box.closest('[data-gtm-composer]');
    if (!form) return;
    /* Nothing typed and nothing attached: Enter is not a way to post an empty
       message, and the server would reject it anyway. */
    if (!box.value.trim() && !(form.querySelector('[data-files]') || {}).files) return;
    e.preventDefault();
    if (typeof form.requestSubmit === 'function') form.requestSubmit();
    else form.submit();
  });

  /* THE CATEGORY TABS. Eight categories, one grid on screen at a time — the
     sort the client asked for ("one for cars, the other for sports, more for
     signs, cups, flags"). Handled before the insert below, because a tab is
     also a BUTTON inside .cc-emoji-menu and would otherwise type its own glyph
     into the message. */
  document.addEventListener('click', function (e) {
    var tab = e.target.closest && e.target.closest('[data-emoji-tab]');
    if (!tab) return;
    e.preventDefault();
    e.stopPropagation();
    var menu = tab.closest('.cc-emoji-menu');
    if (!menu) return;
    var want = tab.getAttribute('data-emoji-tab');
    menu.querySelectorAll('[data-emoji-tab]').forEach(function (t) {
      t.classList.toggle('on', t === tab);
    });
    menu.querySelectorAll('[data-emoji-panel]').forEach(function (p) {
      p.hidden = p.getAttribute('data-emoji-panel') !== want;
    });
  }, true);

  /* Emoji go in at the CARET. Appending to the end puts a face in the middle
     of a sentence somebody was still editing, which is the kind of thing that
     makes people stop using a picker. */
  document.addEventListener('click', function (e) {
    var menu = e.target.closest && e.target.closest('.cc-emoji-menu');
    if (!menu || e.target.tagName !== 'BUTTON') return;
    if (e.target.hasAttribute('data-emoji-tab')) return;
    var form = menu.closest('[data-gtm-composer]');
    var box = bodyOf(form);
    if (!box) return;
    var at = box.selectionStart == null ? box.value.length : box.selectionStart;
    var to = box.selectionEnd == null ? at : box.selectionEnd;
    var ch = e.target.textContent;
    box.value = box.value.slice(0, at) + ch + box.value.slice(to);
    box.selectionStart = box.selectionEnd = at + ch.length;
    box.focus();
    autosize(box);
    refreshSendState(form);
    var picker = menu.closest('details');
    if (picker) picker.open = false;
  });

  /* Any open <details> popover — emoji, the reaction picker — closes on a
     press elsewhere and on Escape. Left alone they stay open behind whatever
     you do next. */
  document.addEventListener('click', function (e) {
    document.querySelectorAll('.cc-emoji[open], .rx-add[open]').forEach(function (d) {
      if (!d.contains(e.target)) d.open = false;
    });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    document.querySelectorAll('.cc-emoji[open], .rx-add[open]').forEach(function (d) { d.open = false; });
  });

  /* ======================================================================
     5. THE "NEW MESSAGE" SHEET
     ====================================================================== */

  var sheet = document.getElementById('gtmNew');
  if (sheet) {
    var lastFocus = null;
    var openSheet = function () {
      lastFocus = document.activeElement;
      sheet.hidden = false;
      sheet.setAttribute('aria-hidden', 'false');
      document.body.classList.add('sheet-open');
      var first = sheet.querySelector('a');
      if (first) first.focus();
    };
    var closeSheet = function () {
      sheet.hidden = true;
      sheet.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('sheet-open');
      if (lastFocus) lastFocus.focus();
    };
    document.addEventListener('click', function (e) {
      if (e.target.closest && e.target.closest('[data-gtm-new]')) { openSheet(); return; }
      if (e.target.closest && e.target.closest('[data-gtm-new-close]')) { closeSheet(); }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !sheet.hidden) closeSheet();
    });
  }

  /* First paint: put the composer into whichever state it should be in, and
     do it again after every swap that could have replaced it. */
  function initComposers() {
    document.querySelectorAll('[data-gtm-composer]').forEach(function (form) {
      var box = bodyOf(form);
      if (box) autosize(box);
      refreshSendState(form);
    });
  }
  initComposers();
  document.body.addEventListener('htmx:afterSettle', initComposers);
})();

/* ---------------------------------------------------------------------------
 * OPENING A ROOM WITHOUT RELOADING THE SCREEN AROUND IT
 *
 * ASKED FOR: "if I pick a group or an organisation the whole page should not be
 * loading — it should only load on that centre part where we have the data."
 *
 * The links carry the htmx attributes; this is the part htmx cannot know about:
 * which row should look selected, which pane a phone should be showing, and the
 * fact that the middle is the only thing that may show a spinner.
 * ------------------------------------------------------------------------- */
(function () {
  'use strict';

  var shell = document.querySelector('[data-gtm]');
  if (!shell) return;
  var chat = document.querySelector('.gtm-chat');

  function markSelected(link) {
    /* The row you pressed, and only that one. Read off the anchor rather than
       from the response, because the response is the conversation and does not
       know which list row pointed at it. */
    document.querySelectorAll('.gtm-conv.on').forEach(function (el) {
      el.classList.remove('on');
    });
    var row = link.closest('.gtm-conv');
    if (row) row.classList.add('on');
    document.querySelectorAll('.gtm-dir.on').forEach(function (el) {
      el.classList.remove('on');
    });
    if (link.classList.contains('gtm-dir')) link.classList.add('on');
  }

  document.addEventListener('click', function (e) {
    var link = e.target.closest('[data-gtm-open]');
    if (!link) return;
    markSelected(link);
    /* On a phone the three panels are three screens; opening a conversation is
       a move to the third. Set before the request so the transition starts with
       the press rather than when the bytes land. */
    shell.setAttribute('data-view', 'chat');
  });

  /* The spinner belongs to the panel being replaced. htmx puts .htmx-request on
     the element that made the request — a list row — so without this the list
     would flicker and the pane it is fetching would sit still. */
  document.body.addEventListener('htmx:beforeRequest', function (e) {
    if (!chat) return;
    var t = e.detail && e.detail.elt;
    if (t && t.closest && t.closest('[data-gtm-open]')) chat.classList.add('is-loading');
  });
  document.body.addEventListener('htmx:afterSwap', function () {
    if (chat) chat.classList.remove('is-loading');
  });
  document.body.addEventListener('htmx:responseError', function () {
    if (chat) chat.classList.remove('is-loading');
  });
})();

/* ---------------------------------------------------------------------------
 * THE DETAILS PANEL
 *
 * "Make it clickable, even the name of the group. At the end add the three dots
 * as well, all for the details ... and when I click on it, and it's three dots
 * at the top — that is in the 2/3 where we have the chats — I get that person's
 * details, like groups in common and name and a profile pic."
 *
 * The panel is already in the page, rendered with the conversation it describes
 * (_room_details.html). This is only what opens and closes it, which is why the
 * whole thing still says the right things with scripting off — it is simply
 * always visible then, at the end of the conversation's own column.
 *
 * Two ways in and they are the same panel: the name block in the header, where
 * anybody who has used a messaging app will press, and the ⋮ beside it, where
 * anybody who has not will look.
 * ------------------------------------------------------------------------- */
(function () {
  'use strict';

  function panel() { return document.querySelector('[data-gtm-details-panel]'); }

  function setOpen(on) {
    var el = panel();
    if (!el) return;
    el.hidden = !on;
    document.querySelectorAll('[data-gtm-details]').forEach(function (b) {
      b.setAttribute('aria-expanded', on ? 'true' : 'false');
    });
    if (on) {
      var close = el.querySelector('[data-gtm-details-close]');
      if (close) close.focus();
    }
  }

  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-gtm-details-close]')) { setOpen(false); return; }
    if (e.target.closest('[data-gtm-details]')) {
      var el = panel();
      setOpen(!el || el.hidden);
    }
  });

  document.addEventListener('keydown', function (e) {
    var el = panel();
    if (e.key === 'Escape' && el && !el.hidden) setOpen(false);
  });

  /* Opening another room replaces the panel along with the conversation, and
     the replacement arrives closed. That is deliberate: the details of the room
     you just left are not the details of the one you just opened, and leaving
     it open would show the new room's members over a conversation somebody
     pressed in order to read. */
})();

/* ---------------------------------------------------------------------------
 * SENDING: CLEAR THE COMPOSER, AND SHOW THE UPLOAD WHERE THE UPLOAD IS
 *
 * "When you type and hit send we should not have a loader. The audio and image
 * maybe — but in the image and audio itself, the way you see it on WhatsApp:
 * you upload the image and you see the circle like a status bar, then it goes
 * round and completes when full and ready. This should be at the bottom, not
 * where I am seeing it."
 *
 * So: text sends with nothing at all. A send carrying a file gets a ring in the
 * composer — at the bottom, beside the send button, where the file was chosen —
 * and it reports real bytes rather than animating a guess.
 * ------------------------------------------------------------------------- */
(function () {
  'use strict';

  var RING = 2 * Math.PI * 13;   /* r=13 in the SVG below */

  function ringFor(form) {
    var ring = form.querySelector('[data-cc-progress]');
    if (ring) return ring;
    ring = document.createElement('span');
    ring.className = 'cc-progress';
    ring.setAttribute('data-cc-progress', '');
    ring.hidden = true;
    ring.innerHTML =
      '<svg viewBox="0 0 32 32" aria-hidden="true">' +
      '<circle class="ccp-track" cx="16" cy="16" r="13"></circle>' +
      '<circle class="ccp-fill" cx="16" cy="16" r="13"' +
      ' stroke-dasharray="' + RING + '" stroke-dashoffset="' + RING + '"></circle>' +
      '</svg><b data-cc-pct>0%</b>';
    /* Beside the send button, which is the bottom-right of the composer — the
       place the file was attached from and the place the eye already is. */
    var row = form.querySelector('.cc-row') || form;
    row.appendChild(ring);
    return ring;
  }

  function hasFiles(form) {
    return Array.prototype.some.call(
      form.querySelectorAll('input[type="file"]'),
      function (i) { return i.files && i.files.length; }
    );
  }

  document.body.addEventListener('htmx:xhr:progress', function (e) {
    var form = e.detail && e.detail.elt;
    if (!form || !form.matches || !form.matches('[data-gtm-composer]')) return;
    if (!e.detail.lengthComputable || !hasFiles(form)) return;
    var ring = ringFor(form);
    ring.hidden = false;
    var pct = Math.min(100, Math.round(e.detail.loaded / e.detail.total * 100));
    var fill = ring.querySelector('.ccp-fill');
    if (fill) fill.setAttribute('stroke-dashoffset', String(RING * (1 - pct / 100)));
    var label = ring.querySelector('[data-cc-pct]');
    if (label) label.textContent = pct + '%';
  });

  document.body.addEventListener('htmx:afterRequest', function (e) {
    var form = e.detail && e.detail.elt;
    if (!form || !form.matches || !form.matches('[data-gtm-composer]')) return;
    var ring = form.querySelector('[data-cc-progress]');
    if (ring) ring.hidden = true;
    if (!e.detail.successful) return;

    /* Clear what was sent, and only that. reset() would also clear the reply
       banner's hidden field while leaving the banner drawn, so the next message
       would quietly quote something the composer no longer shows. */
    var box = form.querySelector('textarea');
    if (box) { box.value = ''; box.style.height = ''; }
    form.querySelectorAll('input[type="file"]').forEach(function (i) { i.value = ''; });
    var tray = form.querySelector('[data-file-tray]');
    if (tray) { tray.innerHTML = ''; tray.hidden = true; }
    var banner = form.querySelector('[data-reply-banner]');
    if (banner) banner.hidden = true;
    var replyId = form.querySelector('[data-reply-id]');
    if (replyId) replyId.value = '';
    var ready = form.querySelector('[data-rec-ready]');
    if (ready) ready.hidden = true;
  });
})();

/* ---------------------------------------------------------------------------
 * FOLDING THE CONVERSATION LIST
 *
 * "This column can be made closable and openable."
 *
 * Remembered per browser, because whether you want the list beside you is a
 * working preference and not a per-page decision — re-opening it on every
 * conversation would be its own annoyance.
 * ------------------------------------------------------------------------- */
(function () {
  'use strict';

  var shell = document.querySelector('[data-gtm]');
  var btn = document.querySelector('[data-gtm-fold]');
  if (!shell || !btn) return;
  var KEY = 'gt-messages-folded';

  function apply(folded) {
    shell.classList.toggle('is-folded', folded);
    btn.setAttribute('aria-expanded', folded ? 'false' : 'true');
    btn.setAttribute('aria-label', folded ? 'Show the conversation list' : 'Hide the conversation list');
  }

  /* Wrapped: a browser with site data blocked throws on read, and a chat client
     that will not open because it could not remember a panel width is a poor
     trade. */
  var saved = false;
  try { saved = localStorage.getItem(KEY) === '1'; } catch (e) { saved = false; }
  apply(saved);

  btn.addEventListener('click', function () {
    var folded = !shell.classList.contains('is-folded');
    apply(folded);
    try { localStorage.setItem(KEY, folded ? '1' : '0'); } catch (e) { /* not fatal */ }
  });
})();

/* ---------------------------------------------------------------------------
 * THE RIGHT-CLICK MENU ON A CONVERSATION
 *
 * "Just as WhatsApp, I should be able to right click and have options like
 * archive chat, mute notifications, pin chat, add favourite, clear chat, delete
 * chat, block a user."
 *
 * The menu is already in the page as real forms — see messages.html. This only
 * decides where it appears and when it goes away, which means the whole feature
 * still works with scripting off (the ⋮ button reveals the same markup) and
 * from the keyboard.
 * ------------------------------------------------------------------------- */
(function () {
  'use strict';

  var open = null;

  function close() {
    if (!open) return;
    open.hidden = true;
    open.style.removeProperty('left');
    open.style.removeProperty('top');
    open = null;
  }

  function show(menu, x, y) {
    close();
    menu.hidden = false;
    open = menu;
    if (x == null) return;
    /* Positioned in viewport coordinates because the menu is fixed — a menu
       placed relative to a row inside a scrolling list drifts away from the
       pointer the moment anything scrolls. Clamped so it cannot open with half
       of itself past the bottom or right edge, which on the last row of a full
       list is otherwise where it always opens. */
    var box = menu.getBoundingClientRect();
    var left = Math.min(x, window.innerWidth - box.width - 8);
    var top = Math.min(y, window.innerHeight - box.height - 8);
    menu.style.left = Math.max(8, left) + 'px';
    menu.style.top = Math.max(8, top) + 'px';
  }

  document.addEventListener('contextmenu', function (e) {
    var row = e.target.closest('[data-chat-row]');
    if (!row) return;
    var menu = row.querySelector('[data-conv-menu]');
    if (!menu) return;
    /* Only inside the conversation list: the browser's own menu is the right
       one everywhere else, including on the message text somebody wants to
       copy. */
    e.preventDefault();
    show(menu, e.clientX, e.clientY);
  });

  /* ---- HOLDING A ROW DOWN ------------------------------------------------
     "The right click and hard press or holding it should now give me the option
     to archive chat, mute notification, pin chat..."

     A phone has no right click, so the hold is the gesture — the same one every
     messaging app uses, and the reason the ⋮ is not the only way in on touch.

     500ms, which is roughly where a browser's own long-press callout fires; and
     a press that MOVES is a scroll, not a hold, so any movement past a few
     pixels cancels it. Without that check every flick down the list would open
     a menu under the thumb. */
  var HOLD = 500, MOVE = 10;
  var timer = null, held = null, from = null;

  function cancelHold() {
    clearTimeout(timer);
    timer = null;
    if (held) held.classList.remove('is-pressing');
    held = null;
    from = null;
  }

  document.addEventListener('touchstart', function (e) {
    var row = e.target.closest && e.target.closest('[data-chat-row]');
    if (!row || !row.querySelector('[data-conv-menu]')) return;
    var t = e.touches[0];
    held = row;
    from = { x: t.clientX, y: t.clientY };
    row.classList.add('is-pressing');
    timer = setTimeout(function () {
      var menu = row.querySelector('[data-conv-menu]');
      row.classList.remove('is-pressing');
      /* Centred on the row rather than on the finger, which is under it. */
      var at = row.getBoundingClientRect();
      show(menu, at.left + 12, at.bottom - 8);
      /* The press has become a menu, so the tap it would otherwise have been
         must not also open the conversation behind it. */
      suppressNextTap = true;
    }, HOLD);
  }, { passive: true });

  document.addEventListener('touchmove', function (e) {
    if (!from) return;
    var t = e.touches[0];
    if (Math.abs(t.clientX - from.x) > MOVE || Math.abs(t.clientY - from.y) > MOVE) cancelHold();
  }, { passive: true });

  document.addEventListener('touchend', cancelHold);
  document.addEventListener('touchcancel', cancelHold);

  /* Set by the hold above and read by the capture listener below, which runs
     before the link's own handler and before gt-messages' open-a-room one. */
  var suppressNextTap = false;
  document.addEventListener('click', function (e) {
    if (!suppressNextTap) return;
    suppressNextTap = false;
    if (!e.target.closest('[data-chat-row]') || e.target.closest('[data-conv-menu]')) return;
    e.preventDefault();
    e.stopPropagation();
  }, true);

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-conv-more]');
    if (btn) {
      e.preventDefault();
      e.stopPropagation();
      var menu = btn.parentElement.querySelector('[data-conv-menu]');
      if (menu === open) { close(); return; }
      var at = btn.getBoundingClientRect();
      show(menu, at.left - 150, at.bottom + 4);
      return;
    }
    /* A press inside the menu is a submit; anything else closes it. */
    if (!e.target.closest('[data-conv-menu]')) close();
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') close();
  });
  /* Fixed positioning and a scrolling list: without this the menu stays put
     while the row it belongs to slides away underneath it. */
  document.addEventListener('scroll', close, true);
  window.addEventListener('resize', close);
})();
