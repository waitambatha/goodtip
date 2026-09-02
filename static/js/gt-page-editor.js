/* Edit the words on a real page, on the page itself.
 *
 * The client's picture of this was precise: open a page, see everything on it,
 * click Edit, change some wording, press Save, and the site says the new thing
 * from then on. So there is no form — the page IS the form.
 *
 * The blocks were found and tagged server-side (admin_panel/pagetext.py) with
 * data-gte on anything holding words and data-gte-img on every picture. This
 * file turns those into editable surfaces, points one floating toolbar at
 * whichever of them has the caret, and posts back only the ones that changed.
 *
 * The editing engine — selection handling, the undo stack, font/size/colour —
 * is the story editor's, published as window.GTEditor. Two copies of that
 * would drift apart within a release.
 */
(function () {
  'use strict';

  var GTE = window.GTEditor;
  var bar = document.querySelector('.gte-bar');
  if (!GTE || !bar) return;

  var CFG = {
    page: bar.getAttribute('data-gte-page'),
    save: bar.getAttribute('data-gte-save'),
    upload: bar.getAttribute('data-gte-upload'),
    alt: bar.getAttribute('data-gte-alt'),
    csrf: bar.getAttribute('data-gte-csrf'),
  };

  var tools = bar.querySelector('[data-gte-tools]');
  var hint = bar.querySelector('[data-gte-hint]');
  var stateEl = bar.querySelector('[data-gte-state]');
  var startBtn = bar.querySelector('[data-gte-start]');
  var saveBtn = bar.querySelector('[data-gte-save-btn]');
  var saveLabel = bar.querySelector('[data-gte-save-label]');
  var cancelBtn = bar.querySelector('[data-gte-cancel]');
  var resetBtn = bar.querySelector('[data-gte-revert-block]');
  var altBtn = bar.querySelector('[data-gte-alt-mode]');
  var imageInput = document.querySelector('[data-gte-image-input]');

  var editing = false;
  var active = null;          // the block the toolbar is pointed at
  var pendingImage = null;    // the <img> waiting on a file picker
  // While on, clicking a picture edits what it SAYS rather than which file it
  // is. See the button's comment in manage/_page_editor.html for why this is a
  // mode and not a second gesture on the image.
  var altMode = false;
  // What each block said when editing began. Used for three things: knowing
  // which blocks actually changed, sending the original alongside the edit so
  // the manage page can show the before and after, and Discard.
  var originals = new Map();
  // Blocks the admin asked to put back to the template's own wording. Held
  // until Save, so Discard still means discard.
  var reverts = new Set();
  // Blocks that have already been given a surface. Editing can be switched on
  // again after a save, and attachSurface hands back the existing history
  // rather than making a second one — so without this the toolbar listener
  // would be subscribed again on every round.
  var wired = new Set();

  var blocks = Array.prototype.slice.call(document.querySelectorAll('[data-gte]'));
  var images = Array.prototype.slice.call(document.querySelectorAll('[data-gte-img]'));

  /* ---- the toolbar follows the caret ---------------------------------- */

  var syncToolbar = GTE.wireToolbar(tools, function () { return active; }, {
    onChange: function () { markDirty(); },
  });

  function setActive(el) {
    if (active === el) return;
    if (active) active.classList.remove('gte-on');
    active = el;
    if (active) active.classList.add('gte-on');
    if (resetBtn) resetBtn.disabled = !active;
    syncToolbar();
  }

  /* ---- turning editing on and off -------------------------------------- */

  function startEditing() {
    if (editing) return;
    editing = true;
    document.body.classList.add('gte-editing');

    blocks.forEach(function (el) {
      originals.set(el, el.innerHTML);
      el.setAttribute('contenteditable', 'true');
      // spellcheck is off by default on most of these, because the page never
      // expected to be typed into; this is prose, so it wants it on.
      el.setAttribute('spellcheck', 'true');
      // onChange repaints the Save button's count; the history's own listener
      // repaints undo/redo the moment a step lands, rather than at the next
      // click or caret move.
      var history = GTE.attachSurface(el, { onChange: markDirty });
      if (!wired.has(el)) {
        wired.add(el);
        history.onChange(syncToolbar);
      }
    });

    images.forEach(function (img) { img.classList.add('gte-img'); });

    startBtn.hidden = true;
    saveBtn.hidden = false;
    cancelBtn.hidden = false;
    tools.hidden = false;
    if (hint) hint.hidden = false;
    setState('Editing', 'is-editing');
    syncBarHeight();
    markDirty();

    if (blocks.length) blocks[0].focus();
  }

  function stopEditing() {
    editing = false;
    document.body.classList.remove('gte-editing');
    blocks.forEach(function (el) {
      el.removeAttribute('contenteditable');
      el.classList.remove('gte-on');
    });
    images.forEach(function (img) { img.classList.remove('gte-img'); });
    setActive(null);
    startBtn.hidden = false;
    saveBtn.hidden = true;
    cancelBtn.hidden = true;
    tools.hidden = true;
    if (hint) hint.hidden = true;
    reverts.clear();
    syncBarHeight();
  }

  function discard() {
    // Straight back to what the server sent, which is the only version we can
    // be certain is right — the page has been typed into, and undoing block by
    // block would leave whatever was already saved in some half state.
    blocks.forEach(function (el) {
      if (originals.has(el)) el.innerHTML = originals.get(el);
    });
    stopEditing();
    setState('Viewing', '');
  }

  /* Publish the bar's real height so the CSS can push the page — and the
   * page's own sticky nav — clear of it. Measured rather than assumed: the
   * bar grows when the toolbar and hint appear, and again when it wraps on a
   * narrow window. */
  var lastBarHeight = -1;
  function syncBarHeight() {
    var h = bar.offsetHeight;
    if (h === lastBarHeight) return;
    lastBarHeight = h;
    document.documentElement.style.setProperty('--gte-h', h + 'px');
  }

  function setState(text, cls) {
    if (!stateEl) return;
    stateEl.textContent = text;
    stateEl.className = 'gte-state ' + (cls || '');
  }

  /* ---- what has actually changed --------------------------------------- */

  function changedBlocks() {
    var out = [];
    blocks.forEach(function (el) {
      var key = el.getAttribute('data-gte');
      if (reverts.has(key)) {
        out.push({ key: key, revert: true });
        return;
      }
      var before = originals.get(el);
      if (before === undefined || el.innerHTML === before) return;
      out.push({ key: key, html: el.innerHTML, original: before });
    });
    return out;
  }

  function markDirty() {
    if (!editing || !saveLabel) return;
    var n = changedBlocks().length;
    saveLabel.textContent = n ? ('Save ' + n + ' change' + (n === 1 ? '' : 's')) : 'Save changes';
    saveBtn.disabled = !n;
  }

  /* ---- saving ---------------------------------------------------------- */

  function save() {
    var payload = changedBlocks();
    if (!payload.length) return;

    saveBtn.disabled = true;
    setState('Saving…', 'is-editing');

    fetch(CFG.save, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CFG.csrf },
      body: JSON.stringify({ page: CFG.page, blocks: payload }),
    }).then(function (r) {
      if (!r.ok) throw new Error('save failed');
      return r.json();
    }).then(function () {
      // A reset is the one change the page cannot show by itself: the block is
      // displaying the edit that was just deleted, and what should be there
      // now is the template's own wording, which only the server has. Fetch
      // the page again rather than leave a block showing something untrue.
      if (payload.some(function (b) { return b.revert; })) {
        window.removeEventListener('beforeunload', warnOnLeave);
        window.location.reload();
        return;
      }
      // Otherwise the page is already showing the new wording — it is what was
      // just typed — so there is nothing to re-render. Reset the baseline so
      // the next edit is measured from here.
      blocks.forEach(function (el) { originals.set(el, el.innerHTML); });
      stopEditing();
      setState('Saved — this is now live', 'is-saved');
    }).catch(function () {
      saveBtn.disabled = false;
      setState("Couldn't save — try again", 'is-error');
    });
  }

  /* ---- pictures --------------------------------------------------------- */

  function pickImage(e) {
    if (!editing) return;
    e.preventDefault();
    e.stopPropagation();
    if (altMode) {
      describeImage(e.currentTarget);
      return;
    }
    pendingImage = e.currentTarget;
    if (imageInput) imageInput.click();
  }

  /* Alt text without touching the file.
   *
   * Most photographs on the site do not need replacing — their descriptions
   * came from whoever wrote the template, and correcting one used to mean a
   * code change or a pointless re-upload. Saved on its own, immediately, for
   * the same reason a picture is: there is no local state to discard. */
  function describeImage(img) {
    var before = img.getAttribute('alt') || '';
    var next = window.prompt(
      'What does this picture show?\n' +
      'This is what a screen reader reads out and what a search engine sees.\n' +
      'Leave it empty if the picture is purely decorative.',
      before
    );
    if (next === null) return;
    next = next.trim();
    img.setAttribute('alt', next);
    setState('Saving description…', 'is-editing');

    var data = new FormData();
    data.append('page', CFG.page);
    data.append('key', img.getAttribute('data-gte-img'));
    data.append('original', before);
    data.append('alt', next);

    fetch(CFG.alt, {
      method: 'POST',
      headers: { 'X-CSRFToken': CFG.csrf },
      body: data,
    }).then(function (r) {
      if (!r.ok) throw new Error('save failed');
      setState('Description saved', 'is-saved');
    }).catch(function () {
      img.setAttribute('alt', before);
      setState("Couldn't save that description", 'is-error');
    });
  }

  if (imageInput) imageInput.addEventListener('change', function () {
    var file = imageInput.files && imageInput.files[0];
    var img = pendingImage;
    pendingImage = null;
    imageInput.value = '';
    if (!file || !img) return;

    // Show it straight away from a local URL: the upload is a round trip and
    // a picture that does not change for a second reads as a dead click.
    var preview = URL.createObjectURL(file);
    var before = img.getAttribute('src');
    img.src = preview;
    setState('Uploading…', 'is-editing');

    /* ALT TEXT COMES WITH THE PICTURE.
     *
     * A swapped photograph used to inherit the alt attribute of the one it
     * replaced, so every replaced picture on the site was described as
     * whatever the previous picture showed — which reads as correct to a
     * screen reader and is not. Asked once, here, with the existing
     * description prefilled so leaving it alone is a keypress. */
    var alt = window.prompt(
      'Describe this picture for screen readers and search engines.\n' +
      'Leave it empty if the picture is purely decorative.',
      img.getAttribute('alt') || ''
    );
    if (alt !== null) img.setAttribute('alt', alt.trim());

    var data = new FormData();
    data.append('page', CFG.page);
    data.append('key', img.getAttribute('data-gte-img'));
    data.append('original', before || '');
    data.append('alt', alt === null ? (img.getAttribute('alt') || '') : alt.trim());
    data.append('file', file);

    fetch(CFG.upload, {
      method: 'POST',
      headers: { 'X-CSRFToken': CFG.csrf },
      body: data,
    }).then(function (r) {
      if (!r.ok) throw new Error('upload failed');
      return r.json();
    }).then(function (json) {
      URL.revokeObjectURL(preview);
      if (json.url) img.src = json.url;
      // Images save on upload rather than waiting for the Save button: the
      // file is already on the server by then, and a picture that reverts on
      // Discard while its file sits in storage is a lie either way.
      setState('Picture saved', 'is-saved');
    }).catch(function () {
      URL.revokeObjectURL(preview);
      if (before) img.src = before;
      setState("Couldn't upload that picture", 'is-error');
    });
  });

  /* ---- wiring ----------------------------------------------------------- */

  /* Bound once, at load, rather than inside startEditing — editing can be
   * turned on again after a save, and re-binding there would stack a fresh
   * copy of every listener on every block each time round. They are inert
   * while `editing` is false. */
  blocks.forEach(function (el) {
    el.addEventListener('focus', function () { if (editing) setActive(el); });
    el.addEventListener('mouseup', function () { if (editing) setActive(el); });
  });
  images.forEach(function (img) { img.addEventListener('click', pickImage); });

  if (altBtn) altBtn.addEventListener('click', function () {
    altMode = !altMode;
    altBtn.classList.toggle('is-on', altMode);
    altBtn.setAttribute('aria-pressed', altMode ? 'true' : 'false');
    // The pictures themselves say which mode they are in, because the button
    // is up in the bar and the click happens down the page.
    document.body.classList.toggle('gte-alt-mode', altMode);
    setState(
      altMode ? 'Click a picture to describe it' : 'Editing',
      'is-editing'
    );
  });

  startBtn.addEventListener('click', startEditing);
  saveBtn.addEventListener('click', save);
  cancelBtn.addEventListener('click', discard);

  syncBarHeight();
  window.addEventListener('resize', syncBarHeight);
  // The bar's own text changes width as it changes state ("Save 3 changes"),
  // which can wrap it to a second row on a narrow window.
  if (window.ResizeObserver) new ResizeObserver(syncBarHeight).observe(bar);

  if (resetBtn) resetBtn.addEventListener('mousedown', function (e) {
    e.preventDefault();
    if (!active) return;
    var key = active.getAttribute('data-gte');
    reverts.add(key);
    // Show the reset immediately. This is the wording the server sent, which
    // for a block with a saved edit is that edit — so the block visibly stops
    // being special only after Save, which is when it actually does.
    active.innerHTML = originals.get(active);
    markDirty();
  });

  // Leaving with unsaved wording on screen is a real loss — it is prose
  // somebody just wrote — so the browser gets to ask. Named, because the
  // deliberate reload after a reset has to be able to take it back off.
  function warnOnLeave(e) {
    if (editing && changedBlocks().length) {
      e.preventDefault();
      e.returnValue = '';
    }
  }
  window.addEventListener('beforeunload', warnOnLeave);

  // A link click in edit mode almost always means "I meant to edit that", not
  // "take me away" — and navigating loses the work in progress.
  document.addEventListener('click', function (e) {
    if (!editing) return;
    var link = e.target.closest('a');
    if (!link || bar.contains(link)) return;
    e.preventDefault();
  }, true);
})();
