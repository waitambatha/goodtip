/* Rich-text editor for the News & blog story form.
 *
 * The old form was a plain textarea inside a sidebar aside, and the client
 * flagged both problems at once: the box felt too small for writing an actual
 * article, and there was no way to format anything (headings, bold, colour,
 * alignment, images). This drives three contenteditable surfaces (headline,
 * teaser, body) plus their toolbars, the font and size pickers and the
 * featured image drop zone — no editor library, just execCommand, since the
 * only audience is the superuser story form.
 *
 * Each toolbar control keeps working after a click/selection change because
 * clicking a <select> or <input type=color> moves focus away from the
 * contenteditable, which drops the browser's text selection. So every
 * surface's last real selection is cached on selectionchange and restored
 * immediately before any command runs.
 */
(function () {
  'use strict';

  var lastRange = new WeakMap();
  // One undo stack per surface, reachable from the styling helpers below so a
  // font/size/colour change is a step you can take back like any other.
  var histories = new WeakMap();

  /* Bracket an edit with a commit on each side: the state before it becomes a
   * step to come back to, and the state after it becomes the new present. */
  function mark(surface) {
    var h = histories.get(surface);
    if (h) h.commit();
  }

  function inSurface(surface, node) {
    return !!node && surface.contains(node);
  }

  function saveSelection(surface) {
    var sel = window.getSelection();
    if (sel && sel.rangeCount && inSurface(surface, sel.anchorNode)) {
      lastRange.set(surface, sel.getRangeAt(0).cloneRange());
    }
  }

  function restoreSelection(surface) {
    var range = lastRange.get(surface);
    surface.focus();
    if (!range) return;
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }

  function escapeHtml(s) {
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function escapeAttr(s) {
    return escapeHtml(s).replace(/"/g, '&quot;');
  }

  function normaliseUrl(url) {
    url = (url || '').trim();
    if (!url) return '';
    if (/^(https?:|mailto:)/i.test(url)) return url;
    return 'https://' + url.replace(/^\/+/, '');
  }

  // Held open inside an otherwise empty span so the browser cannot throw the
  // span away before anything is typed into it. Stripped on the way out.
  var ZWSP = '\u200B';

  /* Put one inline style on whatever is selected — the whole of size and font.
   *
   * execCommand is the only thing that knows how to slice a selection running
   * across half a bold run and two paragraphs, but it can only speak in the
   * seven legacy <font size> buckets. So ask it for the top bucket purely to
   * borrow that slicing, then swap every tag it just made for a span carrying
   * the real value.
   *
   * That swap was the bug behind "the + button doesn't work": replacing the
   * <font> tags threw away the very nodes the cached selection pointed at, so
   * every click after the first restored a range into detached DOM and resized
   * nothing at all. The new spans are re-selected here and the cache updated
   * with them, so the control keeps working click after click.
   */
  function applyInlineStyle(surface, prop, value) {
    restoreSelection(surface);
    var sel = window.getSelection();
    if (!sel || !sel.rangeCount) return;
    mark(surface);

    if (sel.isCollapsed) {
      // Nothing selected. Rather than do nothing — which is what it looked
      // like was happening — start a fresh run at the caret, so the next thing
      // typed comes out in the size or font just asked for.
      var caretSpan = document.createElement('span');
      caretSpan.style[prop] = value;
      caretSpan.appendChild(document.createTextNode(ZWSP));
      sel.getRangeAt(0).insertNode(caretSpan);
      var inside = document.createRange();
      inside.setStart(caretSpan.firstChild, 1);
      inside.collapse(true);
      sel.removeAllRanges();
      sel.addRange(inside);
      lastRange.set(surface, inside.cloneRange());
      syncHidden(surface);
      mark(surface);
      return;
    }

    // Chrome and Firefox default to <font> tags here, but a page that has
    // turned CSS styling on gets spans marked xxx-large instead; catch both.
    try { document.execCommand('styleWithCSS', false, false); } catch (e) {}
    document.execCommand('fontSize', false, '7');

    var legacy = prop === 'fontSize' ? 'size' : 'face';
    var made = surface.querySelectorAll('font[size="7"], span[style*="xxx-large"]');
    if (!made.length) { syncHidden(surface); mark(surface); return; }

    var spans = [];
    made.forEach(function (el) {
      var span;
      if (el.tagName === 'FONT') {
        span = document.createElement('span');
        // Move the nodes rather than copy their markup: innerHTML would build
        // fresh ones and drop the selection on the floor.
        while (el.firstChild) span.appendChild(el.firstChild);
        el.replaceWith(span);
      } else {
        span = el;
        span.style.fontSize = '';
      }
      span.style[prop] = value;
      // A run nested inside this one with its own value would win over it.
      span.querySelectorAll('[style]').forEach(function (n) { n.style[prop] = ''; });
      span.querySelectorAll('font[' + legacy + ']').forEach(function (n) {
        n.removeAttribute(legacy);
      });
      spans.push(span);
    });

    var range = document.createRange();
    range.setStartBefore(spans[0]);
    range.setEndAfter(spans[spans.length - 1]);
    sel.removeAllRanges();
    sel.addRange(range);
    lastRange.set(surface, range.cloneRange());
    syncHidden(surface);
    mark(surface);
  }

  function applyFontSize(surface, px) {
    applyInlineStyle(surface, 'fontSize', px + 'px');
  }

  function applyFontFamily(surface, stack) {
    applyInlineStyle(surface, 'fontFamily', stack);
  }

  function applyTextColour(surface, colour) {
    applyInlineStyle(surface, 'color', colour);
  }

  /* Highlight. "None" arrives here as `transparent` rather than an empty
   * string on purpose: an empty value only clears the background on the run
   * being written, and a highlight applied further up the tree would show
   * straight through it. */
  function applyHighlight(surface, colour) {
    applyInlineStyle(surface, 'backgroundColor', colour);
  }

  /* "Georgia, serif" and '"Georgia", serif' are the same font asked for two
   * ways — compare the first name only, unquoted and lowercased. */
  function firstFamily(stack) {
    return (stack || '').split(',')[0].trim().replace(/^["']|["']$/g, '').toLowerCase();
  }

  /* Size of the text the caret is sitting in, so the number box reports where
   * you already are rather than starting from a guess every time. */
  function currentFontSize(surface) {
    var sel = window.getSelection();
    if (!sel || !sel.rangeCount || !inSurface(surface, sel.anchorNode)) return null;
    var node = sel.anchorNode;
    var el = node.nodeType === 1 ? node : node.parentElement;
    if (!el) return null;
    var px = parseFloat(window.getComputedStyle(el).fontSize);
    return px ? Math.round(px) : null;
  }

  /* ---- Undo / redo ------------------------------------------------------
   *
   * The browser has its own undo stack and this editor cannot use it. Size,
   * font and colour are applied by rewriting nodes directly (applyInlineStyle
   * above swaps every <font> tag execCommand makes for a span), and a DOM edit
   * made outside execCommand is not something the native stack knows how to
   * put back. The visible symptom was Ctrl+Z either doing nothing or unwinding
   * to some state the author never typed.
   *
   * So each surface keeps its own stack of snapshots. A snapshot is the
   * surface's HTML plus where the caret was, measured as a count of characters
   * from the start of the surface — an offset survives having the whole
   * innerHTML replaced, which a Range pointing at particular nodes does not.
   *
   * Typing is coalesced: a burst of keystrokes settles into one step after a
   * short pause, so undo steps back a word or a phrase rather than a letter.
   * Toolbar commands commit on both sides of themselves, so one Ctrl+Z always
   * takes a formatting change straight back off.
   */

  var TYPING_PAUSE = 400;
  var MAX_STEPS = 120;

  function caretOffset(surface) {
    var sel = window.getSelection();
    if (!sel || !sel.rangeCount) return null;
    var range = sel.getRangeAt(0);
    if (!surface.contains(range.endContainer)) return null;
    var probe = range.cloneRange();
    probe.selectNodeContents(surface);
    probe.setEnd(range.endContainer, range.endOffset);
    return probe.toString().length;
  }

  function setCaretOffset(surface, offset) {
    if (offset == null) return;
    var walker = document.createTreeWalker(surface, NodeFilter.SHOW_TEXT, null);
    var seen = 0;
    var node;
    while ((node = walker.nextNode())) {
      var end = seen + node.length;
      if (offset <= end) {
        var range = document.createRange();
        range.setStart(node, offset - seen);
        range.collapse(true);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        lastRange.set(surface, range.cloneRange());
        return;
      }
      seen = end;
    }
    // Ran off the end — the restored HTML is shorter than the offset. Park the
    // caret at the end rather than leaving it in the previous document.
    var tail = document.createRange();
    tail.selectNodeContents(surface);
    tail.collapse(false);
    var s2 = window.getSelection();
    s2.removeAllRanges();
    s2.addRange(tail);
    lastRange.set(surface, tail.cloneRange());
  }

  function makeHistory(surface) {
    var stack = [];
    var index = -1;
    var timer = null;
    var restoring = false;
    var listeners = [];

    function notify() {
      listeners.forEach(function (fn) { fn(index > 0, index < stack.length - 1); });
    }

    /* Put the surface as it stands right now on the stack. A no-op when
     * nothing has changed since the last entry, which is what lets this be
     * called liberally — before a command, after a command, on a typing
     * pause — without filling the stack with duplicates. */
    function commit() {
      if (restoring) return false;
      var html = surface.innerHTML;
      if (index >= 0 && stack[index].html === html) return false;
      // Anything that was undone past is abandoned the moment a new edit is
      // made, the same as every other editor.
      stack.length = index + 1;
      stack.push({ html: html, caret: caretOffset(surface) });
      if (stack.length > MAX_STEPS) stack.shift();
      index = stack.length - 1;
      notify();
      return true;
    }

    function schedule() {
      window.clearTimeout(timer);
      timer = window.setTimeout(commit, TYPING_PAUSE);
    }

    function restore(state) {
      restoring = true;
      surface.innerHTML = state.html;
      surface.focus();
      setCaretOffset(surface, state.caret);
      restoring = false;
      syncHidden(surface);
      notify();
    }

    function undo() {
      window.clearTimeout(timer);
      // Whatever has been typed since the last commit is itself a step, or
      // the first Ctrl+Z would leap over it.
      commit();
      if (index <= 0) return;
      index -= 1;
      restore(stack[index]);
    }

    function redo() {
      window.clearTimeout(timer);
      if (index >= stack.length - 1) return;
      index += 1;
      restore(stack[index]);
    }

    commit();

    return {
      commit: commit,
      schedule: schedule,
      undo: undo,
      redo: redo,
      canUndo: function () { return index > 0; },
      canRedo: function () { return index < stack.length - 1; },
      onChange: function (fn) { listeners.push(fn); fn(index > 0, index < stack.length - 1); },
    };
  }

  /* ---- Toolbar drop-downs ---------------------------------------------
   *
   * Hand-built rather than a <select>, for two reasons the client ran into:
   * the browser decides how wide a select is and cuts the longer names off
   * mid-word, and an <option> cannot be drawn in the font it names. A button
   * and a list of buttons can do both — and the list can scroll, which is what
   * turns twenty font sizes into something you pick from instead of click
   * towards one step at a time.
   */

  // The sizes worth offering: every point through the body-text range, then
  // widening gaps once the jumps stop mattering. Anything else can still be
  // typed into the box.
  var SIZE_PRESETS = [10, 11, 12, 13, 14, 15, 16, 17, 18, 20, 22, 24, 28,
                      32, 36, 40, 48, 56, 64, 72, 96];

  function setMenuOpen(menu, open) {
    menu.classList.toggle('open', open);
    var owner = menu.parentElement.querySelector('[aria-haspopup]');
    if (owner) owner.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  function closeMenus(editor, except) {
    editor.querySelectorAll('.ned-menu.open').forEach(function (m) {
      if (m !== except) setMenuOpen(m, false);
    });
  }

  /* Open with the current value already highlighted and scrolled to, so a
   * list of twenty sizes opens showing the one you are on. */
  function openMenu(editor, menu, isCurrent) {
    closeMenus(editor, menu);
    var on = null;
    menu.querySelectorAll('.ned-menu-item').forEach(function (item) {
      var hit = isCurrent(item);
      item.classList.toggle('is-on', hit);
      if (hit) on = item;
    });
    setMenuOpen(menu, true);
    if (on && on.scrollIntoView) on.scrollIntoView({ block: 'nearest' });
  }

  /* The size control: −  [ 17 ▾ ]  +
   *
   * Returns the number input so the caller can keep it in step with the
   * selection. The − and + buttons cancel mousedown, because the surface loses
   * its text selection the moment focus moves and there would be nothing left
   * to resize; the number box does not, because it has to be typeable. */
  function wireSizeControl(editor, getSurface) {
    var box = editor.querySelector('.ned-size');
    if (!box) return null;
    var num = box.querySelector('[data-cmd="fontSizePx"]');
    if (!num) return null;

    var fallback = parseInt(box.getAttribute('data-size-default'), 10) || 16;
    var min = parseInt(num.getAttribute('min'), 10) || 8;
    var max = parseInt(num.getAttribute('max'), 10) || 120;
    var menu = box.querySelector('[data-size-menu]');

    function value() {
      var v = parseInt(num.value, 10);
      return isNaN(v) ? fallback : Math.min(max, Math.max(min, v));
    }

    function apply(px) {
      num.value = px;
      applyFontSize(getSurface(), px);
    }

    if (menu) {
      SIZE_PRESETS.forEach(function (px) {
        var item = document.createElement('button');
        item.type = 'button';
        item.className = 'ned-menu-item';
        item.setAttribute('role', 'option');
        item.setAttribute('data-size', px);
        item.textContent = px;
        menu.appendChild(item);
      });
      menu.addEventListener('mousedown', function (e) {
        var item = e.target.closest('[data-size]');
        if (!item) return;
        e.preventDefault();
        setMenuOpen(menu, false);
        apply(parseInt(item.getAttribute('data-size'), 10));
      });
      // Click, not mousedown: the box still has to take focus and a caret so
      // a size that isn't on the list can be typed straight in.
      num.addEventListener('click', function () {
        openMenu(editor, menu, function (item) {
          return parseInt(item.getAttribute('data-size'), 10) === value();
        });
        num.select();
      });
    }

    num.addEventListener('change', function () { apply(value()); });
    num.addEventListener('keydown', function (e) {
      // Enter inside a number box would otherwise submit the whole story.
      if (e.key === 'Enter') { e.preventDefault(); apply(value()); }
      if (e.key === 'Escape' && menu) setMenuOpen(menu, false);
      // The arrows already step the number; make them style the text too,
      // rather than leaving the box saying 18 over text still at 17.
      if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        e.preventDefault();
        apply(Math.min(max, Math.max(min, value() + (e.key === 'ArrowUp' ? 1 : -1))));
      }
    });

    box.querySelectorAll('[data-size-step]').forEach(function (btn) {
      btn.addEventListener('mousedown', function (e) {
        e.preventDefault();
        if (menu) setMenuOpen(menu, false);
        var step = parseInt(btn.getAttribute('data-size-step'), 10) || 0;
        apply(Math.min(max, Math.max(min, value() + step)));
      });
    });

    return num;
  }

  /* The font picker: [ Archivo ▾ ] over a list drawn in the fonts themselves.
   *
   * Returns a function that re-labels the button for wherever the caret is,
   * so the toolbar says which font you are in rather than a fixed "Font". */
  function wireFontControl(editor, getSurface) {
    var box = editor.querySelector('.ned-font');
    if (!box) return null;
    var toggle = box.querySelector('[data-font-toggle]');
    var label = box.querySelector('[data-font-label]');
    var menu = box.querySelector('[data-font-menu]');
    if (!toggle || !menu || !label) return null;

    function stackOf(item) { return item.getAttribute('data-font'); }

    function current() {
      var surface = getSurface();
      var sel = window.getSelection();
      if (!surface || !sel || !sel.rangeCount || !inSurface(surface, sel.anchorNode)) return null;
      var node = sel.anchorNode;
      var el = node.nodeType === 1 ? node : node.parentElement;
      if (!el) return null;
      return firstFamily(window.getComputedStyle(el).fontFamily);
    }

    toggle.addEventListener('mousedown', function (e) {
      e.preventDefault();
      if (menu.classList.contains('open')) { setMenuOpen(menu, false); return; }
      var now = current();
      openMenu(editor, menu, function (item) {
        return !!now && firstFamily(stackOf(item)) === now;
      });
    });

    menu.addEventListener('mousedown', function (e) {
      var item = e.target.closest('[data-font]');
      if (!item) return;
      e.preventDefault();
      setMenuOpen(menu, false);
      applyFontFamily(getSurface(), stackOf(item));
    });

    return function syncLabel() {
      var now = current();
      var match = null;
      if (now) {
        menu.querySelectorAll('[data-font]').forEach(function (item) {
          if (!match && firstFamily(stackOf(item)) === now) match = item;
        });
      }
      label.textContent = match ? match.getAttribute('data-font-name') : 'Font';
      // Show it in the font it names, the same as the list does.
      label.style.fontFamily = match ? stackOf(match) : '';
    };
  }

  /* The colour and highlight controls: a button over a grid of swatches.
   *
   * Same shape as the font and size pickers, and for the same reason — the
   * native <input type="color"> opens the operating system's colour dialog,
   * which is a modal three clicks deep and knows nothing about the house
   * palette. The custom input is still in the menu for anything off-palette.
   *
   * Returns a function that re-paints the bar under the button to show the
   * colour the caret is currently sitting in. */
  function wireSwatchControl(editor, getSurface, box) {
    var kind = box.getAttribute('data-swatch');
    var prop = kind === 'mark' ? 'backgroundColor' : 'color';
    var toggle = box.querySelector('[data-swatch-toggle]');
    var menu = box.querySelector('[data-swatch-menu]');
    var bar = box.querySelector('[data-swatch-bar]');
    var custom = box.querySelector('[data-swatch-custom]');
    if (!toggle || !menu) return null;

    function apply(colour) {
      var surface = getSurface();
      if (!surface) return;
      if (kind === 'mark') applyHighlight(surface, colour);
      else applyTextColour(surface, colour);
      if (bar) bar.style.background = colour;
    }

    toggle.addEventListener('mousedown', function (e) {
      e.preventDefault();
      if (menu.classList.contains('open')) { setMenuOpen(menu, false); return; }
      var now = currentColour();
      openMenu(editor, menu, function (item) {
        var c = item.getAttribute('data-colour');
        return !!now && !!c && sameColour(c, now);
      });
    });

    menu.addEventListener('mousedown', function (e) {
      var item = e.target.closest('[data-colour]');
      if (!item) return;
      e.preventDefault();
      setMenuOpen(menu, false);
      apply(item.getAttribute('data-colour'));
    });

    // The custom picker fires `input` continuously while the dialog is open,
    // which would put a step on the undo stack for every shade dragged
    // through. `change` fires once, when they settle on one.
    if (custom) {
      custom.addEventListener('mousedown', function () {
        var surface = getSurface();
        if (surface) saveSelection(surface);
      });
      custom.addEventListener('change', function () {
        setMenuOpen(menu, false);
        apply(custom.value);
      });
    }

    function currentColour() {
      var surface = getSurface();
      var sel = window.getSelection();
      if (!surface || !sel || !sel.rangeCount || !inSurface(surface, sel.anchorNode)) return null;
      var node = sel.anchorNode;
      var el = node.nodeType === 1 ? node : node.parentElement;
      if (!el) return null;
      return window.getComputedStyle(el)[prop];
    }

    return function syncBar() {
      if (!bar) return;
      var now = currentColour();
      // A see-through background means no highlight — show the button as
      // empty rather than painting it the colour of the page behind it.
      if (!now || (kind === 'mark' && isTransparent(now))) {
        bar.style.background = '';
        bar.classList.add('is-none');
        return;
      }
      bar.classList.remove('is-none');
      bar.style.background = now;
    };
  }

  function isTransparent(value) {
    return /^(transparent|rgba\(0,\s*0,\s*0,\s*0\))$/i.test((value || '').trim());
  }

  /* "#C8F135" from the markup versus "rgb(200, 241, 53)" from getComputedStyle
   * are the same colour written two ways. Resolve both through the browser and
   * compare what comes back. */
  var colourProbe = null;
  function resolveColour(value) {
    if (!colourProbe) {
      colourProbe = document.createElement('span');
      colourProbe.style.display = 'none';
      document.body.appendChild(colourProbe);
    }
    colourProbe.style.color = '';
    colourProbe.style.color = value;
    return window.getComputedStyle(colourProbe).color;
  }

  function sameColour(a, b) {
    if (isTransparent(a) || isTransparent(b)) return isTransparent(a) && isTransparent(b);
    try { return resolveColour(a) === resolveColour(b); } catch (e) { return false; }
  }

  function insertLink(surface, url) {
    var safe = normaliseUrl(url);
    if (!safe) return;
    restoreSelection(surface);
    var sel = window.getSelection();
    var text = sel && sel.toString();
    var label = text ? escapeHtml(text) : escapeHtml(safe);
    document.execCommand(
      'insertHTML', false,
      '<a href="' + escapeAttr(safe) + '" target="_blank" rel="noopener">' + label + '</a>'
    );
    syncHidden(surface);
  }

  function csrfToken(form) {
    var el = form.querySelector('input[name="csrfmiddlewaretoken"]');
    return el ? el.value : '';
  }

  /* ALT TEXT IS ASKED FOR AT INSERT, NOT LEFT TO BE ADDED LATER.
   *
   * Every picture the editor put in a story used to go in as alt="" — invisible
   * to a screen reader and worth nothing to a search engine — and there was no
   * way to fix that afterwards without editing HTML. Asked here, at the one
   * moment the author knows what the picture shows and is already looking at
   * it. Skipping is allowed and stores alt="", which is the correct markup for
   * a decorative image; what is not allowed is having no way to say. */
  function askAlt(existing) {
    var answer = window.prompt(
      'Describe this picture for screen readers and search engines.\n' +
      'Leave it empty if the picture is purely decorative.',
      existing || ''
    );
    // null means Cancel, which for an existing image must leave it alone.
    return answer === null ? null : answer.trim();
  }

  function insertImage(editor, surface, file) {
    var uploadUrl = editor.getAttribute('data-upload-url');
    if (!uploadUrl || !file) return;
    var form = surface.closest('form');
    var data = new FormData();
    data.append('file', file);
    fetch(uploadUrl, {
      method: 'POST',
      headers: { 'X-CSRFToken': csrfToken(form) },
      body: data,
    }).then(function (r) { return r.json(); }).then(function (json) {
      if (!json.url) return;
      var alt = askAlt('');
      restoreSelection(surface);
      document.execCommand(
        'insertHTML', false,
        '<img src="' + escapeAttr(json.url) + '" alt="' + escapeAttr(alt || '') + '">'
      );
      syncHidden(surface);
    }).catch(function () {
      window.alert("Couldn't upload that image, try again.");
    });
  }

  /* And afterwards: click a picture already in the story to change its
   * description. Without this the only chance to get alt text right is the
   * half-second the file finishes uploading, which is not a chance. */
  function wireImageAltEditing(surface) {
    surface.addEventListener('click', function (e) {
      var img = e.target;
      if (!img || img.tagName !== 'IMG') return;
      var next = askAlt(img.getAttribute('alt') || '');
      if (next === null) return;
      img.setAttribute('alt', next);
      syncHidden(surface);
    });
  }

  function syncHidden(surface) {
    var input = document.getElementById(surface.getAttribute('data-hidden-input'));
    // The zero-width space that holds a just-started run open is scaffolding
    // for the editor, not part of the story — drop it on the way to the form.
    if (input) input.value = surface.innerHTML.replace(/\u200B/g, '');
  }

  function closePopovers(editor) {
    editor.querySelectorAll('.ned-pop.open').forEach(function (p) { p.classList.remove('open'); });
    closeMenus(editor);
  }

  /* The undo/redo pair. Buttons as well as the keyboard shortcut, because the
   * shortcut is invisible: an author who has just made a mess with the colour
   * picker needs to be able to see the way back. Both grey out when there is
   * nothing left in that direction, which is also the only feedback that the
   * stack is tracking their work at all. */
  function wireHistoryButtons(editor, getSurface) {
    var undoBtn = editor.querySelector('[data-history="undo"]');
    var redoBtn = editor.querySelector('[data-history="redo"]');
    if (!undoBtn && !redoBtn) return null;

    [[undoBtn, 'undo'], [redoBtn, 'redo']].forEach(function (pair) {
      if (!pair[0]) return;
      pair[0].addEventListener('mousedown', function (e) {
        e.preventDefault();
        var h = histories.get(getSurface());
        if (h) h[pair[1]]();
      });
    });

    // Called after anything that could have changed what is available, and
    // whenever the toolbar changes which surface it is pointed at.
    return function syncHistoryButtons() {
      var h = histories.get(getSurface());
      if (undoBtn) undoBtn.disabled = !(h && h.canUndo());
      if (redoBtn) redoBtn.disabled = !(h && h.canRedo());
    };
  }

  /* Light up bold/italic/underline/strikethrough and the alignment buttons for
   * whatever the caret is inside. Without this the toolbar is write-only —
   * you can turn bold on but the button looks identical either way, so the
   * only way to know is to look at the letters. */
  var STATE_CMDS = [
    'bold', 'italic', 'underline', 'strikeThrough',
    'justifyLeft', 'justifyCenter', 'justifyRight', 'justifyFull',
    'insertUnorderedList', 'insertOrderedList',
  ];

  function syncActiveStates(editor, surface) {
    var sel = window.getSelection();
    if (!sel || !sel.rangeCount || !inSurface(surface, sel.anchorNode)) return;
    STATE_CMDS.forEach(function (cmd) {
      var btn = editor.querySelector('button[data-cmd="' + cmd + '"]');
      if (!btn) return;
      var on = false;
      // queryCommandState throws on commands a browser does not implement.
      try { on = document.queryCommandState(cmd); } catch (e) { on = false; }
      btn.classList.toggle('is-on', !!on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  /* ---- The two halves of an editor -------------------------------------
   *
   * Split because the same machinery drives two different things. The story
   * form has one toolbar per surface, sitting directly above it. The page
   * editor (gt-page-editor.js) has one floating toolbar and a whole page full
   * of surfaces, and it points that toolbar at whichever block is being
   * edited. Everything below is therefore written against "the surface the
   * toolbar is currently for" rather than a fixed one.
   */

  /* Give a contenteditable element its undo stack and its keyboard. */
  function attachSurface(surface, opts) {
    if (histories.has(surface)) return histories.get(surface);
    opts = opts || {};

    var history = makeHistory(surface);
    histories.set(surface, history);

    function changed() {
      syncHidden(surface);
      if (opts.onChange) opts.onChange(surface);
    }

    changed();
    surface.addEventListener('input', function () {
      changed();
      // Coalesced: a run of keystrokes becomes one step once typing pauses.
      history.schedule();
    });
    surface.addEventListener('blur', function () {
      changed();
      history.commit();
    });

    /* Ctrl/Cmd+Z and Ctrl+Shift+Z (or Ctrl+Y) drive this editor's own stack.
     * The browser's native undo has to be stopped rather than left alongside
     * it: the two disagree — the DOM rewriting that size, font and colour do
     * is invisible to the native one — and letting both run is how you end up
     * somewhere the author never typed. */
    surface.addEventListener('keydown', function (e) {
      if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
      var key = (e.key || '').toLowerCase();
      if (key === 'z') {
        e.preventDefault();
        if (e.shiftKey) history.redo(); else history.undo();
        changed();
      } else if (key === 'y') {
        e.preventDefault();
        history.redo();
        changed();
      }
    });
    // Undo reached any other way — the browser's Edit menu, a trackpad
    // gesture, a phone keyboard — arrives as beforeinput and never as a
    // keydown, so it needs catching separately or it would bypass the stack.
    surface.addEventListener('beforeinput', function (e) {
      if (e.inputType === 'historyUndo') { e.preventDefault(); history.undo(); changed(); }
      else if (e.inputType === 'historyRedo') { e.preventDefault(); history.redo(); changed(); }
    });

    return history;
  }

  /* Wire every control in `editor` to whatever getSurface() returns.
   *
   * Returns a `sync` function that repaints the toolbar — pressed states, the
   * font name, the size box, the colour bars, the undo/redo buttons — for the
   * current selection. It runs on selectionchange, and the page editor calls
   * it again whenever it re-points the toolbar at a different block.
   */
  function wireToolbar(editor, getSurface, opts) {
    opts = opts || {};

    function surfaceNow() { return getSurface(); }
    function after() {
      var surface = surfaceNow();
      if (!surface) return;
      syncHidden(surface);
      if (opts.onChange) opts.onChange(surface);
    }
    function commit() {
      var h = histories.get(surfaceNow());
      if (h) h.commit();
    }

    var sizeBox = wireSizeControl(editor, surfaceNow);
    var syncFontLabel = wireFontControl(editor, surfaceNow);
    var syncSwatches = [];
    editor.querySelectorAll('[data-swatch]').forEach(function (box) {
      var fn = wireSwatchControl(editor, surfaceNow, box);
      if (fn) syncSwatches.push(fn);
    });
    var syncHistoryButtons = wireHistoryButtons(editor, surfaceNow);
    // Only the body surface takes pictures; the headline and teaser have no
    // image button, so there is nothing there to click.
    if (editor.getAttribute('data-upload-url')) {
      editor.querySelectorAll('.ned-surface').forEach(wireImageAltEditing);
    }

    function sync() {
      var surface = surfaceNow();
      if (syncHistoryButtons) syncHistoryButtons();
      if (!surface) return;
      if (syncFontLabel) syncFontLabel();
      syncSwatches.forEach(function (fn) { fn(); });
      syncActiveStates(editor, surface);
      // Don't fight the author while they are typing a number into the box.
      if (!sizeBox || document.activeElement === sizeBox) return;
      var px = currentFontSize(surface);
      if (px) sizeBox.value = px;
    }

    document.addEventListener('selectionchange', function () {
      var surface = surfaceNow();
      if (surface) saveSelection(surface);
      sync();
    });

    editor.querySelectorAll('[data-cmd]').forEach(function (ctrl) {
      var cmd = ctrl.getAttribute('data-cmd');

      if (ctrl.tagName === 'BUTTON') {
        // preventDefault on mousedown keeps the contenteditable's selection
        // alive — a click alone would blur the surface first.
        ctrl.addEventListener('mousedown', function (e) {
          e.preventDefault();
          var surface = surfaceNow();
          if (!surface) return;
          restoreSelection(surface);
          commit();
          document.execCommand(cmd, false, ctrl.getAttribute('data-value') || undefined);
          after();
          commit();
          sync();
        });
        return;
      }

      // The size box is a number input, not a command select — wireSizeControl
      // owns it, so it must not fall through to the generic handlers below.
      if (cmd === 'fontSizePx') return;

      if (ctrl.tagName === 'SELECT') {
        ctrl.addEventListener('change', function () {
          var surface = surfaceNow();
          if (!ctrl.value || !surface) return;
          restoreSelection(surface);
          commit();
          document.execCommand(cmd, false, ctrl.value);
          after();
          commit();
          ctrl.selectedIndex = 0;
          sync();
        });
        return;
      }

      if (ctrl.type === 'color') {
        // `change`, not `input`: a colour dialog fires `input` for every shade
        // dragged through, and each one would land on the undo stack.
        ctrl.addEventListener('change', function () {
          var surface = surfaceNow();
          if (!surface) return;
          restoreSelection(surface);
          commit();
          document.execCommand(cmd, false, ctrl.value);
          after();
          commit();
          sync();
        });
      }
    });

    editor.querySelectorAll('[data-action]').forEach(function (ctrl) {
      var action = ctrl.getAttribute('data-action');

      /* Strip formatting back to plain text.
       *
       * removeFormat alone is not enough here. It clears what execCommand
       * itself applied — bold, italic, <font> — but the size, font and colour
       * controls write inline styles onto spans, and those it leaves exactly
       * where they are. So the styles this editor puts on are taken off by
       * hand afterwards, over every element the selection touches. */
      if (action === 'clearFormat') {
        ctrl.addEventListener('mousedown', function (e) {
          e.preventDefault();
          var surface = surfaceNow();
          if (!surface) return;
          restoreSelection(surface);
          commit();
          document.execCommand('removeFormat');
          var sel = window.getSelection();
          if (sel && sel.rangeCount && !sel.isCollapsed) {
            var range = sel.getRangeAt(0);
            surface.querySelectorAll('[style]').forEach(function (el) {
              if (!range.intersectsNode(el)) return;
              ['fontSize', 'fontFamily', 'color', 'backgroundColor'].forEach(function (prop) {
                el.style[prop] = '';
              });
              if (!el.getAttribute('style')) el.removeAttribute('style');
            });
          }
          after();
          commit();
          sync();
        });
        return;
      }

      if (action === 'link') {
        var pop = editor.querySelector('.ned-pop[data-pop="link"]');
        ctrl.addEventListener('mousedown', function (e) {
          e.preventDefault();
          var surface = surfaceNow();
          if (surface) saveSelection(surface);
          closePopovers(editor);
          if (pop) {
            pop.classList.add('open');
            var input = pop.querySelector('input');
            if (input) { input.value = ''; input.focus(); }
          }
        });
        if (pop) {
          var applyBtn = pop.querySelector('[data-pop-apply]');
          var linkInput = pop.querySelector('input');
          var apply = function () {
            var surface = surfaceNow();
            if (!surface) return;
            commit();
            insertLink(surface, linkInput.value);
            after();
            commit();
            pop.classList.remove('open');
          };
          if (applyBtn) applyBtn.addEventListener('click', apply);
          if (linkInput) linkInput.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); apply(); }
            if (e.key === 'Escape') pop.classList.remove('open');
          });
        }
        return;
      }

      if (action === 'image') {
        var fileInput = editor.querySelector('[data-inline-image]');
        ctrl.addEventListener('mousedown', function (e) {
          e.preventDefault();
          var surface = surfaceNow();
          if (surface) saveSelection(surface);
        });
        ctrl.addEventListener('click', function () {
          if (fileInput) fileInput.click();
        });
        if (fileInput) fileInput.addEventListener('change', function () {
          var surface = surfaceNow();
          if (surface && fileInput.files && fileInput.files[0]) {
            commit();
            insertImage(editor, surface, fileInput.files[0]);
          }
          fileInput.value = '';
        });
      }
    });

    document.addEventListener('mousedown', function (e) {
      if (!editor.contains(e.target)) { closePopovers(editor); return; }
      // Inside this editor too: clicking into the text, or onto another
      // control, should put an open list away — only the list's own control
      // gets to keep it open, or the toggle would close and reopen at once.
      editor.querySelectorAll('.ned-menu.open').forEach(function (m) {
        if (!m.parentElement.contains(e.target)) setMenuOpen(m, false);
      });
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeMenus(editor);
    });

    return sync;
  }

  function wireEditor(editor) {
    var surface = editor.querySelector('.ned-surface');
    if (!surface) return;

    var history = attachSurface(surface);
    var sync = wireToolbar(editor, function () { return surface; });
    // The undo/redo buttons otherwise only repainted on selectionchange, so
    // they lagged a typing burst by however long the coalescing pause is.
    history.onChange(sync);

    var form = surface.closest('form');
    if (form) form.addEventListener('submit', function () { syncHidden(surface); });
  }

  function slugify(text) {
    return (text || '').toLowerCase()
      .replace(/[^a-z0-9\s-]/g, '')
      .trim()
      .replace(/[\s-]+/g, '-')
      .slice(0, 60);
  }

  function wireSlugPreview() {
    var headline = document.querySelector('[data-editor="headline"] .ned-surface');
    var preview = document.querySelector('[data-slug-preview]');
    if (!headline || !preview) return;
    var base = preview.getAttribute('data-slug-preview') || '/news/';
    var fixed = preview.getAttribute('data-slug-fixed');
    var render = function () {
      var slug = fixed || slugify(headline.textContent) || 'your-story';
      preview.textContent = base + slug + '/';
    };
    render();
    headline.addEventListener('input', render);
  }

  // Optional "sources" list: repeatable label + URL rows, added/removed freely
  // since the client wants this available but never required.
  function wireSourceRows() {
    var list = document.querySelector('[data-source-list]');
    var addBtn = document.querySelector('[data-source-add]');
    var template = document.querySelector('[data-source-template]');
    if (!list || !addBtn || !template) return;

    function addRow() {
      var row = template.content.firstElementChild.cloneNode(true);
      list.appendChild(row);
    }

    addBtn.addEventListener('click', addRow);
    list.addEventListener('click', function (e) {
      var removeBtn = e.target.closest('[data-source-remove]');
      if (removeBtn) removeBtn.closest('.ned-source-row').remove();
    });
  }

  /* Featured image: a real drop zone rather than a bare file input.
   *
   * The browser's own control cannot be styled, gives no preview, shows a
   * truncated filename, and offers no way to take an image back off a post.
   * So the input is driven from a button beside it, the chosen file is shown
   * immediately from a local object URL — no upload round trip — and Remove
   * sets a flag the view reads to clear the field. */
  function wireFeaturedImage() {
    var drop = document.querySelector('[data-image-drop]');
    if (!drop) return;

    var input = drop.querySelector('[data-image-input]');
    var preview = drop.querySelector('[data-image-preview]');
    var nameEl = drop.querySelector('[data-image-name]');
    var noteEl = drop.querySelector('[data-image-note]');
    var pickLabel = drop.querySelector('[data-image-pick-label]');
    var pickBtn = drop.querySelector('[data-image-pick]');
    var clearBtn = drop.querySelector('[data-image-clear]');
    var clearFlag = drop.querySelector('[data-image-clear-flag]');
    if (!input || !preview) return;

    var EMPTY_ICON = '<svg class="ned-drop-ph"><use href="#ic-image"/></svg>';
    var EMPTY_NAME = 'The card and hero image for this story';
    var EMPTY_NOTE = 'Shown on the dashboard, the news list and the top of the article. ' +
                     'Drag a file in, or choose one.';
    var objectUrl = null;

    function releaseUrl() {
      if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; }
    }

    function readableSize(bytes) {
      if (bytes < 1024) return bytes + ' B';
      if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + ' KB';
      return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }

    function showFile(file) {
      releaseUrl();
      objectUrl = URL.createObjectURL(file);
      preview.innerHTML = '<img alt="">';
      preview.querySelector('img').src = objectUrl;
      drop.classList.add('has-image');
      if (nameEl) nameEl.textContent = file.name;
      if (noteEl) noteEl.textContent = readableSize(file.size) + ' · saved when you save the post';
      if (pickLabel) pickLabel.textContent = 'Replace image';
      if (clearBtn) clearBtn.hidden = false;
      if (clearFlag) clearFlag.value = '';
    }

    function showEmpty() {
      releaseUrl();
      preview.innerHTML = EMPTY_ICON;
      drop.classList.remove('has-image');
      if (nameEl) nameEl.textContent = EMPTY_NAME;
      if (noteEl) noteEl.textContent = EMPTY_NOTE;
      if (pickLabel) pickLabel.textContent = 'Choose image';
      if (clearBtn) clearBtn.hidden = true;
    }

    function accept(file) {
      if (!file) return;
      if (!/^image\//.test(file.type)) {
        if (noteEl) noteEl.textContent = "That file isn't an image — pick a JPG, PNG or WebP.";
        return;
      }
      showFile(file);
    }

    if (pickBtn) pickBtn.addEventListener('click', function () { input.click(); });
    input.addEventListener('change', function () {
      if (input.files && input.files[0]) accept(input.files[0]);
    });

    if (clearBtn) clearBtn.addEventListener('click', function () {
      input.value = '';
      // Tells the view to drop the stored image too, not just the new pick.
      if (clearFlag) clearFlag.value = '1';
      showEmpty();
    });

    ['dragenter', 'dragover'].forEach(function (evt) {
      drop.addEventListener(evt, function (e) {
        e.preventDefault();
        drop.classList.add('is-dragging');
      });
    });
    ['dragleave', 'drop'].forEach(function (evt) {
      drop.addEventListener(evt, function (e) {
        if (evt === 'dragleave' && drop.contains(e.relatedTarget)) return;
        drop.classList.remove('is-dragging');
      });
    });
    drop.addEventListener('drop', function (e) {
      e.preventDefault();
      var files = e.dataTransfer && e.dataTransfer.files;
      if (!files || !files.length) return;
      // The file has to end up on the input itself — the form posts that, not
      // whatever the preview happens to be showing.
      try {
        input.files = files;
      } catch (err) {
        var dt = new DataTransfer();
        dt.items.add(files[0]);
        input.files = dt.files;
      }
      accept(files[0]);
    });
  }

  /* The engine, published for the page editor.
   *
   * gt-page-editor.js drives the same toolbar over a whole page of blocks
   * instead of one story form, and reimplementing 400 lines of selection
   * handling, undo stack and font/size/colour plumbing to do it would leave
   * two copies to keep in step. It loads this file first and builds on what
   * is here. Nothing else should reach for this. */
  window.GTEditor = {
    attachSurface: attachSurface,
    wireToolbar: wireToolbar,
    histories: histories,
    saveSelection: saveSelection,
    restoreSelection: restoreSelection,
    escapeAttr: escapeAttr,
    closeMenus: closeMenus,
    closePopovers: closePopovers,
  };

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-editor]').forEach(wireEditor);
    wireSlugPreview();
    wireSourceRows();
    wireFeaturedImage();
  });
})();

/* ---------------------------------------------------------------------------
 * THE SCHEDULER
 * ---------------------------------------------------------------------------
 * The publish field was a bare `datetime-local`: it tells you the moment you
 * typed and nothing else. What an editor wants to know is the STATE — is this
 * out, is it being held, how long until it goes — and none of that is legible
 * from a date, because "published" here means `is_published AND published_at
 * <= now` and a future date is a schedule rather than a mistake.
 *
 * Three jobs, all of them client-side on purpose:
 *
 *   THE STATE follows the input as it is edited, not only the saved value.
 *   Typing next Friday into a live story should say "will be held until
 *   Friday" before you press Save, because that is the moment you can still
 *   change your mind.
 *
 *   THE COUNTDOWN is computed here rather than rendered server-side. "In 2
 *   days" written into the HTML is wrong the moment the page has been open for
 *   an hour, and the story editor is a page people leave open.
 *
 *   THE PRESETS are built from the browser's own clock. `datetime-local`
 *   carries no timezone and the view reads it in the site's, so a "tomorrow"
 *   worked out on the server would be a different tomorrow for an editor
 *   sitting anywhere else.
 *
 * No cron is involved in any of this. Every reader-facing query asks for
 * `published_at <= now` (admin_panel.models.LivePostManager), so the moment
 * arrives on its own; this is a description of a fact the database already
 * enforces, not a mechanism.
 */
(function () {
  'use strict';

  var box = document.querySelector('[data-sched]');
  if (!box) return;
  var input = box.querySelector('[data-sched-input]');
  var stateEl = box.querySelector('[data-sched-state]');
  var countEl = box.querySelector('[data-sched-count]');
  var hintEl = box.querySelector('[data-sched-hint]');
  if (!input) return;

  var savedState = box.getAttribute('data-state') || 'draft';

  /* `datetime-local` wants "YYYY-MM-DDTHH:MM" in LOCAL time. toISOString is
     UTC and would shift the value by the offset — the classic way a scheduler
     ends up publishing things eleven hours early. */
  function toField(d) {
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
           'T' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function parse() {
    var v = input.value;
    if (!v) return null;
    var d = new Date(v);
    return isNaN(d.getTime()) ? null : d;
  }

  /* Whole units, largest that fits. "in 34 hours" is worse than "in 1 day" for
     a thing being scheduled — nobody plans a publish to the hour three days
     out — and "in 2 minutes" matters when it is 2 minutes. */
  function until(ms) {
    var s = Math.round(ms / 1000);
    if (s < 60) return 'in under a minute';
    var m = Math.round(s / 60);
    if (m < 60) return 'in ' + m + ' minute' + (m === 1 ? '' : 's');
    var h = Math.round(m / 60);
    if (h < 36) return 'in ' + h + ' hour' + (h === 1 ? '' : 's');
    var d = Math.round(h / 24);
    if (d < 14) return 'in ' + d + ' day' + (d === 1 ? '' : 's');
    return 'in ' + Math.round(d / 7) + ' weeks';
  }

  function paint() {
    var when = parse();
    var now = new Date();
    var state, label, hint;

    if (savedState === 'new') {
      state = 'new';
      label = 'Not saved yet';
      hint = when && when > now
        ? 'Saving with Published ticked will hold it until then.'
        : 'A past date backdates the story. A future date holds it back until then.';
    } else if (!when) {
      state = 'draft';
      label = 'No date set';
      hint = 'Pick a moment, or leave it and it publishes when you save.';
    } else if (when > now) {
      state = 'scheduled';
      /* The saved state is what it IS; this is what it WILL be once saved, and
         saying so is the whole point of following the input rather than the
         stored value. */
      label = savedState === 'live' ? 'Will be held back' : 'Scheduled';
      hint = 'It appears on the site on its own at that time. Nothing to run, nobody to be at a keyboard.';
    } else {
      state = savedState === 'draft' || savedState === 'new' ? 'draft' : 'live';
      label = state === 'live' ? 'Live on the site' : 'Draft';
      hint = state === 'live'
        ? 'Readers can see this now.'
        : 'Tick Published to put it on the site.';
    }

    box.setAttribute('data-state', state);
    if (stateEl) stateEl.textContent = label;
    if (hintEl) hintEl.textContent = hint;
    if (countEl) countEl.textContent = (state === 'scheduled' && when) ? until(when - now) : '';
  }

  input.addEventListener('input', paint);
  input.addEventListener('change', paint);

  box.querySelectorAll('[data-sched-set]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var d = new Date();
      var which = btn.getAttribute('data-sched-set');
      if (which === 'evening') {
        d.setHours(18, 0, 0, 0);
        /* Already past six: "tonight" has gone, so it means tomorrow night
           rather than a time in the past nobody asked for. */
        if (d <= new Date()) d.setDate(d.getDate() + 1);
      } else if (which === 'tomorrow') {
        d.setDate(d.getDate() + 1);
        d.setHours(9, 0, 0, 0);
      } else if (which === 'monday') {
        /* The NEXT Monday, never today — pressing "next Monday" on a Monday
           and getting nine o'clock this morning is a date in the past. */
        d.setDate(d.getDate() + ((8 - d.getDay()) % 7 || 7));
        d.setHours(9, 0, 0, 0);
      }
      input.value = toField(d);
      paint();
      input.focus();
    });
  });

  paint();
  /* The countdown goes stale on its own, so it is re-drawn on a slow tick —
     a minute is well inside the resolution anything here is expressed in. */
  setInterval(paint, 60000);
})();
