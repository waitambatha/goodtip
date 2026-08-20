/* Copy-link button on shared story pages. Facebook/LinkedIn get their own
 * share-intent links (plain <a> tags, no JS needed); this is only for the
 * "copy the URL" option sitting next to them. */
(function () {
  'use strict';

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-copy-link]');
    if (!btn) return;
    if (btn.classList.contains('copied')) return;  // mid-flash, ignore the re-click
    var url = btn.getAttribute('data-copy-link');
    // Write into the label span when there is one. Setting textContent on the
    // button itself would delete the icon sitting beside the words, and it
    // never came back — restoring the old text cannot restore an <svg>.
    var label = btn.querySelector('[data-copy-label]');
    var target = label || btn;
    var done = function () {
      var original = label ? label.textContent : (btn.getAttribute('data-label') || btn.textContent);
      btn.classList.add('copied');
      target.textContent = 'Link copied';
      setTimeout(function () {
        btn.classList.remove('copied');
        target.textContent = original;
      }, 1800);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(url).then(done);
      return;
    }
    var tmp = document.createElement('textarea');
    tmp.value = url;
    tmp.style.position = 'fixed';
    tmp.style.opacity = '0';
    document.body.appendChild(tmp);
    tmp.select();
    document.execCommand('copy');
    document.body.removeChild(tmp);
    done();
  });
})();
