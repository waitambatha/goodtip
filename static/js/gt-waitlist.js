/*
 * The waiting-list pop-ups and the dropdowns that go in them (client, 25 Sep 2026).
 *
 *   1. the dialog: join -> verify -> done, and signin; opened by anything marked
 *      data-wl-open="join|signin" or by the address hash (#join, #signin)
 *   2. forms: posted as JSON to the /coming-soon/ endpoints, errors shown in place
 *   3. the six code boxes (type, paste, backspace, auto-submit)
 *   4. dropdowns: every select[data-dd] becomes a picker you can type into.
 *      The <select> stays underneath and is what gets submitted.
 *   5. the member page: the launch countdown and the click-to-play reel
 *
 * The dialog is inert until this runs; the page carries a <noscript> form.
 */
(function () {
  'use strict';

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var root = document.documentElement;
  var reduced = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- 4. dropdowns ---------- */
  var scrim = document.createElement('div'); scrim.className = 'wl-dd-scrim'; scrim.hidden = true;
  var panel = document.createElement('div'); panel.className = 'wl-dd'; panel.hidden = true;
  panel.innerHTML = '<div class="wl-dd-head"><input type="text" class="wl-dd-q" autocomplete="off" spellcheck="false" ' +
    'placeholder="Type to search&hellip;" aria-label="Filter the options"></div>' +
    '<ul class="wl-dd-list" role="listbox"></ul><p class="wl-dd-none" hidden>Nothing matches that.</p>';
  document.body.appendChild(scrim); document.body.appendChild(panel);
  var q = $('.wl-dd-q', panel), list = $('.wl-dd-list', panel), none = $('.wl-dd-none', panel);
  var cur = null;              // { sel, trig, wrap, items }
  var sheet = function () { return window.matchMedia('(max-width: 640px)').matches; };

  function labelOf(sel) { var o = sel.options[sel.selectedIndex]; return o && o.value ? o.textContent : ''; }
  function paint(d) {
    var t = labelOf(d.sel);
    $('.tr-dd-v', d.trig).textContent = t || 'Select one';
    d.wrap.classList.toggle('has-val', !!t);
  }
  function place() {
    if (!cur) return;
    if (sheet()) { panel.style.cssText = ''; return; }
    var r = cur.trig.getBoundingClientRect();
    var room = window.innerHeight - r.bottom - 14, above = r.top - 14;
    var up = room < 230 && above > room;
    var h = Math.min(320, Math.max(150, up ? above : room));
    panel.style.left = r.left + 'px'; panel.style.width = r.width + 'px';
    panel.style.maxHeight = h + 'px';
    if (up) { panel.style.top = 'auto'; panel.style.bottom = (window.innerHeight - r.top + 6) + 'px'; }
    else { panel.style.bottom = 'auto'; panel.style.top = (r.bottom + 6) + 'px'; }
    panel.classList.toggle('up', up);
  }
  function active(li, scroll) {
    $$('.on', list).forEach(function (x) { x.classList.remove('on'); x.removeAttribute('aria-selected'); });
    if (!li) return;
    li.classList.add('on'); li.setAttribute('aria-selected', 'true');
    q.setAttribute('aria-activedescendant', li.id);
    if (scroll) li.scrollIntoView({ block: 'nearest' });
  }
  function visible() { return $$('li:not([hidden])', list); }
  function filter(text) {
    var t = (text || '').trim().toLowerCase(), n = 0;
    $$('li', list).forEach(function (li) {
      var hit = !t || li.textContent.toLowerCase().indexOf(t) !== -1;
      li.hidden = !hit; if (hit) n++;
    });
    none.hidden = n > 0;
    active(visible()[0] || null, true);
  }
  function openDD(d, seed) {
    if (cur) closeDD(false);
    cur = d;
    list.innerHTML = '';
    $$('option', d.sel).forEach(function (o, i) {
      var li = document.createElement('li');
      li.setAttribute('role', 'option'); li.id = 'wl-dd-o' + i; li.dataset.v = o.value;
      li.textContent = o.value ? o.textContent : 'Skip this one';
      if (!o.value) li.classList.add('wl-dd-skip');
      if (o.selected) li.classList.add('is-sel');
      list.appendChild(li);
    });
    d.trig.setAttribute('aria-expanded', 'true'); d.wrap.classList.add('is-open');
    panel.hidden = false; scrim.hidden = !sheet(); panel.classList.toggle('sheet', sheet());
    place();
    q.value = seed || '';
    filter(q.value);
    if (!seed) active($('.is-sel', list) || visible()[0], true);
    q.focus({ preventScroll: true });
  }
  function closeDD(refocus) {
    if (!cur) return;
    var d = cur; cur = null;
    panel.hidden = true; scrim.hidden = true;
    d.trig.setAttribute('aria-expanded', 'false'); d.wrap.classList.remove('is-open');
    if (refocus !== false) d.trig.focus({ preventScroll: true });
  }
  function choose(li) {
    if (!li || !cur) return;
    var d = cur; d.sel.value = li.dataset.v;
    d.sel.dispatchEvent(new Event('change', { bubbles: true }));
    paint(d); closeDD(true);
  }
  q.addEventListener('input', function () { filter(q.value); });
  q.addEventListener('keydown', function (e) {
    var v = visible(), i = v.indexOf($('.on', list));
    if (e.key === 'ArrowDown') { e.preventDefault(); active(v[Math.min(v.length - 1, i + 1)], true); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); active(v[Math.max(0, i - 1)], true); }
    else if (e.key === 'Home') { e.preventDefault(); active(v[0], true); }
    else if (e.key === 'End') { e.preventDefault(); active(v[v.length - 1], true); }
    else if (e.key === 'Enter') { e.preventDefault(); choose($('.on', list)); }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeDD(true); }
    else if (e.key === 'Tab') closeDD(false);
  });
  list.addEventListener('mousemove', function (e) { var li = e.target.closest('li'); if (li && !li.classList.contains('on')) active(li, false); });
  list.addEventListener('click', function (e) { choose(e.target.closest('li')); });
  scrim.addEventListener('click', function () { closeDD(true); });
  window.addEventListener('resize', function () { if (cur) { panel.classList.toggle('sheet', sheet()); scrim.hidden = !sheet(); place(); } });
  document.addEventListener('scroll', function (e) {
    if (cur && !panel.contains(e.target)) { if (sheet()) return; place(); }
  }, true);
  document.addEventListener('mousedown', function (e) {
    if (cur && !panel.contains(e.target) && !cur.wrap.contains(e.target)) closeDD(false);
  });

  function enhance(sel) {
    if (sel.dataset.ddDone) return; sel.dataset.ddDone = '1';
    var wrap = sel.closest('.tr-f') || sel.parentNode;
    var lab = wrap.querySelector('label');
    var trig = document.createElement('button');
    trig.type = 'button'; trig.className = 'tr-dd';
    trig.setAttribute('role', 'combobox'); trig.setAttribute('aria-haspopup', 'listbox'); trig.setAttribute('aria-expanded', 'false');
    trig.innerHTML = '<span class="tr-dd-v"></span>';
    if (lab) { if (!lab.id) lab.id = sel.id + '-l'; trig.setAttribute('aria-labelledby', lab.id); lab.removeAttribute('for'); }
    sel.parentNode.insertBefore(trig, sel);
    sel.classList.add('wl-native'); sel.tabIndex = -1; sel.setAttribute('aria-hidden', 'true');
    var d = { sel: sel, trig: trig, wrap: wrap };
    paint(d);
    trig.addEventListener('click', function () { if (cur && cur.trig === trig) closeDD(true); else openDD(d); });
    trig.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDD(d); }
      // Typing straight onto the field opens it already filtering.
      else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); openDD(d, e.key); }
    });
    var f = sel.form;
    if (f && !f.dataset.ddReset) { f.dataset.ddReset = '1'; f.addEventListener('reset', function () { setTimeout(function () { $$('select[data-dd]', f).forEach(function (s) { var t = s.closest('.tr-f'); paint({ sel: s, trig: $('.tr-dd', t), wrap: t }); }); }); }); }
  }
  $$('select[data-dd]').forEach(enhance);

  /* ---------- 1. the dialog ---------- */
  var wl = $('#wl');
  var STEPS = ['join', 'verify', 'done', 'signin'];
  var step = null, opener = null, email = '', timer = null;

  function endpoint(form) { return form.getAttribute('action'); }
  function post(form, extra) {
    var fd = new FormData(form);
    if (extra) Object.keys(extra).forEach(function (k) { fd.set(k, extra[k]); });
    return fetch(endpoint(form), {
      method: 'POST', body: fd, credentials: 'same-origin',
      headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' }
    }).then(function (r) { return r.json(); }).catch(function () {
      return { ok: false, error: 'We couldn’t reach the server. Check your connection and try again.' };
    });
  }
  function showError(section, msg) {
    var p = $('[data-wl-error]', section);
    if (!p) return;
    p.textContent = msg || ''; p.hidden = !msg;
    if (msg) { p.classList.remove('shake'); void p.offsetWidth; p.classList.add('shake'); }
  }
  function busy(form, on) {
    var b = $('[data-wl-submit]', form); if (!b) return;
    b.disabled = on; b.classList.toggle('is-busy', on);
  }

  function show(name) {
    STEPS.forEach(function (s) {
      var el = $('[data-wl-step="' + s + '"]', wl); if (!el) return;
      el.hidden = s !== name;
      if (s !== name) showError(el, '');
    });
    step = name;
    wl.dataset.step = name;
    var card = $('.wl-card', wl); if (card) $('.wl-main', wl).scrollTop = 0;
    var focus = { join: '#wl-name', signin: '#wl-si-email', verify: '[data-wl-otp] input', done: '.wl-done .tr-submit' }[name];
    setTimeout(function () { var f = $(focus, wl); if (f) f.focus({ preventScroll: true }); }, reduced ? 0 : 60);
  }
  function openWL(name, from) {
    if (!wl) return;
    if (STEPS.indexOf(name) === -1) name = 'join';
    if (wl.hidden) {
      opener = from || document.activeElement;
      wl.hidden = false; void wl.offsetWidth; wl.classList.add('is-open');
      root.classList.add('wl-lock');
    }
    show(name);
  }
  function closeWL() {
    if (!wl || wl.hidden) return;
    closeDD(false);
    wl.classList.remove('is-open'); root.classList.remove('wl-lock');
    var done = function () { wl.hidden = true; };
    if (reduced) done(); else setTimeout(done, 220);
    clearInterval(timer);
    if (/^#(join|signin|verify)$/.test(location.hash)) history.replaceState(history.state, '', location.pathname + location.search);
    if (opener && opener.focus && document.contains(opener)) opener.focus({ preventScroll: true });
    opener = null;
  }
  function fromHash() {
    var h = location.hash.replace('#', '');
    if (h === 'join' || h === 'signin') openWL(h); else if (h !== 'verify' && wl && !wl.hidden && step !== 'done') closeWL();
  }

  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('[data-wl-open]');
    if (a && wl) {
      e.preventDefault();
      var name = a.getAttribute('data-wl-open');
      history.replaceState(history.state, '', '#' + name);
      // Lets gt-trailer.js put away a showcase that is open behind us.
      window.dispatchEvent(new Event('hashchange'));
      openWL(name, a);
      return;
    }
    if (e.target.closest && e.target.closest('[data-wl-close]')) closeWL();
  });
  document.addEventListener('keydown', function (e) {
    if (!wl || wl.hidden) return;
    if (e.key === 'Escape') { if (!cur) closeWL(); return; }
    if (e.key !== 'Tab' || cur) return;
    var f = $$('a[href], button:not([disabled]), input:not([type=hidden]):not([disabled]), [tabindex="0"]', $('.wl-card', wl))
      .filter(function (x) { return x.offsetParent !== null && !x.closest('[hidden]'); });
    if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  window.addEventListener('hashchange', fromHash);
  window.addEventListener('wl:hash', fromHash);

  if (wl) {
    /* ---------- 2. forms ---------- */
    $$('[data-wl-eye]', wl).forEach(function (b) {
      b.addEventListener('click', function () {
        var i = b.parentNode.querySelector('input'), on = i.type === 'password';
        i.type = on ? 'text' : 'password';
        b.setAttribute('aria-pressed', on ? 'true' : 'false'); b.setAttribute('aria-label', on ? 'Hide password' : 'Show password');
      });
    });

    var joinF = $('[data-wl-form="join"]', wl), verF = $('[data-wl-form="verify"]', wl), signF = $('[data-wl-form="signin"]', wl);

    function need(form, section) {
      var bad = $$('input[required]', form).filter(function (i) { return !i.value.trim(); })[0];
      if (bad) { showError(section, 'Fill in ' + (bad.type === 'password' ? 'your password' : bad.type === 'email' ? 'your email address' : 'your name') + ' first.'); bad.focus(); return false; }
      return true;
    }

    joinF.addEventListener('submit', function (e) {
      e.preventDefault();
      var sec = joinF.closest('[data-wl-step]');
      if (!need(joinF, sec)) return;
      if (joinF.password.value.length < 8) { showError(sec, 'Your password needs at least 8 characters.'); joinF.password.focus(); return; }
      showError(sec, ''); busy(joinF, true);
      post(joinF).then(function (r) {
        busy(joinF, false);
        if (!r.ok) return showError(sec, r.error || 'Something went wrong. Try again.');
        email = r.email || joinF.email.value.trim();
        $('[data-wl-email]', wl).textContent = email;
        verF.email.value = email;
        clearOtp(); show('verify'); countdown(30);
        history.replaceState(history.state, '', '#verify');
      });
    });

    /* ---------- 3. the six boxes ---------- */
    var boxes = $$('[data-wl-otp] input', wl);
    function clearOtp() { boxes.forEach(function (b) { b.value = ''; b.classList.remove('bad'); }); }
    function code() { return boxes.map(function (b) { return b.value; }).join(''); }
    function fill(str) {
      var d = (str || '').replace(/\D/g, '').slice(0, 6);
      boxes.forEach(function (b, i) { b.value = d[i] || ''; });
      (boxes[Math.min(d.length, 5)]).focus();
      if (d.length === 6) verF.requestSubmit ? verF.requestSubmit() : verF.dispatchEvent(new Event('submit', { cancelable: true }));
    }
    boxes.forEach(function (b, i) {
      b.addEventListener('input', function () {
        var v = b.value.replace(/\D/g, '');
        if (v.length > 1) return fill(v);            // autofill from the SMS/mail app lands in one box
        b.value = v; b.classList.remove('bad');
        if (v && i < 5) boxes[i + 1].focus();
        if (code().length === 6) fill(code());
      });
      b.addEventListener('keydown', function (e) {
        if (e.key === 'Backspace' && !b.value && i > 0) { boxes[i - 1].value = ''; boxes[i - 1].focus(); e.preventDefault(); }
        else if (e.key === 'ArrowLeft' && i > 0) { boxes[i - 1].focus(); e.preventDefault(); }
        else if (e.key === 'ArrowRight' && i < 5) { boxes[i + 1].focus(); e.preventDefault(); }
      });
      b.addEventListener('paste', function (e) {
        e.preventDefault(); fill((e.clipboardData || window.clipboardData).getData('text'));
      });
      b.addEventListener('focus', function () { b.select(); });
    });

    verF.addEventListener('submit', function (e) {
      e.preventDefault();
      var sec = verF.closest('[data-wl-step]');
      if (code().length < 6) { showError(sec, 'Type all 6 digits of the code.'); return; }
      verF.code.value = code(); showError(sec, ''); busy(verF, true);
      post(verF).then(function (r) {
        busy(verF, false);
        if (!r.ok) { boxes.forEach(function (b) { b.classList.add('bad'); }); boxes[0].focus(); return showError(sec, r.error || 'That code did not work.'); }
        finish(r.next, 'You’re on the list.');
      });
    });

    var resend = $('[data-wl-resend]', wl), wait = $('[data-wl-wait]', wl);
    function countdown(n) {
      clearInterval(timer); resend.disabled = true;
      var t = n; wait.textContent = ' (' + t + 's)';
      timer = setInterval(function () {
        t -= 1;
        if (t <= 0) { clearInterval(timer); resend.disabled = false; wait.textContent = ''; }
        else wait.textContent = ' (' + t + 's)';
      }, 1000);
    }
    resend.addEventListener('click', function () {
      var sec = verF.closest('[data-wl-step]');
      var fd = new FormData(); fd.set('email', email); resend.disabled = true;
      fd.set('csrfmiddlewaretoken', verF.csrfmiddlewaretoken.value);
      fetch(wl.dataset.resend, { method: 'POST', body: fd, credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' } })
        .then(function (r) { return r.json(); }).catch(function () { return { ok: false, error: 'We couldn’t reach the server.' }; })
        .then(function (r) {
          if (!r.ok) { resend.disabled = false; return showError(sec, r.error || 'Could not send another code.'); }
          showError(sec, ''); clearOtp(); boxes[0].focus(); countdown(30);
        });
    });
    $('[data-wl-back]', wl).addEventListener('click', function () { clearInterval(timer); show('join'); history.replaceState(history.state, '', '#join'); });

    signF.addEventListener('submit', function (e) {
      e.preventDefault();
      var sec = signF.closest('[data-wl-step]');
      if (!need(signF, sec)) return;
      showError(sec, ''); busy(signF, true);
      post(signF).then(function (r) {
        busy(signF, false);
        if (!r.ok) return showError(sec, r.error || 'That email and password do not match.');
        finish(r.next, 'Welcome back.');
      });
    });

    function finish(next, heading) {
      $('[data-wl-done-h]', wl).textContent = heading;
      $('.wl-done .tr-submit', wl).setAttribute('href', next || '/coming-soon/me/');
      show('done');
      setTimeout(function () { window.location.href = next || '/coming-soon/me/'; }, reduced ? 400 : 1400);
    }

    fromHash();
  }

  /* ---------- 5. the member page ---------- */
  // Countdown. The server writes the first reading; this only keeps it moving,
  // and stops for good at zero (the page is then left showing the last figures).
  $$('[data-countdown]').forEach(function (box) {
    var end = Date.parse(box.getAttribute('data-countdown'));
    if (isNaN(end)) return;
    var cell = function (k) { return $('[data-cd="' + k + '"]', box); };
    var two = function (n) { return n < 10 ? '0' + n : String(n); };
    var tick = function () {
      var left = Math.max(0, Math.floor((end - Date.now()) / 1000));
      cell('d').textContent = Math.floor(left / 86400);
      cell('h').textContent = two(Math.floor(left % 86400 / 3600));
      cell('m').textContent = two(Math.floor(left % 3600 / 60));
      cell('s').textContent = two(left % 60);
      if (left === 0) { clearInterval(iv); box.classList.add('is-zero'); }
    };
    var iv = setInterval(tick, 1000);
    tick();
  });

  // Theatre. One stage and a strip of chapters. Nothing downloads until the play
  // button or a chapter is tapped; tapping a chapter switches to it and plays it.
  var stage = $('[data-stage]');
  if (stage) {
    var thumbs = $$('.wm-th');
    var img = $('.wm-stage-img', stage), playBtn = $('[data-stage-play]', stage);
    var titleEl = $('[data-stage-t]', stage), blurbEl = $('[data-stage-b]', stage);
    var stop = function () {
      var old = $('.wm-stage-vid', stage);
      if (old) { old.pause(); old.removeAttribute('src'); old.load(); old.remove(); }
      stage.classList.remove('is-playing');
    };
    var play = function () {
      stop();
      var v = document.createElement('video');
      v.className = 'wm-stage-vid'; v.src = stage.getAttribute('data-clip'); v.poster = img.src;
      v.controls = true; v.autoplay = true; v.muted = true; v.loop = true; v.preload = 'auto';
      v.setAttribute('playsinline', ''); v.setAttribute('aria-label', titleEl.textContent);
      stage.appendChild(v); stage.classList.add('is-playing');
      var pr = v.play(); if (pr && pr.catch) pr.catch(function () {});
    };
    var pick = function (th, andPlay) {
      thumbs.forEach(function (t) { t.classList.toggle('is-on', t === th); t.removeAttribute('aria-current'); });
      th.setAttribute('aria-current', 'true');
      stop();
      stage.setAttribute('data-clip', th.getAttribute('data-clip'));
      img.src = th.getAttribute('data-poster');
      titleEl.textContent = th.getAttribute('data-tag');
      blurbEl.textContent = th.getAttribute('data-title');
      playBtn.setAttribute('aria-label', 'Play: ' + th.getAttribute('data-tag'));
      if (andPlay) play();
    };
    playBtn.addEventListener('click', play);
    thumbs.forEach(function (th) { th.addEventListener('click', function () { pick(th, true); }); });
  }
})();
