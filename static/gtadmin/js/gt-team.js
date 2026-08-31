/* The add-an-administrator form.
 *
 * Three jobs, all of them about not letting the form say something the person
 * filling it in did not mean:
 *
 *   - "Needs my approval" only means anything on a capability they actually
 *     hold, so it follows its own row's tick and cannot be left stranded on;
 *   - full access is all-or-nothing, so the per-capability list goes away
 *     rather than sitting there looking like it still decides something;
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
      var key = all.getAttribute('data-group-all');
      var rows = form.querySelectorAll('.ad-caprow[data-group="' + CSS.escape(key) + '"] [data-cap]');
      var on = 0;
      rows.forEach(function (r) { if (r.checked) on++; });
      all.checked = on > 0 && on === rows.length;
      all.indeterminate = on > 0 && on < rows.length;
    });
  }

  function line(text, cls) {
    var p = document.createElement('p');
    p.className = cls || '';
    p.textContent = text;
    return p;
  }

  function render() {
    var full = isFull();
    if (limitedOnly) limitedOnly.hidden = full;
    if (!preview) return;

    preview.innerHTML = '';

    if (full) {
      preview.appendChild(line('Full access.', 'ad-pv-head'));
      preview.appendChild(line(
        'Everything in GoodTip HQ, including adding other administrators and ' +
        'approving their work. Nothing they do will wait for anybody.'
      ));
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
      preview.appendChild(line('Tick something above and it will appear here.', 'gt-muted'));
      return;
    }

    if (direct.length) {
      preview.appendChild(line('They can do this straight away', 'ad-pv-head'));
      preview.appendChild(listOf(direct, 'is-go'));
    }
    if (reviewed.length) {
      preview.appendChild(line('This waits for you to approve it', 'ad-pv-head'));
      preview.appendChild(listOf(reviewed, 'is-wait'));
      preview.appendChild(line(
        'They will be emailed when you have looked, whichever way it goes.',
        'gt-muted'
      ));
    }
  }

  function listOf(names, cls) {
    var ul = document.createElement('ul');
    ul.className = 'ad-pv-list ' + cls;
    names.forEach(function (n) {
      var li = document.createElement('li');
      li.textContent = n;
      ul.appendChild(li);
    });
    return ul;
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
      var key = all.getAttribute('data-group-all');
      form.querySelectorAll('.ad-caprow[data-group="' + CSS.escape(key) + '"] [data-cap]')
        .forEach(function (box) {
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
