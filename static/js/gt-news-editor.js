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
      return;
    }

    // Chrome and Firefox default to <font> tags here, but a page that has
    // turned CSS styling on gets spans marked xxx-large instead; catch both.
    try { document.execCommand('styleWithCSS', false, false); } catch (e) {}
    document.execCommand('fontSize', false, '7');

    var legacy = prop === 'fontSize' ? 'size' : 'face';
    var made = surface.querySelectorAll('font[size="7"], span[style*="xxx-large"]');
    if (!made.length) { syncHidden(surface); return; }

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
  }

  function applyFontSize(surface, px) {
    applyInlineStyle(surface, 'fontSize', px + 'px');
  }

  function applyFontFamily(surface, stack) {
    applyInlineStyle(surface, 'fontFamily', stack);
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
  function wireSizeControl(editor, surface) {
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
      applyFontSize(surface, px);
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
  function wireFontControl(editor, surface) {
    var box = editor.querySelector('.ned-font');
    if (!box) return null;
    var toggle = box.querySelector('[data-font-toggle]');
    var label = box.querySelector('[data-font-label]');
    var menu = box.querySelector('[data-font-menu]');
    if (!toggle || !menu || !label) return null;

    function stackOf(item) { return item.getAttribute('data-font'); }

    function current() {
      var sel = window.getSelection();
      if (!sel || !sel.rangeCount || !inSurface(surface, sel.anchorNode)) return null;
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
      applyFontFamily(surface, stackOf(item));
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
      restoreSelection(surface);
      document.execCommand(
        'insertHTML', false,
        '<img src="' + escapeAttr(json.url) + '" alt="">'
      );
      syncHidden(surface);
    }).catch(function () {
      window.alert("Couldn't upload that image, try again.");
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

  function wireEditor(editor) {
    var surface = editor.querySelector('.ned-surface');
    if (!surface) return;

    syncHidden(surface);
    surface.addEventListener('input', function () { syncHidden(surface); });
    surface.addEventListener('blur', function () { syncHidden(surface); });

    var sizeBox = wireSizeControl(editor, surface);
    var syncFontLabel = wireFontControl(editor, surface);
    document.addEventListener('selectionchange', function () {
      saveSelection(surface);
      if (syncFontLabel) syncFontLabel();
      // Don't fight the author while they are typing a number into the box.
      if (!sizeBox || document.activeElement === sizeBox) return;
      var px = currentFontSize(surface);
      if (px) sizeBox.value = px;
    });

    editor.querySelectorAll('[data-cmd]').forEach(function (ctrl) {
      var cmd = ctrl.getAttribute('data-cmd');

      if (ctrl.tagName === 'BUTTON') {
        // preventDefault on mousedown keeps the contenteditable's selection
        // alive — a click alone would blur the surface first.
        ctrl.addEventListener('mousedown', function (e) {
          e.preventDefault();
          restoreSelection(surface);
          document.execCommand(cmd, false, ctrl.getAttribute('data-value') || undefined);
          syncHidden(surface);
        });
        return;
      }

      // The size box is a number input, not a command select — wireSizeControl
      // owns it, so it must not fall through to the generic handlers below.
      if (cmd === 'fontSizePx') return;

      if (ctrl.tagName === 'SELECT') {
        ctrl.addEventListener('change', function () {
          if (!ctrl.value) return;
          restoreSelection(surface);
          document.execCommand(cmd, false, ctrl.value);
          syncHidden(surface);
          ctrl.selectedIndex = 0;
        });
        return;
      }

      if (ctrl.type === 'color') {
        ctrl.addEventListener('input', function () {
          restoreSelection(surface);
          document.execCommand(cmd, false, ctrl.value);
          syncHidden(surface);
        });
      }
    });

    editor.querySelectorAll('[data-action]').forEach(function (ctrl) {
      var action = ctrl.getAttribute('data-action');

      if (action === 'link') {
        var pop = editor.querySelector('.ned-pop[data-pop="link"]');
        ctrl.addEventListener('mousedown', function (e) {
          e.preventDefault();
          saveSelection(surface);
          closePopovers(editor);
          if (pop) {
            pop.classList.add('open');
            var input = pop.querySelector('input');
            if (input) { input.value = ''; input.focus(); }
          }
        });
        if (pop) {
          var applyBtn = pop.querySelector('[data-pop-apply]');
          var input = pop.querySelector('input');
          var apply = function () {
            insertLink(surface, input.value);
            pop.classList.remove('open');
          };
          if (applyBtn) applyBtn.addEventListener('click', apply);
          if (input) input.addEventListener('keydown', function (e) {
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
          saveSelection(surface);
        });
        ctrl.addEventListener('click', function () {
          if (fileInput) fileInput.click();
        });
        if (fileInput) fileInput.addEventListener('change', function () {
          if (fileInput.files && fileInput.files[0]) {
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

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-editor]').forEach(wireEditor);
    wireSlugPreview();
    wireSourceRows();
    wireFeaturedImage();
  });
})();
