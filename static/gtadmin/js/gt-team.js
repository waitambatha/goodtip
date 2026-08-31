/* The add-an-administrator form.
 *
 * Four jobs, all of them about not letting the form say something the person
 * filling it in did not mean:
 *
 *   - "Needs my approval" only means anything on a capability they actually
 *     hold, so it follows its own row's tick and cannot be left stranded on;
 *   - full access is all-or-nothing, so the per-capability list goes away
 *     rather than sitting there looking like it still decides something;
 *   - each area's heading says how many of its rows are on, so a collapsed
 *     group still tells you something;
 *   - the summary at the bottom is generated from the boxes themselves, so it
 *     cannot drift from what is really ticked.
 */
(function () {
  'use strict';

  var form = document.querySelector('[data-team-form]');
  if (!form) return;

  var levelRadios = form.querySelectorAll('[data-level]');
  var limitedOnly = form.querySelector('[data-limited-only]');
  var preview = form.querySelector('[data-preview]');
  var caps = Array.prototype.slice.call(form.querySelectorAll('[data-cap]'));

  function reviewFor(key) {
    return form.querySelector('[data-review="' + CSS.escape(key) + '"]');
  }

  function rowsIn(key) {
    return form.querySelectorAll(
      '.ad-caprow[data-group="' + CSS.escape(key) + '"] [data-cap]'
    );
  }

  function isFull() {
    var on = form.querySelector('[data-level]:checked');
    return !!on && on.value === 'full';
  }

  /* A review box on a capability they do not hold would post a value the
   * server then has to ignore, and worse, it would read on screen as though
   * something were being decided. */
  function syncRow(box) {
    var review = reviewFor(box.getAttribute('data-cap'));
    if (!review) return;
    review.disabled = !box.checked;
    if (!box.checked) review.checked = false;
    review.closest('.ad-capreview').classList.toggle('is-off', !box.checked);
  }

  function syncGroupHeaders() {
    form.querySelectorAll('[data-group-all]').forEach(function (all) {
      var rows = rowsIn(all.getAttribute('data-group-all'));
      var on = 0;
      rows.forEach(function (r) { if (r.checked) on++; });
      all.checked = on > 0 && on === rows.length;
      all.indeterminate = on > 0 && on < rows.length;
    });

    /* The count on the summary bar. Worded rather than "3/4": the heading is
     * read at a glance and a fraction asks to be worked out. */
    form.querySelectorAll('[data-group-count]').forEach(function (tag) {
      var rows = rowsIn(tag.getAttribute('data-group-count'));
      var on = 0;
      rows.forEach(function (r) { if (r.checked) on++; });
      tag.classList.toggle('is-on', on > 0);
      tag.textContent = on === 0 ? 'None'
        : on === rows.length ? 'All ' + on
        : on + ' of ' + rows.length;
    });
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    return node;
  }

  function listOf(names, cls) {
    var ul = el('ul', 'ad-pv-list ' + cls);
    names.forEach(function (n) { ul.appendChild(el('li', '', n)); });
    return ul;
  }

  function empty(message) {
    var wrap = el('div', 'ad-pv-empty');
    wrap.innerHTML =
      '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#ic-check"/></svg>';
    wrap.appendChild(el('p', '', message));
    return wrap;
  }

  function render() {
    var full = isFull();
    if (limitedOnly) limitedOnly.hidden = full;
    if (!preview) return;

    preview.innerHTML = '';
    preview.classList.remove('is-empty');

    if (full) {
      preview.appendChild(el('div', 'ad-pv-head', 'Full access'));
      preview.appendChild(listOf([
        'Everything in GoodTip HQ, including adding other administrators ' +
        'and approving their work.',
        'Nothing they do will wait for anybody.'
      ], 'is-go'));
      return;
    }

    var direct = [], reviewed = [];
    caps.forEach(function (box) {
      if (!box.checked) return;
      var label = box.closest('.ad-capmain').querySelector('b');
      var name = label ? label.firstChild.textContent.trim() : box.value;
      var review = reviewFor(box.value);
      (review && review.checked ? reviewed : direct).push(name);
    });

    if (!direct.length && !reviewed.length) {
      preview.classList.add('is-empty');
      preview.appendChild(empty('Tick something above and it will appear here.'));
      return;
    }

    if (direct.length) {
      preview.appendChild(el('div', 'ad-pv-head', 'They can do this straight away'));
      preview.appendChild(listOf(direct, 'is-go'));
    }
    if (reviewed.length) {
      preview.appendChild(el('div', 'ad-pv-head', 'This waits for you to approve it'));
      preview.appendChild(listOf(reviewed, 'is-wait'));
      preview.appendChild(el(
        'p', 'ad-pv-note',
        'They will be emailed when you have looked, whichever way it goes.'
      ));
    }
  }

  caps.forEach(function (box) {
    syncRow(box);
    box.addEventListener('change', function () {
      syncRow(box);
      syncGroupHeaders();
      render();
    });
  });

  form.querySelectorAll('[data-review]').forEach(function (r) {
    r.addEventListener('change', render);
  });

  form.querySelectorAll('[data-group-all]').forEach(function (all) {
    all.addEventListener('change', function () {
      rowsIn(all.getAttribute('data-group-all')).forEach(function (box) {
        box.checked = all.checked;
        syncRow(box);
      });
      syncGroupHeaders();
      render();
    });
  });

  levelRadios.forEach(function (r) { r.addEventListener('change', render); });

  syncGroupHeaders();
  render();
})();
