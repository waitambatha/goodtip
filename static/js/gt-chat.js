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
})();
