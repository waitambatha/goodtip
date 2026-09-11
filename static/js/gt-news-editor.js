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
      // A slideshow's pictures are described in its own manager.
      if (img.closest('[data-slides]')) return;
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

  /* ---- Slideshows ------------------------------------------------------
   *
   * "Under paragraph one I want to be able to put a max of 10 images, but it
   * will show as one and auto slide ... and not only images but also a video,
   * less than 45 seconds ... 10 slides max."
   *
   * A slideshow is markup in the body, not a separate record:
   *
   *   <figure class="gt-slides" data-slides contenteditable="false">
   *     <div class="gs-track"><div class="gs-slide">…</div>…</div>
   *   </figure>
   *
   * so it saves, redirects, previews and gates exactly as the rest of the
   * story does, and it sits wherever in the story the author put it. The
   * reader's gt-slides.js turns it into one-at-a-time; here it is a strip of
   * thumbnails that opens this manager when pressed.
   *
   * Files upload the moment they are chosen, so what is written into the
   * story is only ever the address of a stored file. The limits are checked
   * here to save a wasted upload, and again by the server, which is the
   * check that counts (admin_panel.views.news_upload_image).
   */
  var SLIDES_MAX = 10;
  var VIDEO_MAX_S = 45;
  var IMAGE_MAX_BYTES = 10 * 1024 * 1024;
  var VIDEO_MAX_BYTES = 80 * 1024 * 1024;
  var VIDEO_RE = /\.(mp4|mov|m4v|webm)$/i;

  /* How long a video runs, read by the browser from the file on disk before
     any of it is uploaded. null when the browser cannot tell — the server
     reads MP4 and MOV itself, so that is not a way past the limit. */
  function videoLength(file) {
    return new Promise(function (resolve) {
      var v = document.createElement('video');
      var url = URL.createObjectURL(file);
      var done = function (secs) { URL.revokeObjectURL(url); resolve(secs); };
      v.preload = 'metadata';
      v.onloadedmetadata = function () { done(isFinite(v.duration) ? v.duration : null); };
      v.onerror = function () { done(null); };
      v.src = url;
    });
  }

  function mmss(secs) {
    secs = Math.round(secs || 0);
    return Math.floor(secs / 60) + ':' + ('0' + (secs % 60)).slice(-2);
  }

  function wireSlides(editor, ctrl, getSurface, commit, after) {
    var dlg = editor.querySelector('[data-slides-dialog]');
    var input = editor.querySelector('[data-slides-input]');
    var uploadUrl = editor.getAttribute('data-upload-url');
    if (!dlg || !input || !uploadUrl || typeof dlg.showModal !== 'function') {
      ctrl.hidden = true;
      return;
    }
    var list = dlg.querySelector('[data-slides-list]');
    var empty = dlg.querySelector('[data-slides-empty]');
    var countEl = dlg.querySelector('[data-slides-count]');
    var msg = dlg.querySelector('[data-slides-msg]');
    var addBtn = dlg.querySelector('[data-slides-add]');
    var applyBtn = dlg.querySelector('[data-slides-apply]');
    var delBtn = dlg.querySelector('[data-slides-delete]');
    var cancelBtn = dlg.querySelector('[data-slides-cancel]');
    var editing = null;   // the <figure> being changed, or null for a new one
    var pending = 0;

    function items() { return Array.prototype.slice.call(list.children); }
    function say(text) { msg.textContent = text || ''; }

    function refresh() {
      var rows = items();
      var n = rows.length;
      empty.hidden = n > 0;
      countEl.textContent = n + ' of ' + SLIDES_MAX;
      addBtn.disabled = n >= SLIDES_MAX;
      // Not while anything is still uploading: a slide written into the
      // story before its file is stored would point at nothing.
      applyBtn.disabled = n === 0 || pending > 0;
      applyBtn.textContent = pending > 0 ? 'Uploading…'
        : (editing ? 'Update slideshow' : 'Insert slideshow');
      rows.forEach(function (li, i) {
        li.querySelector('[data-up]').disabled = i === 0;
        li.querySelector('[data-down]').disabled = i === n - 1;
      });
    }

    function row(kind, src, alt, secs) {
      var li = document.createElement('li');
      li.className = 'nsd-item';
      li.setAttribute('data-kind', kind);
      if (src) li.setAttribute('data-src', src);
      var media = kind === 'video'
        ? '<video muted playsinline preload="metadata" src="' + escapeAttr(src || '') + '"></video>' +
          '<b class="nsd-badge">Video' + (secs ? ' · ' + mmss(secs) : '') + '</b>'
        : '<img alt="" src="' + escapeAttr(src || '') + '">';
      li.innerHTML =
        '<span class="nsd-thumb">' + media + '<i class="nsd-spin" aria-hidden="true"></i></span>' +
        '<input type="text" class="nsd-alt" maxlength="200">' +
        '<span class="nsd-acts">' +
          '<button type="button" data-up title="Move earlier" aria-label="Move earlier">&uarr;</button>' +
          '<button type="button" data-down title="Move later" aria-label="Move later">&darr;</button>' +
          '<button type="button" data-drop title="Remove" aria-label="Remove">&times;</button>' +
        '</span>';
      var altBox = li.querySelector('.nsd-alt');
      altBox.placeholder = (kind === 'video' ? 'What this video shows' : 'What this picture shows') +
                           ' (for screen readers)';
      altBox.value = alt || '';
      return li;
    }

    function open(fig) {
      editing = fig || null;
      list.innerHTML = '';
      say('');
      if (fig) {
        fig.querySelectorAll('.gs-slide').forEach(function (slide) {
          var v = slide.querySelector('video');
          var im = slide.querySelector('img');
          if (v) list.appendChild(row('video', v.getAttribute('src'), v.getAttribute('aria-label')));
          else if (im) list.appendChild(row('image', im.getAttribute('src'), im.getAttribute('alt')));
        });
      }
      delBtn.hidden = !fig;
      refresh();
      dlg.showModal();
    }

    function upload(file, li) {
      var data = new FormData();
      data.append('file', file);
      pending++;
      li.classList.add('is-loading');
      refresh();
      return fetch(uploadUrl, {
        method: 'POST',
        headers: { 'X-CSRFToken': csrfToken(editor.closest('form') || document) },
        body: data,
      }).then(function (r) {
        // nginx refuses an oversized body with its own HTML page, before
        // Django ever sees it — so there is no JSON to read.
        if (r.status === 413) throw new Error(file.name + ' is bigger than the server will accept.');
        return r.json().then(function (json) {
          if (!r.ok || !json.url) throw new Error(json.error || ("Couldn't upload " + file.name + '.'));
          return json;
        });
      }).then(function (json) {
        li.setAttribute('data-src', json.url);
        var m = li.querySelector('img, video');
        if (m) {
          if (m.src.indexOf('blob:') === 0) URL.revokeObjectURL(m.src);
          m.src = json.url;
        }
      }).catch(function (err) {
        li.remove();
        say(err && err.message ? err.message : ("Couldn't upload " + file.name + '.'));
      }).then(function () {
        pending--;
        li.classList.remove('is-loading');
        refresh();
      });
    }

    function addFiles(files) {
      say('');
      var chosen = Array.prototype.slice.call(files);
      var room = SLIDES_MAX - items().length;
      if (chosen.length > room) {
        say('A slideshow holds ' + SLIDES_MAX + '. The last ' + (chosen.length - Math.max(0, room)) +
            ' you chose ' + (chosen.length - room === 1 ? 'was' : 'were') + ' left out.');
        chosen = chosen.slice(0, Math.max(0, room));
      }
      chosen.forEach(function (file) {
        var isVideo = /^video\//.test(file.type) || VIDEO_RE.test(file.name);
        var isImage = !isVideo && /^image\//.test(file.type);
        if (!isVideo && !isImage) { say(file.name + " isn't a picture or a video."); return; }
        if (isImage && file.size > IMAGE_MAX_BYTES) { say(file.name + ' is over 10 MB.'); return; }
        if (isVideo && file.size > VIDEO_MAX_BYTES) { say(file.name + ' is over 80 MB.'); return; }
        // Shown straight away from the file on disk; swapped for the stored
        // copy when the upload lands.
        var li = row(isVideo ? 'video' : 'image', URL.createObjectURL(file), '');
        li.removeAttribute('data-src');
        list.appendChild(li);
        refresh();
        if (!isVideo) { upload(file, li); return; }
        pending++;
        refresh();
        videoLength(file).then(function (secs) {
          pending--;
          if (secs != null && secs > VIDEO_MAX_S) {
            li.remove();
            refresh();
            say(file.name + ' runs ' + Math.round(secs) + ' seconds. Slideshow videos have to be ' +
                VIDEO_MAX_S + ' seconds or less.');
            return;
          }
          var badge = li.querySelector('.nsd-badge');
          if (badge && secs != null) badge.textContent = 'Video · ' + mmss(secs);
          upload(file, li);
        });
      });
    }

    function markup() {
      var slides = items().filter(function (li) { return li.getAttribute('data-src'); }).map(function (li) {
        var src = escapeAttr(li.getAttribute('data-src'));
        var alt = escapeAttr(li.querySelector('.nsd-alt').value.trim());
        return li.getAttribute('data-kind') === 'video'
          ? '<div class="gs-slide" data-kind="video"><video src="' + src + '" muted playsinline preload="metadata"' +
            (alt ? ' aria-label="' + alt + '"' : '') + '></video></div>'
          : '<div class="gs-slide" data-kind="image"><img src="' + src + '" alt="' + alt + '"></div>';
      });
      if (!slides.length) return '';
      return '<figure class="gt-slides" data-slides contenteditable="false"><div class="gs-track">' +
             slides.join('') + '</div></figure>';
    }

    /* The top-level block the caret is in — a slideshow goes AFTER the
       paragraph the author is in ("under paragraph one"), never inside it:
       a <figure> inside a <p> is not valid, and the browser would split the
       paragraph around it wherever it liked. */
    function blockAtCaret(surface) {
      var sel = window.getSelection();
      if (!sel || !sel.rangeCount) return null;
      var range = sel.getRangeAt(0);
      var node = range.startContainer;
      if (!inSurface(surface, node)) return null;
      if (node === surface) return surface.childNodes[Math.max(0, range.startOffset - 1)] || null;
      while (node.parentNode && node.parentNode !== surface) node = node.parentNode;
      return node.parentNode === surface ? node : null;
    }

    function place(html) {
      var surface = getSurface();
      if (!surface) return;
      commit();
      var tmp = document.createElement('div');
      tmp.innerHTML = html;
      var fig = tmp.firstElementChild;
      if (editing && surface.contains(editing)) {
        if (fig) editing.parentNode.replaceChild(fig, editing);
        else editing.parentNode.removeChild(editing);
      } else if (fig) {
        restoreSelection(surface);
        var block = blockAtCaret(surface);
        if (block) surface.insertBefore(fig, block.nextSibling);
        else surface.appendChild(fig);
        // Somewhere to carry on typing, if it landed at the very end.
        if (!fig.nextElementSibling) {
          var para = document.createElement('p');
          para.innerHTML = '<br>';
          surface.appendChild(para);
        }
      }
      after();
      commit();
    }

    ctrl.addEventListener('mousedown', function (e) {
      e.preventDefault();
      var surface = getSurface();
      if (surface) saveSelection(surface);
    });
    ctrl.addEventListener('click', function () { open(null); });
    addBtn.addEventListener('click', function () { input.click(); });
    input.addEventListener('change', function () {
      if (input.files && input.files.length) addFiles(input.files);
      input.value = '';
    });
    list.addEventListener('click', function (e) {
      var li = e.target.closest('.nsd-item');
      if (!li) return;
      if (e.target.closest('[data-up]') && li.previousElementSibling) list.insertBefore(li, li.previousElementSibling);
      else if (e.target.closest('[data-down]') && li.nextElementSibling) list.insertBefore(li.nextElementSibling, li);
      else if (e.target.closest('[data-drop]')) li.remove();
      else return;
      refresh();
    });
    ['dragenter', 'dragover'].forEach(function (evt) {
      dlg.addEventListener(evt, function (e) { e.preventDefault(); });
    });
    dlg.addEventListener('drop', function (e) {
      e.preventDefault();
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
    });
    applyBtn.addEventListener('click', function () {
      var html = markup();
      if (html) place(html);
      dlg.close();
    });
    delBtn.addEventListener('click', function () {
      if (editing) place('');
      dlg.close();
    });
    cancelBtn.addEventListener('click', function () { dlg.close(); });
    dlg.addEventListener('close', function () { editing = null; });

    // Press a slideshow already in the story to change it.
    var surface = getSurface();
    if (surface) surface.addEventListener('click', function (e) {
      var fig = e.target.closest && e.target.closest('[data-slides]');
      if (fig && surface.contains(fig)) {
        e.preventDefault();
        open(fig);
      }
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

      if (action === 'slides') {
        wireSlides(editor, ctrl, surfaceNow, commit, after);
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
    // Before the first history snapshot, so undo never lands on a version of
    // a slideshow that could be typed into.
    surface.querySelectorAll('[data-slides]').forEach(function (f) {
      f.setAttribute('contenteditable', 'false');
    });

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

  /* FEATURED MEDIA — up to three pictures and three videos, in order.
   *
   * Replaces the single featured-image drop zone (Sep 2026, client: "the place
   * where we have featured image, can it take 3 images and 3 videos as well,
   * that will also be changing").
   *
   * Each file uploads the moment it is chosen, to the same endpoint the body
   * and the slideshows use (admin_panel.views.news_upload_image, ?slot=featured)
   * — which is where the size, type and 45-second limits already live — and
   * the manager keeps a list of what came back. That list is written into the
   * hidden `featured_media` field as JSON on every change, and the view turns
   * it into NewsMedia rows on save. Nothing is attached to the story until the
   * story is saved: closing the page leaves an orphan file, not a changed post.
   *
   * THE CAP IS PER KIND, three and three, because the two do different jobs
   * and a story with four photographs and no clip is a normal thing to want.
   * The server enforces it again; this is the version that explains itself.
   *
   * ORDER MATTERS — the first item is what the card and the story open on —
   * so items can be dragged, or moved with their arrow buttons for anyone
   * not using a mouse. */
  function wireFeaturedSet() {
    var box = document.querySelector('[data-featured]');
    if (!box) return;
    var list = box.querySelector('[data-featured-list]');
    var empty = box.querySelector('[data-featured-empty]');
    var out = box.querySelector('[data-featured-json]');
    var msg = box.querySelector('[data-featured-msg]');
    var url = box.getAttribute('data-upload-url');
    var MAX = parseInt(box.getAttribute('data-max'), 10) || 3;
    var LABEL = { image: 'picture', video: 'video' };

    var items = [];
    try {
      var seed = document.getElementById('featuredData');
      items = seed ? (JSON.parse(seed.textContent) || []) : [];
    } catch (e) { items = []; }

    function csrf() {
      var f = document.querySelector('[name=csrfmiddlewaretoken]');
      if (f) return f.value;
      var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
      return m ? decodeURIComponent(m[1]) : '';
    }
    function count(kind) {
      return items.filter(function (i) { return i.kind === kind && !i.pending; }).length +
             items.filter(function (i) { return i.kind === kind && i.pending; }).length;
    }
    function say(text, bad) {
      if (!msg) return;
      msg.textContent = text || '';
      msg.classList.toggle('is-bad', !!bad);
    }
    function sync() {
      out.value = JSON.stringify(items.filter(function (i) { return !i.pending; })
        .map(function (i) { return { kind: i.kind, path: i.path, alt: i.alt || '' }; }));
      ['image', 'video'].forEach(function (kind) {
        var n = count(kind);
        var c = box.querySelector('[data-featured-count="' + kind + '"]');
        if (c) c.textContent = n + ' / ' + MAX;
        var b = box.querySelector('[data-featured-add="' + kind + '"]');
        if (b) b.disabled = n >= MAX;
      });
      if (empty) empty.hidden = items.length > 0;
      box.classList.toggle('has-items', items.length > 0);
    }

    function move(from, to) {
      if (to < 0 || to >= items.length || from === to) return;
      var it = items.splice(from, 1)[0];
      items.splice(to, 0, it);
      render();
    }

    function render() {
      list.innerHTML = '';
      items.forEach(function (it, n) {
        var li = document.createElement('li');
        li.className = 'ned-fm-item' + (it.pending ? ' is-loading' : '') + (n === 0 ? ' is-lead' : '');
        li.draggable = !it.pending;
        li.dataset.index = n;

        var thumb = document.createElement('div');
        thumb.className = 'ned-fm-thumb';
        if (it.kind === 'video') {
          var v = document.createElement('video');
          v.src = it.url || it.preview || '';
          v.muted = true; v.playsInline = true; v.preload = 'metadata';
          thumb.appendChild(v);
        } else {
          var img = document.createElement('img');
          img.src = it.url || it.preview || '';
          img.alt = '';
          thumb.appendChild(img);
        }
        var badge = document.createElement('span');
        badge.className = 'ned-fm-badge';
        badge.textContent = n === 0 ? 'Leads' : String(n + 1);
        thumb.appendChild(badge);
        var kind = document.createElement('span');
        kind.className = 'ned-fm-kind';
        kind.textContent = it.kind === 'video' ? 'Video' : 'Picture';
        thumb.appendChild(kind);
        if (it.pending) {
          var spin = document.createElement('span');
          spin.className = 'ned-fm-spin';
          spin.setAttribute('aria-label', 'Uploading');
          thumb.appendChild(spin);
        }
        li.appendChild(thumb);

        var alt = document.createElement('input');
        alt.type = 'text';
        alt.className = 'ned-fm-alt';
        alt.maxLength = 200;
        alt.value = it.alt || '';
        alt.placeholder = it.kind === 'video' ? 'What happens in this clip' : 'What this picture shows';
        alt.setAttribute('aria-label', 'Description of ' + LABEL[it.kind] + ' ' + (n + 1));
        alt.disabled = !!it.pending;
        alt.addEventListener('input', function () { it.alt = alt.value; sync(); });
        li.appendChild(alt);

        var acts = document.createElement('div');
        acts.className = 'ned-fm-acts';
        [['←', 'Move earlier', function () { move(n, n - 1); }, n === 0],
         ['→', 'Move later', function () { move(n, n + 1); }, n === items.length - 1],
         ['×', 'Remove', function () { items.splice(n, 1); render(); say(''); }, false]
        ].forEach(function (spec) {
          var b = document.createElement('button');
          b.type = 'button';
          b.textContent = spec[0];
          b.title = spec[1];
          b.setAttribute('aria-label', spec[1] + ' — ' + LABEL[it.kind] + ' ' + (n + 1));
          b.disabled = spec[3] || !!it.pending;
          if (spec[0] === '×') b.className = 'is-drop';
          b.addEventListener('click', spec[2]);
          acts.appendChild(b);
        });
        li.appendChild(acts);
        list.appendChild(li);
      });
      sync();
    }

    /* Drag to reorder. The item being dragged is remembered by its index;
       dropping on another item puts it in that one's place. */
    var dragFrom = null;
    list.addEventListener('dragstart', function (e) {
      var li = e.target.closest('.ned-fm-item');
      if (!li) return;
      dragFrom = parseInt(li.dataset.index, 10);
      li.classList.add('is-dragging');
      try { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', String(dragFrom)); } catch (err) {}
    });
    list.addEventListener('dragover', function (e) {
      if (dragFrom === null) return;
      e.preventDefault();
      var li = e.target.closest('.ned-fm-item');
      list.querySelectorAll('.is-over').forEach(function (x) { x.classList.remove('is-over'); });
      if (li) li.classList.add('is-over');
    });
    list.addEventListener('drop', function (e) {
      if (dragFrom === null) return;
      e.preventDefault();
      e.stopPropagation();
      var li = e.target.closest('.ned-fm-item');
      var to = li ? parseInt(li.dataset.index, 10) : items.length - 1;
      var from = dragFrom;
      dragFrom = null;
      move(from, to);
    });
    list.addEventListener('dragend', function () {
      dragFrom = null;
      list.querySelectorAll('.is-dragging, .is-over').forEach(function (x) {
        x.classList.remove('is-dragging', 'is-over');
      });
    });

    function kindOf(file) {
      if (/^image\//.test(file.type)) return 'image';
      if (/^video\//.test(file.type) || /\.(mp4|mov|m4v|webm)$/i.test(file.name)) return 'video';
      return null;
    }

    function upload(file) {
      var kind = kindOf(file);
      if (!kind) { say('"' + file.name + '" is not a picture or a video.', true); return; }
      if (count(kind) >= MAX) {
        say('That is already ' + MAX + ' ' + LABEL[kind] + 's — remove one to add another.', true);
        return;
      }
      var it = { kind: kind, pending: true, alt: '', preview: URL.createObjectURL(file) };
      items.push(it);
      render();
      var data = new FormData();
      data.append('file', file);
      fetch(url, { method: 'POST', body: data, credentials: 'same-origin',
                   headers: { 'X-CSRFToken': csrf() } })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
        .then(function (res) {
          if (!res.ok || !res.j.url || !res.j.path) throw new Error(res.j.error || 'That upload did not go through.');
          it.pending = false;
          it.url = res.j.url;
          it.path = res.j.path;
          // The server decides what a file is: a .mov picked under "Add
          // pictures" is still a video, and counts as one.
          it.kind = res.j.kind === 'video' ? 'video' : 'image';
          if (count(it.kind) > MAX) {
            items.splice(items.indexOf(it), 1);
            say('That is already ' + MAX + ' ' + LABEL[it.kind] + 's — that one was left out.', true);
          } else {
            say('');
          }
        })
        .catch(function (err) {
          var at = items.indexOf(it);
          if (at >= 0) items.splice(at, 1);
          say(err.message || 'That upload did not go through.', true);
        })
        .then(function () {
          if (it.preview) { URL.revokeObjectURL(it.preview); it.preview = null; }
          render();
        });
    }

    box.querySelectorAll('[data-featured-add]').forEach(function (btn) {
      var kind = btn.getAttribute('data-featured-add');
      var input = box.querySelector('[data-featured-input="' + kind + '"]');
      btn.addEventListener('click', function () { if (input) input.click(); });
      if (input) input.addEventListener('change', function () {
        Array.prototype.slice.call(input.files || []).forEach(upload);
        input.value = '';
      });
    });

    /* Files dropped anywhere on the box — not on an item, which is a reorder. */
    ['dragenter', 'dragover'].forEach(function (evt) {
      box.addEventListener(evt, function (e) {
        if (dragFrom !== null) return;
        if (!e.dataTransfer || Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') < 0) return;
        e.preventDefault();
        box.classList.add('is-dropping');
      });
    });
    ['dragleave', 'drop'].forEach(function (evt) {
      box.addEventListener(evt, function (e) {
        if (evt === 'dragleave' && box.contains(e.relatedTarget)) return;
        box.classList.remove('is-dropping');
      });
    });
    box.addEventListener('drop', function (e) {
      if (dragFrom !== null) return;
      var files = e.dataTransfer && e.dataTransfer.files;
      if (!files || !files.length) return;
      e.preventDefault();
      Array.prototype.slice.call(files).forEach(upload);
    });

    render();
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
    wireFeaturedSet();
  });
})();

/* ---------------------------------------------------------------------------
 * WHEN IT GOES OUT — the scheduler, as a button and a calendar
 * ---------------------------------------------------------------------------
 * The client, Sep 2026: "I do not like how that automated publishing where it
 * will auto post. Let it be a button, then when I click, a nice time and
 * calendar UI comes in."
 *
 * The date box that used to sit open on every story is gone. What an editor
 * sees now is the state in words — "Goes out when you save", "Scheduled",
 * "Live on the site" — and one button. Pressing it opens a month grid and a
 * clock, with the four times anybody actually picks as presets; choosing a
 * moment writes it into the hidden `published_at` field and says, in words,
 * when that is and how long until it.
 *
 * BLANK MEANS "WHEN YOU SAVE". On a new story the field is empty until a date
 * is picked, and the view uses the moment of saving (_parse_published_at's
 * fallback). "Go out when I save" empties it again. On an existing story the
 * same button is "Keep the original date" and puts the stored date back, so
 * fixing a typo does not quietly re-date a story as today's news.
 *
 * LOCAL TIME THROUGHOUT. The field carries "YYYY-MM-DDTHH:MM" with no zone and
 * the view reads it in the site's timezone; toISOString would be UTC and shift
 * the time by the offset — the classic way a scheduler publishes something
 * eleven hours early.
 *
 * No cron. Every reader-facing query asks for `published_at <= now`
 * (admin_panel.models.LivePostManager), so the moment arrives on its own.
 */
(function () {
  'use strict';

  var box = document.querySelector('[data-pub]');
  if (!box) return;
  var input = box.querySelector('[data-pub-input]');
  var stateEl = box.querySelector('[data-pub-state]');
  var whenEl = box.querySelector('[data-pub-when]');
  var openBtn = box.querySelector('[data-pub-open]');
  var openLabel = box.querySelector('[data-pub-open-label]');
  var resetBtn = box.querySelector('[data-pub-reset]');
  var pop = box.querySelector('[data-pub-pop]');
  if (!input || !openBtn || !pop) return;

  var saved = box.getAttribute('data-saved') || 'new';
  var original = input.value;
  var MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                'August', 'September', 'October', 'November', 'December'];
  var DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

  function p2(n) { return (n < 10 ? '0' : '') + n; }
  function toField(d) {
    return d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate()) +
           'T' + p2(d.getHours()) + ':' + p2(d.getMinutes());
  }
  function parse(v) {
    if (!v) return null;
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(v);
    if (!m) return null;
    return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  }
  function sameDay(a, b) {
    return a && b && a.getFullYear() === b.getFullYear() &&
           a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }
  function clock(d) {
    var h = d.getHours(), m = d.getMinutes();
    return ((h % 12) || 12) + ':' + p2(m) + (h < 12 ? ' am' : ' pm');
  }
  function long(d) {
    return DAYS[(d.getDay() + 6) % 7] + ' ' + d.getDate() + ' ' + MONTHS[d.getMonth()].slice(0, 3) +
           ' ' + d.getFullYear() + ', ' + clock(d);
  }
  /* Whole units, largest that fits: nobody plans a publish to the hour three
     days out, and "in 2 minutes" matters when it is two minutes. */
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

  /* ---- the summary above the button ---------------------------------- */
  function paint() {
    var when = parse(input.value);
    var now = new Date();
    var state, label, line = '';
    if (!when) {
      state = saved === 'new' ? 'now' : saved;
      label = saved === 'new' ? 'Goes out when you save' : 'No date';
    } else if (when > now) {
      state = 'scheduled';
      label = 'Scheduled';
      line = long(when) + ' · ' + until(when - now);
    } else if (saved === 'new') {
      state = 'back';
      label = 'Backdated';
      line = 'Filed under ' + long(when);
    } else {
      state = saved === 'draft' ? 'draft' : 'live';
      label = state === 'live' ? 'Live on the site' : 'Draft';
      line = (state === 'live' ? 'Published ' : 'Dated ') + long(when);
    }
    box.setAttribute('data-state', state);
    if (stateEl) stateEl.textContent = label;
    if (whenEl) whenEl.textContent = line;

    var changed = input.value !== original;
    if (resetBtn) {
      resetBtn.hidden = !changed;
      resetBtn.textContent = saved === 'new' ? 'Go out when I save' : 'Keep the original date';
    }
    if (openLabel) {
      openLabel.textContent = when ? 'Change date & time' : 'Schedule for later';
    }
  }

  /* ---- the picker ------------------------------------------------------ */
  var view, picked;

  function presets() {
    var now = new Date();
    var evening = new Date(now); evening.setHours(18, 0, 0, 0);
    // Already past six, "tonight" has gone: it means tomorrow night rather
    // than a time in the past nobody asked for.
    if (evening <= now) evening.setDate(evening.getDate() + 1);
    var tomorrow = new Date(now); tomorrow.setDate(tomorrow.getDate() + 1); tomorrow.setHours(9, 0, 0, 0);
    // The NEXT Monday, never today.
    var monday = new Date(now); monday.setDate(monday.getDate() + ((8 - monday.getDay()) % 7 || 7));
    monday.setHours(9, 0, 0, 0);
    var hour = new Date(now); hour.setMinutes(0, 0, 0); hour.setHours(hour.getHours() + 1);
    return [['In an hour', hour], [evening.getDate() === now.getDate() ? 'Tonight 6pm' : 'Tomorrow 6pm', evening],
            ['Tomorrow 9am', tomorrow], ['Next Mon 9am', monday]];
  }

  function build() {
    var now = new Date();
    var first = new Date(view.getFullYear(), view.getMonth(), 1);
    var lead = (first.getDay() + 6) % 7;             // Monday-first grid
    var days = new Date(view.getFullYear(), view.getMonth() + 1, 0).getDate();

    var html = '';
    html += '<div class="pp-presets" role="group" aria-label="Quick picks">';
    presets().forEach(function (pr, n) {
      html += '<button type="button" data-pp-preset="' + n + '">' + pr[0] + '</button>';
    });
    html += '</div>';

    html += '<div class="pp-cal">';
    html += '<div class="pp-month"><button type="button" class="pp-nav" data-pp-nav="-1" aria-label="Previous month">&#8249;</button>' +
            '<b aria-live="polite">' + MONTHS[view.getMonth()] + ' ' + view.getFullYear() + '</b>' +
            '<button type="button" class="pp-nav" data-pp-nav="1" aria-label="Next month">&#8250;</button></div>';
    html += '<div class="pp-grid" role="grid">';
    DAYS.forEach(function (d) { html += '<span class="pp-dow" aria-hidden="true">' + d.slice(0, 2) + '</span>'; });
    for (var i = 0; i < lead; i++) html += '<span></span>';
    for (var day = 1; day <= days; day++) {
      var d = new Date(view.getFullYear(), view.getMonth(), day);
      var cls = 'pp-day';
      if (sameDay(d, now)) cls += ' is-today';
      if (sameDay(d, picked)) cls += ' is-picked';
      if (d < new Date(now.getFullYear(), now.getMonth(), now.getDate())) cls += ' is-past';
      html += '<button type="button" class="' + cls + '" data-pp-day="' + day + '"' +
              ' aria-label="' + DAYS[(d.getDay() + 6) % 7] + ' ' + day + ' ' + MONTHS[d.getMonth()] + '"' +
              (sameDay(d, picked) ? ' aria-pressed="true"' : '') + '>' + day + '</button>';
    }
    html += '</div></div>';

    var h = picked.getHours(), m = picked.getMinutes();
    html += '<div class="pp-time"><span class="pp-k">Time</span>' +
            '<select data-pp-h aria-label="Hour">';
    for (var hh = 1; hh <= 12; hh++) html += '<option' + (((h % 12) || 12) === hh ? ' selected' : '') + '>' + hh + '</option>';
    html += '</select><b>:</b><select data-pp-m aria-label="Minutes">';
    for (var mm = 0; mm < 60; mm += 5) html += '<option value="' + mm + '"' + (Math.floor(m / 5) * 5 === mm ? ' selected' : '') + '>' + p2(mm) + '</option>';
    html += '</select><div class="pp-ampm" role="group" aria-label="Morning or afternoon">' +
            '<button type="button" data-pp-ampm="am"' + (h < 12 ? ' class="on" aria-pressed="true"' : '') + '>am</button>' +
            '<button type="button" data-pp-ampm="pm"' + (h >= 12 ? ' class="on" aria-pressed="true"' : '') + '>pm</button></div></div>';

    html += '<p class="pp-sum" data-pp-sum></p>';
    html += '<div class="pp-foot"><button type="button" class="pp-cancel" data-pp-cancel>Cancel</button>' +
            '<button type="button" class="pp-ok" data-pp-ok>Set this time</button></div>';
    pop.innerHTML = html;
    summary();
  }

  function summary() {
    var el = pop.querySelector('[data-pp-sum]');
    if (!el) return;
    var now = new Date();
    el.textContent = long(picked) + ' · ' +
      (picked > now ? until(picked - now) : 'in the past — the story will be backdated');
    el.classList.toggle('is-past', picked <= now);
  }

  function readTime() {
    var h = parseInt(pop.querySelector('[data-pp-h]').value, 10) % 12;
    var pm = pop.querySelector('[data-pp-ampm="pm"]').classList.contains('on');
    picked.setHours(h + (pm ? 12 : 0), parseInt(pop.querySelector('[data-pp-m]').value, 10), 0, 0);
    summary();
  }

  function open() {
    var cur = parse(input.value);
    if (!cur) {
      // Nothing chosen yet: start on the next whole hour, which is the
      // likeliest thing to want and never in the past.
      cur = new Date(); cur.setMinutes(0, 0, 0); cur.setHours(cur.getHours() + 1);
    }
    picked = new Date(cur);
    view = new Date(cur.getFullYear(), cur.getMonth(), 1);
    build();
    pop.hidden = false;
    box.classList.add('is-open');
    openBtn.setAttribute('aria-expanded', 'true');
    var focus = pop.querySelector('.pp-day.is-picked') || pop.querySelector('.pp-day');
    if (focus) focus.focus();
  }
  function close(refocus) {
    pop.hidden = true;
    box.classList.remove('is-open');
    openBtn.setAttribute('aria-expanded', 'false');
    if (refocus) openBtn.focus();
  }

  openBtn.addEventListener('click', function () { pop.hidden ? open() : close(true); });
  if (resetBtn) resetBtn.addEventListener('click', function () {
    input.value = original;
    close(false);
    paint();
    openBtn.focus();
  });

  pop.addEventListener('click', function (e) {
    var t = e.target.closest('button');
    if (!t) return;
    if (t.hasAttribute('data-pp-nav')) {
      view.setMonth(view.getMonth() + parseInt(t.getAttribute('data-pp-nav'), 10));
      build();
      var back = pop.querySelector('[data-pp-nav="' + t.getAttribute('data-pp-nav') + '"]');
      if (back) back.focus();
    } else if (t.hasAttribute('data-pp-day')) {
      picked.setFullYear(view.getFullYear(), view.getMonth(), parseInt(t.getAttribute('data-pp-day'), 10));
      build();
      var sel = pop.querySelector('.pp-day.is-picked');
      if (sel) sel.focus();
    } else if (t.hasAttribute('data-pp-preset')) {
      picked = new Date(presets()[parseInt(t.getAttribute('data-pp-preset'), 10)][1]);
      view = new Date(picked.getFullYear(), picked.getMonth(), 1);
      build();
      var ok = pop.querySelector('[data-pp-ok]');
      if (ok) ok.focus();
    } else if (t.hasAttribute('data-pp-ampm')) {
      pop.querySelectorAll('[data-pp-ampm]').forEach(function (b) {
        var on = b === t;
        b.classList.toggle('on', on);
        if (on) b.setAttribute('aria-pressed', 'true'); else b.removeAttribute('aria-pressed');
      });
      readTime();
    } else if (t.hasAttribute('data-pp-cancel')) {
      close(true);
    } else if (t.hasAttribute('data-pp-ok')) {
      readTime();
      input.value = toField(picked);
      close(true);
      paint();
    }
  });
  pop.addEventListener('change', function (e) {
    if (e.target.matches('[data-pp-h], [data-pp-m]')) readTime();
  });
  /* Arrow keys move through the month grid — a calendar you cannot walk with
     the keyboard is a calendar a keyboard user cannot use at all. */
  pop.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { e.preventDefault(); close(true); return; }
    var day = e.target.closest('[data-pp-day]');
    if (!day) return;
    var step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[e.key];
    if (!step) return;
    e.preventDefault();
    picked.setDate(picked.getDate() + step);
    view = new Date(picked.getFullYear(), picked.getMonth(), 1);
    build();
    var sel = pop.querySelector('.pp-day.is-picked');
    if (sel) sel.focus();
  });
  document.addEventListener('click', function (e) {
    if (!pop.hidden && !box.contains(e.target)) close(false);
  });

  paint();
  /* The countdown goes stale on its own; a minute is well inside the
     resolution anything here is expressed in. */
  setInterval(paint, 60000);
})();
