/* Rich-text editor for the News & blog story form.
 *
 * The old form was a plain textarea inside a sidebar aside, and the client
 * flagged both problems at once: the box felt too small for writing an actual
 * article, and there was no way to format anything (headings, bold, colour,
 * alignment, images). This drives three contenteditable surfaces (headline,
 * teaser, body) plus their toolbars, the font-size stepper and the featured
 * image drop zone — no editor library, just execCommand, since the only
 * audience is the superuser story form.
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

  function applyFontSize(surface, px) {
    restoreSelection(surface);
    // execCommand only speaks in the seven legacy <font size> buckets, so ask
    // for the top one and swap every tag it just made for a span carrying the
    // exact pixel value the author typed.
    document.execCommand('fontSize', false, '7');
    surface.querySelectorAll('font[size="7"]').forEach(function (el) {
      var span = document.createElement('span');
      span.style.fontSize = px + 'px';
      span.innerHTML = el.innerHTML;
      el.replaceWith(span);
    });
    syncHidden(surface);
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

  /* The size stepper: −  [ 17 ]  +
   *
   * Returns the number input so the caller can keep it in step with the
   * selection. Every control here cancels mousedown, because the surface loses
   * its text selection the moment focus moves and there would be nothing left
   * to resize. */
  function wireSizeControl(editor, surface) {
    var box = editor.querySelector('.ned-size');
    if (!box) return null;
    var num = box.querySelector('[data-cmd="fontSizePx"]');
    if (!num) return null;

    var fallback = parseInt(box.getAttribute('data-size-default'), 10) || 16;
    var min = parseInt(num.getAttribute('min'), 10) || 8;
    var max = parseInt(num.getAttribute('max'), 10) || 120;

    function value() {
      var v = parseInt(num.value, 10);
      return isNaN(v) ? fallback : Math.min(max, Math.max(min, v));
    }

    function apply(px) {
      num.value = px;
      applyFontSize(surface, px);
    }

    num.addEventListener('change', function () { apply(value()); });
    num.addEventListener('keydown', function (e) {
      // Enter inside a number box would otherwise submit the whole story.
      if (e.key === 'Enter') { e.preventDefault(); apply(value()); }
    });

    box.querySelectorAll('[data-size-step]').forEach(function (btn) {
      btn.addEventListener('mousedown', function (e) {
        e.preventDefault();
        var step = parseInt(btn.getAttribute('data-size-step'), 10) || 0;
        apply(Math.min(max, Math.max(min, value() + step)));
      });
    });

    return num;
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
    if (input) input.value = surface.innerHTML;
  }

  function closePopovers(editor) {
    editor.querySelectorAll('.ned-pop.open').forEach(function (p) { p.classList.remove('open'); });
  }

  function wireEditor(editor) {
    var surface = editor.querySelector('.ned-surface');
    if (!surface) return;

    syncHidden(surface);
    surface.addEventListener('input', function () { syncHidden(surface); });
    surface.addEventListener('blur', function () { syncHidden(surface); });

    var sizeBox = wireSizeControl(editor, surface);
    document.addEventListener('selectionchange', function () {
      saveSelection(surface);
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
      if (!editor.contains(e.target)) closePopovers(editor);
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
