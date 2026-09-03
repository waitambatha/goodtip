/* GoodTip messages — the three-panel screen.
 *
 * Everything on this page that is not a link, a form or an htmx attribute.
 * Five jobs, and they are separate on purpose:
 *
 *   1. FILTER      the conversation list, by tab and by typed text
 *   2. LEVELS      the phone flow: list -> room -> conversation, and back
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
     1. FILTERING THE CONVERSATION LIST
     ======================================================================
     Both filters are applied by one function rather than each hiding rows on
     its own — with two independent hiders, typing a name and then pressing a
     tab shows rows the search had already excluded. */

  var tabs = Array.prototype.slice.call(root.querySelectorAll('[data-gtm-tab]'));
  var box = root.querySelector('[data-gtm-filter]');
  var list = root.querySelector('[data-gtm-list]');
  var nomatch = root.querySelector('[data-gtm-nomatch]');
  var tree = root.querySelector('[data-gtm-sec="tree"]');
  var face = 'all';

  function applyFilter() {
    if (!list) return;
    var term = (box && box.value || '').trim().toLowerCase();
    var rows = list.querySelectorAll('.gtm-conv');
    var shown = 0;
    Array.prototype.forEach.call(rows, function (row) {
      var okFace = face === 'all' || row.getAttribute('data-face') === face;
      var okTerm = !term || (row.getAttribute('data-find') || '').indexOf(term) !== -1;
      var on = okFace && okTerm;
      row.hidden = !on;
      if (on) shown++;
    });
    /* "Nothing here matches that" only makes sense when there was something
       to match. With no conversations at all the list already carries its own
       empty state, and showing both reads as two different problems. */
    if (nomatch) nomatch.hidden = shown !== 0 || rows.length === 0;
    /* The organisation tree is "where can I write", which is a different
       question from "what have I been writing". Searching or narrowing to one
       kind is asking the second question, so the tree gets out of the way. */
    if (tree) tree.hidden = !!term || face !== 'all';
  }

  tabs.forEach(function (tab) {
    tab.addEventListener('click', function () {
      face = tab.getAttribute('data-gtm-tab');
      tabs.forEach(function (t) {
        var on = t === tab;
        t.classList.toggle('on', on);
        t.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      applyFilter();
    });
  });
  if (box) {
    box.addEventListener('input', applyFilter);
    /* Escape clears rather than blurs. In a search box that is filtering
       what is on screen, "get me back to everything" is the thing you want,
       and losing focus does not do it. */
    box.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && box.value) { box.value = ''; applyFilter(); e.stopPropagation(); }
    });
  }

  /* ======================================================================
     2. LEVELS — the phone flow
     ======================================================================
     One attribute on the wrapper says which of the three panels is on screen;
     the stylesheet does the rest, so there is no measuring here and nothing
     to recompute on resize. On a desktop all three are visible and the
     attribute is inert. */

  function setView(v) { root.setAttribute('data-view', v); }

  document.addEventListener('click', function (e) {
    var back = e.target.closest && e.target.closest('[data-gtm-back]');
    if (back) { setView(back.getAttribute('data-gtm-back')); return; }
    /* The people button in the conversation header, which on a phone is the
       only way back to the member list without leaving the conversation. */
    if (e.target.closest && e.target.closest('[data-gtm-showroom]')) { setView('room'); return; }
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

  /* Emoji go in at the CARET. Appending to the end puts a face in the middle
     of a sentence somebody was still editing, which is the kind of thing that
     makes people stop using a picker. */
  document.addEventListener('click', function (e) {
    var menu = e.target.closest && e.target.closest('.cc-emoji-menu');
    if (!menu || e.target.tagName !== 'BUTTON') return;
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
