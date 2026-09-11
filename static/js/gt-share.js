/* Sharing a story — see partials/_news_share.html.
 *
 * The platform buttons are plain share-intent links and work with no script
 * at all. This adds four things on top:
 *
 *   * the big Share button wipes the strip of platforms open, left to right;
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

  /* OPEN IS A CLASS, NOT `hidden`.
   *
   * The strip animates from no width to its content's width, and a `hidden`
   * element has no content width to animate to — it would snap open and snap
   * shut. Closed is the CSS default instead (grid-template-columns: 0fr), so
   * there is nothing to hide on load and no flash of a full-width strip
   * before this file runs; .is-open does the opening and the CSS transition
   * does the movement. See .shr-reveal in goodtip.css.
   *
   * The strip stays in the accessibility tree while it is closed, which is
   * why the platforms inside it are made unreachable by the keyboard instead:
   * tabbing into a control that is nought pixels wide and off to one side is
   * the failure that a plain CSS collapse usually ships with. */
  function setOpen(box, open) {
    var panel = panelFor(box);
    var btn = box.querySelector('[data-share-toggle]');
    if (!panel || !btn) return;
    panel.classList.toggle('is-open', open);
    panel.setAttribute('aria-hidden', open ? 'false' : 'true');
    panel.querySelectorAll('a, button').forEach(function (el) {
      if (open) el.removeAttribute('tabindex');
      else el.setAttribute('tabindex', '-1');
    });
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      var first = panel.querySelector('a, button:not([hidden])');
      if (first) first.focus({ preventScroll: true });
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-share]').forEach(function (box) {
      // Closed until asked for. This is also what takes the platforms out of
      // the tab order and marks the strip aria-hidden — the CSS can collapse
      // the width but it cannot do either of those.
      if (panelFor(box)) setOpen(box, false);
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

    // A press anywhere else puts an open strip away.
    document.querySelectorAll('[data-share]').forEach(function (b) {
      var p = panelFor(b);
      if (p && p.classList.contains('is-open') && !b.contains(e.target)) setOpen(b, false);
    });
  });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    document.querySelectorAll('[data-share]').forEach(function (b) {
      var p = panelFor(b);
      if (p && p.classList.contains('is-open')) {
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
