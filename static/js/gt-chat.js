/* GoodTip chat — reply-to-a-message, attachments, and the profile card.
 *
 * Drives both ends of a message thread and the Wall's reply threads, because
 * the client asked for one behaviour in both places and two implementations of
 * "swipe to reply" is two things to keep in step.
 *
 * Everything is delegated from the document. Nothing here is bound per bubble
 * at load, so a thread that arrives from a page load, a form post or (later) a
 * partial swap all behave the same.
 *
 * ---------------------------------------------------------------------------
 * WHY SWIPE *AND* A BUTTON
 * ---------------------------------------------------------------------------
 * A swipe is invisible. If it were the only way in, the feature would exist
 * for the people who already guessed it was there — which is nobody using this
 * for the first time. The arrow is the discoverable control and the swipe is
 * the fast one, and because they call the same function they cannot disagree
 * about what replying means.
 *
 * The swipe is deliberately conservative: it only starts once the finger has
 * moved further horizontally than vertically, so a normal scroll through a
 * long thread never drags a bubble sideways. Below the threshold it springs
 * back and nothing happens.
 */
(function () {
  'use strict';

  var TRIGGER = 56;      /* px of travel that counts as "reply to this" */
  var MAX_DRAG = 92;     /* the bubble stops moving past here */

  /* ---- Picking a message to reply to ------------------------------------ */

  function composerFor(el) {
    /* The nearest form that is a chat composer. Nearest rather than "the
       one on the page" because the Wall has one per post, and a reply picked
       under post 12 must fill in post 12's box and not the first on screen. */
    var scope = el.closest('[data-chat-scope]') || document;
    return scope.querySelector('[data-chat-form]');
  }

  function setReply(row) {
    var form = composerFor(row);
    if (!form) return;
    var banner = form.querySelector('[data-reply-banner]');
    var idField = form.querySelector('[data-reply-id]');
    if (!banner || !idField) return;

    idField.value = row.getAttribute('data-msg') || '';
    var who = banner.querySelector('[data-reply-who]');
    var quote = banner.querySelector('[data-reply-quote]');
    if (who) who.textContent = row.getAttribute('data-author') || '';
    if (quote) quote.textContent = row.getAttribute('data-quote') || '';
    banner.hidden = false;

    var box = form.querySelector('[data-chat-body]');
    if (box) box.focus();
  }

  function clearReply(form) {
    var banner = form.querySelector('[data-reply-banner]');
    var idField = form.querySelector('[data-reply-id]');
    if (banner) banner.hidden = true;
    if (idField) idField.value = '';
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-reply-to]');
    if (btn) {
      e.preventDefault();
      /* Reply now lives in the press menu, and a menu item that leaves its
         menu open has not finished. Declared later in this file, so it is
         reached through the window rather than by moving the handler. */
      if (typeof closeMsgMenu === 'function') closeMsgMenu();
      var row = btn.closest('[data-msg]');
      if (row) setReply(row);
      return;
    }

    var cancel = e.target.closest('[data-reply-cancel]');
    if (cancel) {
      e.preventDefault();
      clearReply(cancel.closest('form'));
      return;
    }

    /* Jumping to a quoted message. The browser would do the anchor on its
       own; what it would not do is say WHICH of nine near-identical bubbles
       you just landed on, so the target flashes. */
    var jump = e.target.closest('[data-jump]');
    if (jump) {
      /* Two id shapes, because the two places this runs number their rows
         differently: a message thread uses msg-<id>, a Wall thread reply-<id>.
         Looking for only the first meant every quote on the Wall fell through
         to the browser's own anchor handling and never flashed. */
      var at = jump.getAttribute('data-jump');
      var target = document.getElementById('msg-' + at) || document.getElementById('reply-' + at);
      if (target) {
        e.preventDefault();
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        /* `chat-lit`, not `flash`: `.flash` is already the flash-message
           component (a bordered, padded alert box), and putting it on a chat
           row would have wrapped the bubble in one. */
        target.classList.remove('chat-lit');
        /* Reading offsetWidth restarts the animation. Without it a second
           press on the same quote does nothing at all, because the class is
           already there and re-adding it is not a change. */
        void target.offsetWidth;
        target.classList.add('chat-lit');
      }
      return;
    }

    /* The profile card. One open at a time — two cards up at once in a
       conversation reads as a rendering fault rather than as two cards. */
    var peek = e.target.closest('[data-peek]');
    if (peek) {
      e.preventDefault();
      var col = peek.closest('.chat-col') || peek.parentElement;
      var card = col && col.querySelector('.chat-peek');
      var opening = card && card.hidden;
      document.querySelectorAll('.chat-peek').forEach(function (c) { c.hidden = true; });
      document.querySelectorAll('[data-peek]').forEach(function (b) {
        b.setAttribute('aria-expanded', 'false');
      });
      if (card && opening) {
        card.hidden = false;
        peek.setAttribute('aria-expanded', 'true');
      }
      return;
    }

    /* A press anywhere else closes whatever card is open. */
    if (!e.target.closest('.chat-peek')) {
      document.querySelectorAll('.chat-peek').forEach(function (c) { c.hidden = true; });
    }
  });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var open = document.querySelector('.chat-peek:not([hidden])');
    if (open) { open.hidden = true; return; }
    var banner = document.querySelector('[data-reply-banner]:not([hidden])');
    if (banner) clearReply(banner.closest('form'));
  });

  /* ---- Swipe to reply ---------------------------------------------------- */

  var drag = null;

  document.addEventListener('touchstart', function (e) {
    if (e.touches.length !== 1) return;
    var row = e.target.closest('[data-msg]');
    if (!row) return;
    /* Not while a link or a button is under the finger — a tap on an
       attachment should open it, and a drag begun on one would swallow the
       tap that was actually intended. */
    if (e.target.closest('a, button, input, textarea, select')) return;
    drag = {
      row: row,
      x: e.touches[0].clientX,
      y: e.touches[0].clientY,
      dx: 0,
      live: false,
      /* Which way the bubble travels. Your own messages sit on the right and
         swipe left; theirs sit on the left and swipe right. Dragging a bubble
         off its own side of the page and across the thread reads as moving
         it, not as answering it. */
      dir: row.classList.contains('mine') ? -1 : 1
    };
  }, { passive: true });

  document.addEventListener('touchmove', function (e) {
    if (!drag) return;
    var dx = e.touches[0].clientX - drag.x;
    var dy = e.touches[0].clientY - drag.y;

    if (!drag.live) {
      /* Undecided until the finger commits. Vertical wins ties, because the
         common gesture in a thread is a scroll and a scroll that occasionally
         drags a bubble sideways is worse than a swipe that occasionally needs
         a second go. */
      if (Math.abs(dy) >= Math.abs(dx) || Math.abs(dx) < 12) {
        if (Math.abs(dy) > 12) drag = null;   /* it was a scroll */
        return;
      }
      if (dx * drag.dir < 0) { drag = null; return; }   /* wrong way */
      drag.live = true;
      drag.row.classList.add('swiping');
    }

    drag.dx = Math.max(-MAX_DRAG, Math.min(MAX_DRAG, dx));
    drag.row.style.setProperty('--swipe', drag.dx + 'px');
    drag.row.classList.toggle('will-reply', Math.abs(drag.dx) >= TRIGGER);
  }, { passive: true });

  function endDrag() {
    if (!drag) return;
    var row = drag.row;
    var fired = drag.live && Math.abs(drag.dx) >= TRIGGER;
    row.classList.remove('swiping', 'will-reply');
    row.style.removeProperty('--swipe');
    drag = null;
    if (fired) {
      setReply(row);
      /* A short buzz, where the device does them. The bubble has already
         sprung back by the time the composer fills in, so without some
         acknowledgement a successful swipe and a failed one feel identical. */
      if (navigator.vibrate) { try { navigator.vibrate(10); } catch (err) {} }
    }
  }

  document.addEventListener('touchend', endDrag, { passive: true });
  document.addEventListener('touchcancel', endDrag, { passive: true });

  /* ---- Attachments ------------------------------------------------------- */

  /* The tray is built from the input's own FileList, so it cannot claim to be
     sending something the form will not send. Removing a chip rewrites that
     list through a DataTransfer, which is the only way to take one file out of
     an <input type=file> — assigning to input.files is otherwise refused. */
  function paintTray(input) {
    var form = input.closest('form');
    var tray = form && form.querySelector('[data-file-tray]');
    if (!tray) return;
    tray.innerHTML = '';
    var files = Array.prototype.slice.call(input.files || []);
    tray.hidden = !files.length;
    files.forEach(function (file, i) {
      var chip = document.createElement('span');
      chip.className = 'cc-chip';
      var name = document.createElement('b');
      name.textContent = file.name;
      var size = document.createElement('small');
      size.textContent = file.size < 1024 * 1024
        ? Math.round(file.size / 1024) + ' KB'
        : (file.size / (1024 * 1024)).toFixed(1) + ' MB';
      var x = document.createElement('button');
      x.type = 'button';
      x.className = 'cc-chip-x';
      x.setAttribute('aria-label', 'Remove ' + file.name);
      x.textContent = '×';
      x.addEventListener('click', function () {
        var dt = new DataTransfer();
        files.forEach(function (f, n) { if (n !== i) dt.items.add(f); });
        input.files = dt.files;
        paintTray(input);
      });
      chip.appendChild(name);
      chip.appendChild(size);
      chip.appendChild(x);
      tray.appendChild(chip);
    });
  }

  document.addEventListener('change', function (e) {
    var input = e.target.closest('[data-files]');
    if (input) paintTray(input);
  });

  /* ---- The box itself ---------------------------------------------------- */

  /* Grow to fit, up to a point. A textarea that grows without limit pushes the
     conversation it belongs to off the top of the screen. */
  function grow(box) {
    box.style.height = 'auto';
    box.style.height = Math.min(box.scrollHeight, 190) + 'px';
  }

  document.addEventListener('input', function (e) {
    var box = e.target.closest('[data-chat-body]');
    if (box) grow(box);
  });

  document.addEventListener('keydown', function (e) {
    var box = e.target.closest('[data-chat-body]');
    if (!box || e.key !== 'Enter' || e.shiftKey || e.isComposing) return;
    /* Enter sends. Shift+Enter is the newline — the arrangement every chat
       app uses, and the one people's fingers already have. */
    var form = box.closest('form');
    if (!form) return;
    e.preventDefault();
    /* Nothing to send is not a send. The box is not `required` (a file with
       no words is a real message), so without this an idle Enter would post
       an empty form and reload the page for nothing. */
    var files = form.querySelector('[data-files]');
    if (!box.value.trim() && !(files && files.files && files.files.length)) return;
    if (typeof form.requestSubmit === 'function') form.requestSubmit();
    else form.submit();
  });

  /* ---- Land at the bottom ------------------------------------------------ */

  /* A conversation is read newest-last, so arriving at the top of a long one
     means scrolling past everything you have already read to reach the thing
     you came for — and past the box you came to write in.
     
     Three things stop it firing, and each has cost somebody something:
       * a hash naming a message (#msg-123) is someone following a quote to a
         specific line, and jumping them to the bottom instead loses the line
         they asked for;
       * a page that barely scrolls is already showing the whole thread, and
         nudging it reads as the page twitching on load;
       * the Wall is a feed, read top down, and has no single conversation to
         be at the bottom of. */
  function toBottom() {
    if (location.hash.indexOf('#msg-') === 0) return;
    if (document.querySelector('[data-chat-feed], .gt-feed')) return;
    var chat = document.querySelector('.chat-list');
    if (!chat) return;
    var page = document.documentElement;
    if (page.scrollHeight < window.innerHeight * 1.4) return;
    var composer = document.querySelector('[data-chat-form]');
    if (composer) composer.scrollIntoView({ block: 'end' });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', toBottom);
  } else {
    toBottom();
  }

  /* ---- WHAT TO DO WITH A MESSAGE ----------------------------------------
   *
   * CLIENT, 20 SEP 2026: "on the edit and delete, it should not be buttons.
   * What it should be is when I click on it — or if it's touch I will press it
   * a little bit — and those should be there."
   *
   * THREE WAYS IN, ONE MENU. A press on the bubble, a hold on touch, or the ⋮
   * for the keyboard and for anybody who wants a target. They all open the
   * same markup, which is rendered server-side with real forms — so with
   * scripting off the menu is simply always open and every item still works.
   *
   * WHY PRESSING THE BUBBLE IS SAFE. A bubble is not a link and has nothing
   * else to do when pressed, so the gesture is free — and it is the one every
   * chat app people already use has trained them in. A press that lands on a
   * LINK inside the message, or on a selection somebody is making, is left
   * alone: reading a URL and copying a quote both have to keep working.
   */
  var msgMenu = null;
  var msgMenuHome = null;    // where it lives when it is not open
  var msgMenuRow = null;

  /* IT HAS TO BE MOVED TO THE BODY TO OPEN, and that is not a stylistic
   * choice. The menu is position:fixed so it can be placed in viewport
   * coordinates — a menu anchored inside a scrolling conversation slides away
   * from its message the moment anything moves. But `.chat-row` carries a
   * transform (the swipe-to-reply drag lives on it), and ANY transform makes
   * an element the containing block for fixed descendants. Left where it was
   * rendered, the menu's coordinates were being resolved against the row
   * instead of the window and it opened at x=2111 on a 1920px screen: open,
   * visible, correct in every computed style, and off the side of the world.
   *
   * So it is portalled out on open and put back on close, which keeps the
   * markup where it belongs — inside the message, with its forms and its CSRF
   * tokens — and the positioning where it has to be.
   */
  function closeMsgMenu() {
    if (!msgMenu) return;
    msgMenu.hidden = true;
    msgMenu.style.removeProperty('left');
    msgMenu.style.removeProperty('top');
    if (msgMenuRow) msgMenuRow.classList.remove('is-menu-open');
    /* Home again — unless the poll replaced the conversation while it was
       open, in which case its row is gone and the node goes with it rather
       than being left parked on the body forever. */
    if (msgMenuHome && document.body.contains(msgMenuHome)) msgMenuHome.appendChild(msgMenu);
    else msgMenu.remove();
    msgMenu = null; msgMenuHome = null; msgMenuRow = null;
  }

  function openMsgMenu(row, x, y) {
    var menu = row.querySelector('[data-msg-menu]');
    if (!menu) return;
    if (menu === msgMenu) { closeMsgMenu(); return; }
    closeMsgMenu();

    if (x == null) {
      var at = (row.querySelector('.chat-bubble') || row).getBoundingClientRect();
      x = at.left; y = at.bottom + 6;
    }

    msgMenuHome = menu.parentElement;
    msgMenuRow = row;
    document.body.appendChild(menu);
    menu.hidden = false;
    msgMenu = menu;
    row.classList.add('is-menu-open');

    /* Clamped so it cannot open with half of itself past an edge — on the
       newest message, which sits at the bottom of the stream, that is
       otherwise exactly where it opens. */
    var box = menu.getBoundingClientRect();
    var left = Math.min(x, window.innerWidth - box.width - 10);
    var top = Math.min(y, window.innerHeight - box.height - 10);
    menu.style.left = Math.max(10, left) + 'px';
    menu.style.top = Math.max(10, top) + 'px';
  }

  document.addEventListener('click', function (e) {
    var more = e.target.closest && e.target.closest('[data-msg-more]');
    if (more) {
      e.preventDefault(); e.stopPropagation();
      var r = more.closest('[data-msg]');
      var at = more.getBoundingClientRect();
      if (r) openMsgMenu(r, at.right - 200, at.bottom + 4);
      return;
    }
    /* Inside the menu is a command; anywhere else closes it. */
    if (e.target.closest('[data-msg-menu]')) return;
    if (msgMenu) { closeMsgMenu(); return; }

    var bubble = e.target.closest && e.target.closest('.chat-bubble');
    if (!bubble) return;
    /* A link in the message, a control, or a selection being made — all of
       those are what the press was for, and none of them is "open the menu". */
    if (e.target.closest('a, button, input, textarea, .chat-vn, video, audio')) return;
    var sel = window.getSelection();
    if (sel && String(sel).length) return;
    var row = bubble.closest('[data-msg]');
    if (row) openMsgMenu(row, e.clientX, e.clientY);
  });

  /* ---- the hold, on touch ---- */
  var holdTimer = null, holdFrom = null, holdRow = null;

  document.addEventListener('touchstart', function (e) {
    var bubble = e.target.closest && e.target.closest('.chat-bubble');
    if (!bubble) return;
    var row = bubble.closest('[data-msg]');
    if (!row || !row.querySelector('[data-msg-menu]')) return;
    holdRow = row;
    holdFrom = { x: e.touches[0].clientX, y: e.touches[0].clientY };
    row.classList.add('is-pressing');
    holdTimer = setTimeout(function () {
      row.classList.remove('is-pressing');
      var at = bubble.getBoundingClientRect();
      openMsgMenu(row, at.left, at.bottom + 6);
      holdRow = null;
      /* The press has become a menu, so the tap it would otherwise have been
         must not also fire — see the click guard below. */
      row.dataset.heldOpen = '1';
    }, 420);
  }, { passive: true });

  function cancelHold() {
    if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
    if (holdRow) { holdRow.classList.remove('is-pressing'); holdRow = null; }
  }
  document.addEventListener('touchmove', function (e) {
    /* A scroll, not a hold. Ten pixels of travel is enough to tell them apart
       and small enough that a steady finger is never mistaken for a swipe. */
    if (!holdFrom || !e.touches[0]) return;
    var t = e.touches[0];
    if (Math.abs(t.clientX - holdFrom.x) > 10 || Math.abs(t.clientY - holdFrom.y) > 10) cancelHold();
  }, { passive: true });
  document.addEventListener('touchend', cancelHold, { passive: true });
  document.addEventListener('touchcancel', cancelHold, { passive: true });
  document.addEventListener('click', function (e) {
    var row = e.target.closest && e.target.closest('[data-msg]');
    if (row && row.dataset.heldOpen) { delete row.dataset.heldOpen; e.stopPropagation(); }
  }, true);

  /* ---- and it has to CLOSE, completely ----
   * Client, of the conversation menu: "make sure it closes and closes
   * completely." The same three gaps apply to this one, and all three leave a
   * menu on screen that nothing will take down: it is position:fixed, so
   * scrolling slides the conversation out from under it and leaves it
   * floating; Escape did nothing; and a poll that replaces the stream detaches
   * the node this is holding, so the next close() has nothing to close.
   */
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && msgMenu) closeMsgMenu();
  });
  window.addEventListener('scroll', closeMsgMenu, { passive: true, capture: true });
  window.addEventListener('resize', closeMsgMenu, { passive: true });
  document.body && document.body.addEventListener('htmx:afterSettle', function () {
    /* The poll replaces the whole conversation every twelve seconds. An open
       menu is on the BODY by then, so the swap cannot take it — which means
       it would otherwise survive as an orphan pointing at a message that no
       longer exists on screen. Closed outright, and close() drops the node
       because its row has gone. */
    if (msgMenu) closeMsgMenu();
    document.querySelectorAll('[data-msg-menu]:not([hidden])').forEach(function (m) {
      m.hidden = true;
    });
  });

  /* ---- EDITING A MESSAGE IN PLACE ---------------------------------------
   *
   * The form is already in the page, rendered hidden beside the text it would
   * replace (partials/_chat.html). All this does is decide which of the two is
   * showing, which is why editing still works with JavaScript off: there the
   * box is simply always open.
   *
   * Delegated like everything else here, because the conversation is replaced
   * whole by the poll and anything bound to a bubble at load would stop
   * existing at the first refresh.
   */
  /* The box the control belongs to.
   *
   * Three shapes wear an editor and they are not nested the same way: a chat
   * bubble ([data-msg]), a Wall post ([data-chat-scope]) and a Wall reply
   * ([data-msg] again, inside the post). `closest` over both selectors picks
   * the innermost, which is what makes the pencil on a reply open the reply's
   * box and not the post's — the reply is found first on the way up. */
  var EDIT_HOST = '[data-msg], [data-chat-scope]';

  function hostOf(el) {
    return el && el.closest && el.closest(EDIT_HOST);
  }

  function editorIn(row) {
    return row && row.querySelector('[data-chat-edit]');
  }

  function openEditor(row) {
    var form = editorIn(row);
    if (!form) return;
    /* One at a time. Two half-finished edits in one thread is a way to lose
       the one you meant to keep. */
    document.querySelectorAll('[data-chat-edit]').forEach(function (f) {
      if (f !== form) f.hidden = true;
    });
    row.classList.add('is-editing');
    form.hidden = false;
    var box = form.querySelector('[data-chat-edit-body]');
    if (!box) return;
    box.style.height = 'auto';
    box.style.height = Math.min(box.scrollHeight, 150) + 'px';
    box.focus();
    // Caret at the end, not at the start: an edit is almost always a fix to
    // the end of a line, and selecting the whole thing invites replacing it.
    box.setSelectionRange(box.value.length, box.value.length);
  }

  function closeEditor(row) {
    var form = editorIn(row);
    if (!form) return;
    form.hidden = true;
    row.classList.remove('is-editing');
    // Put back what was actually sent, so reopening does not resume an
    // abandoned draft as though it were the message.
    var box = form.querySelector('[data-chat-edit-body]');
    if (box) box.value = box.defaultValue;
  }

  document.addEventListener('click', function (e) {
    var open = e.target.closest && e.target.closest('[data-chat-edit-open]');
    if (open) {
      closeMsgMenu();            // the menu did its job
      var row = hostOf(open);
      if (row) openEditor(row);
      return;
    }
    var cancel = e.target.closest && e.target.closest('[data-chat-edit-cancel]');
    if (cancel) {
      var cancelRow = hostOf(cancel);
      if (cancelRow) closeEditor(cancelRow);
    }
  });

  /* Escape closes it, as it closes everything else that opens over something.
     Enter sends it, because this is a chat and that is what Enter does here —
     Shift+Enter still breaks the line. */
  document.addEventListener('keydown', function (e) {
    var box = e.target;
    if (!box.matches || !box.matches('[data-chat-edit-body]')) return;
    if (e.key === 'Escape') {
      var row = hostOf(box);
      if (row) closeEditor(row);
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      var form = box.closest('form');
      if (form && form.requestSubmit) form.requestSubmit();
      else if (form) form.submit();
    }
  });

  /* ---- A POLL MUST NOT PULL A CLIP OUT FROM UNDER SOMEBODY --------------
   *
   * CLIENT, 17 SEP 2026: "the audio only lasts a few seconds after you click
   * on it and then it cuts off."
   *
   * Nothing was wrong with the player or the file. The conversation refreshes
   * itself every twelve seconds by replacing the whole scrolling pane
   * (partials/_room_stream.html), and a replaced <audio> is a destroyed
   * <audio>: the element that was playing is gone, its replacement is a new
   * one at 0:00 and paused. So every voice note stopped somewhere inside the
   * first twelve seconds, at a point that moved depending on when in the
   * cycle you pressed play — which is exactly what "a few seconds" describes.
   *
   * The pane already refuses to poll while the tab is in the background, for
   * the same class of reason. This is the other case where refreshing costs
   * more than it is worth: while something is playing inside it, the poll
   * waits. A voice note is capped at ninety seconds and the reader is, by
   * definition, right there — a message arriving during one shows up the
   * moment the clip finishes.
   *
   * Only actual playback holds it off. A note left PAUSED half way does lose
   * its place at the next swap, and that is the deliberate side of the trade:
   * a guard that also counted paused clips could be held open indefinitely by
   * somebody who pressed pause and walked away, and a conversation that never
   * updates again is a worse bug than the one being fixed.
   *
   * Applies to video by the same argument, and to any polled region — the
   * rule is about the region, not about this screen.
   */
  function mediaPlayingInside(el) {
    if (!el || !el.querySelectorAll) return false;
    var media = el.querySelectorAll('audio, video');
    for (var i = 0; i < media.length; i++) {
      if (!media[i].paused && !media[i].ended) return true;
    }
    return false;
  }

  document.body && document.body.addEventListener('htmx:beforeRequest', function (e) {
    var el = e.detail && e.detail.elt;
    if (!el || !el.getAttribute) return;
    // Polls only. A press of Send, a room change or a reply must always go.
    var trigger = el.getAttribute('hx-trigger') || '';
    if (trigger.indexOf('every') === -1) return;
    if (mediaPlayingInside(el)) { e.preventDefault(); return; }
    /* AN OPEN EDITOR IS STATE THE READER MADE, and the poll replaces the pane
       it sits in. The composer was moved outside the polled region for exactly
       this reason (see partials/_room_stream.html) — an editor cannot be,
       because it belongs to the message it is editing. So the poll waits, the
       same way it waits for a clip. Closing the box lets it through again. */
    if (el.querySelector('[data-chat-edit]:not([hidden])')) e.preventDefault();
  });
})();
