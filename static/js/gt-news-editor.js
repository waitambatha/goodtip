/* Rich-text editor for the News & blog story form.
 *
 * The old form was a plain textarea inside a sidebar aside, and the client
 * flagged both problems at once: the box felt too small for writing an actual
 * article, and there was no way to format anything (headings, bold, colour,
 * alignment, images). This drives two contenteditable surfaces (headline,
 * body) plus their toolbars — no editor library, just execCommand, since the
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

  function applyFontSize(surface, px) {
    restoreSelection(surface);
    document.execCommand('fontSize', false, '7');
    surface.querySelectorAll('font[size="7"]').forEach(function (el) {
      var span = document.createElement('span');
      span.style.fontSize = px + 'px';
      span.innerHTML = el.innerHTML;
      el.replaceWith(span);
    });
    syncHidden(surface);
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
    document.addEventListener('selectionchange', function () { saveSelection(surface); });

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

      if (ctrl.tagName === 'SELECT') {
        ctrl.addEventListener('change', function () {
          if (!ctrl.value) return;
          restoreSelection(surface);
          if (cmd === 'fontSizeCustom') {
            applyFontSize(surface, ctrl.value);
          } else {
            document.execCommand(cmd, false, ctrl.value);
            syncHidden(surface);
          }
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
        var fileInput = editor.querySelector('input[type="file"]');
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

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-editor]').forEach(wireEditor);
    wireSlugPreview();
    wireSourceRows();
  });
})();
