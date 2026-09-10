/* Sharing a story — see partials/_news_share.html.
 *
 * The platform buttons are plain share-intent links and work with no script
 * at all. This adds four things on top:
 *
 *   * the big Share button opens and closes the panel of platforms;
 *   * on a desktop, a platform opens in a pop-up window, so the reader shares
 *     and comes straight back to the story rather than being taken off it;
 *     on a phone the link is followed as it is, which is what lets
 *     WhatsApp or Facebook open in their own app;
 *   * "More…" appears where the device has its own share sheet;
 *   * Copy link, which is also used by the story editor's address bar.
 */
(function () {
  'use strict';

  var coarse = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;

  function panelFor(box) { return box && box.querySelector('[data-share-panel]'); }

  function setOpen(box, open) {
    var panel = panelFor(box);
    var btn = box.querySelector('[data-share-toggle]');
    if (!panel || !btn) return;
    panel.hidden = !open;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      var first = panel.querySelector('a, button:not([hidden])');
      if (first) first.focus({ preventScroll: true });
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-share]').forEach(function (box) {
      // Closed until asked for. Open in the markup, so that with no script
      // the platforms are still there.
      var panel = panelFor(box);
      if (panel) panel.hidden = true;
    });
    if (navigator.share) {
      document.querySelectorAll('[data-share-native]').forEach(function (b) { b.hidden = false; });
    }
  });

  document.addEventListener('click', function (e) {
    var toggle = e.target.closest('[data-share-toggle]');
    if (toggle) {
      var box = toggle.closest('[data-share]');
      setOpen(box, toggle.getAttribute('aria-expanded') !== 'true');
      return;
    }

    var popup = e.target.closest('[data-share-popup]');
    if (popup && !coarse) {
      var w = window.open(popup.href, 'gt-share', 'popup,width=640,height=680');
      // Blocked pop-ups return null — then the link's own target=_blank
      // opens a tab instead, which is the next best thing.
      if (w) {
        e.preventDefault();
        try { w.opener = null; } catch (err) { /* cross-origin already */ }
      }
      return;
    }

    var native = e.target.closest('[data-share-native]');
    if (native && navigator.share) {
      navigator.share({
        title: native.getAttribute('data-share-title') || document.title,
        url: native.getAttribute('data-share-url') || location.href,
      }).catch(function () { /* the reader closed the sheet */ });
      return;
    }

    var btn = e.target.closest('[data-copy-link]');
    if (btn) {
      copyLink(btn);
      return;
    }

    // A press anywhere else puts an open panel away.
    document.querySelectorAll('[data-share]').forEach(function (b) {
      var p = panelFor(b);
      if (p && !p.hidden && !b.contains(e.target)) setOpen(b, false);
    });
  });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    document.querySelectorAll('[data-share]').forEach(function (b) {
      var p = panelFor(b);
      if (p && !p.hidden) {
        setOpen(b, false);
        var t = b.querySelector('[data-share-toggle]');
        if (t) t.focus();
      }
    });
  });

  function copyLink(btn) {
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
  }
})();
