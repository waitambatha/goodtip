/* The GX screens' tabs (templates/admin/hub/gx_base.html): one part of a
   sheet at a time. A tab is a button with data-gx-tab="name" inside
   [data-gx-tabs]; its pane is [data-gx-pane="name"] in the same sheet. The
   open tab is kept in the address (#name), so a reload, the back button or
   a link from elsewhere ("#conversation") opens the same part. Without this
   script every pane shows, one under another — nothing is lost.

   Also: [data-gx-open="name"] anywhere on the page opens that tab (a
   "Follow up" button that jumps to the conversation, say), and the answer
   card's two choices and quick prices on a custom-price request. */
(function () {
  'use strict';

  /* THE LOADER (client, 24 Oct 2026: "when i click things it kinda flickers
     ... make sure we have nice loaders"). A lime bar runs along the top and
     the sheet calms while the next page is on its way; a pressed submit
     button spins. The browser's own cross-fade (@view-transition in the CSS)
     does the rest. Cleared again if the page comes back from the back/forward
     cache, so nothing is left spinning. */
  var root = document.documentElement;
  var bar = document.createElement('div');
  bar.className = 'gx-loadbar';
  bar.setAttribute('aria-hidden', 'true');
  document.body.appendChild(bar);
  var spin = document.createElement('div');
  spin.className = 'gx-spin';
  spin.setAttribute('role', 'status');
  spin.innerHTML = '<i aria-hidden="true"></i><span>Loading…</span>';
  document.body.appendChild(spin);
  function busy() { root.classList.add('gx-loading'); }

  /* Asking first: a form with data-gx-confirm, or a button with
     data-gx-ask, confirms before it goes. Listened for in the capture phase
     so a "no" stops the loader too. */
  document.addEventListener('submit', function (e) {
    var f = e.target, msg = f.getAttribute && f.getAttribute('data-gx-confirm');
    if (msg && !window.confirm(msg)) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-gx-ask]');
    if (b && !window.confirm(b.getAttribute('data-gx-ask'))) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href]');
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (a.target && a.target !== '_self') return;
    if (a.hasAttribute('download') || a.origin !== location.origin) return;
    if (a.pathname === location.pathname && a.search === location.search && a.hash) return;  // same page, a tab
    if (/[?&]export=/.test(a.search)) return;  // a file, not a page
    // Hold the click for a breath so the loader is seen, then go — a local
    // page answers faster than the eye would otherwise catch it.
    e.preventDefault();
    busy();
    var href = a.href;
    setTimeout(function () { location.href = href; }, 160);
  });
  document.addEventListener('submit', function (e) {
    if (e.defaultPrevented) return;
    var btn = e.submitter || e.target.querySelector('[type=submit]');
    if (btn && btn.classList.contains('gx-btn')) btn.classList.add('is-busy');
    busy();
  });
  window.addEventListener('pageshow', function () {
    root.classList.remove('gx-loading');
    document.querySelectorAll('.gx-btn.is-busy').forEach(function (b) { b.classList.remove('is-busy'); });
  });

  /* THE DROPDOWN (client, 24 Oct 2026: "the drop down not quite good, let's
     have a better design"). Every select.gx-in becomes a button and a list
     in the page's own style; the real <select> stays underneath, kept in
     step, and is what the form sends — so without this script the plain
     select still works. Arrow keys, Enter, Escape and typing to search (a
     search box appears once there are more than seven choices). */
  var COLOURS = ['--cc-1', '--cc-2', '--cc-3', '--cc-4', '--cc-5'];
  var CHEV = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var openSel = null;
  document.querySelectorAll('select.gx-in').forEach(function (sel, n) {
    var wrap = document.createElement('div');
    wrap.className = 'gx-sel';
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(sel);
    sel.tabIndex = -1;
    var id = 'gxsel' + n;
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'gx-sel-btn';
    btn.setAttribute('aria-haspopup', 'listbox'); btn.setAttribute('aria-expanded', 'false'); btn.setAttribute('aria-controls', id);
    var label = sel.closest('label');
    if (label) btn.setAttribute('aria-label', label.childNodes[0].textContent.trim());
    btn.innerHTML = '<span class="gx-sel-dot" aria-hidden="true"></span><span class="gx-sel-val"></span>' + CHEV;
    wrap.appendChild(btn);
    var pop = document.createElement('div');
    pop.className = 'gx-sel-pop';
    var find = null;
    var real = Array.prototype.filter.call(sel.options, function (o) { return o.value; });
    if (real.length > 7) {
      find = document.createElement('input');
      find.type = 'search'; find.className = 'gx-sel-find'; find.placeholder = 'Type to search…';
      pop.appendChild(find);
    }
    var list = document.createElement('ul');
    list.className = 'gx-sel-list'; list.id = id; list.setAttribute('role', 'listbox');
    pop.appendChild(list);
    var empty = document.createElement('div');
    empty.className = 'gx-sel-empty'; empty.textContent = 'Nothing matches'; empty.hidden = true;
    pop.appendChild(empty);
    wrap.appendChild(pop);
    var items = Array.prototype.map.call(sel.options, function (o, i) {
      var li = document.createElement('li');
      li.className = 'gx-sel-opt' + (o.value ? '' : ' is-none');
      li.setAttribute('role', 'option');
      li.dataset.i = i;
      if (o.value) li.style.setProperty('--oc', 'var(' + COLOURS[(i - 1 + COLOURS.length) % COLOURS.length] + ')');
      li.innerHTML = (o.value ? '<i aria-hidden="true"></i>' : '') + '<span></span>';
      li.querySelector('span').textContent = o.value ? o.text : 'None';
      list.appendChild(li);
      return li;
    });
    var active = -1;
    function paint() {
      var o = sel.options[sel.selectedIndex], v = wrap.querySelector('.gx-sel-val');
      v.textContent = o && o.value ? o.text : (o ? o.text : 'Choose…');
      v.classList.toggle('is-empty', !(o && o.value));
      wrap.querySelector('.gx-sel-dot').style.background = o && o.value ? 'var(' + COLOURS[(sel.selectedIndex - 1 + COLOURS.length) % COLOURS.length] + ')' : '#C9D0DA';
      items.forEach(function (li, i) { li.setAttribute('aria-selected', i === sel.selectedIndex ? 'true' : 'false'); });
    }
    function visible() { return items.filter(function (li) { return !li.hidden; }); }
    function light(i) {
      var vis = visible();
      if (!vis.length) return;
      active = Math.max(0, Math.min(vis.length - 1, i));
      items.forEach(function (li) { li.classList.remove('is-active'); });
      vis[active].classList.add('is-active');
      vis[active].scrollIntoView({ block: 'nearest' });
    }
    function open() {
      if (openSel && openSel !== close) openSel();
      wrap.classList.add('is-open'); btn.setAttribute('aria-expanded', 'true');
      openSel = close;
      var vis = visible();
      light(Math.max(0, vis.indexOf(items[sel.selectedIndex])));
      if (find) { find.value = ''; filter(); setTimeout(function () { find.focus(); }, 30); }
    }
    function close() {
      wrap.classList.remove('is-open'); btn.setAttribute('aria-expanded', 'false');
      if (openSel === close) openSel = null;
    }
    function choose(li) {
      sel.selectedIndex = +li.dataset.i;
      sel.dispatchEvent(new Event('change', { bubbles: true }));
      sel.dispatchEvent(new Event('input', { bubbles: true }));
      paint(); close(); btn.focus();
    }
    function filter() {
      var q = (find.value || '').toLowerCase();
      items.forEach(function (li) { li.hidden = q && li.textContent.toLowerCase().indexOf(q) === -1; });
      empty.hidden = visible().length > 0;
      light(0);
    }
    if (find) find.addEventListener('input', filter);
    btn.addEventListener('click', function () { wrap.classList.contains('is-open') ? close() : open(); });
    list.addEventListener('click', function (e) { var li = e.target.closest('.gx-sel-opt'); if (li) choose(li); });
    list.addEventListener('mousemove', function (e) { var li = e.target.closest('.gx-sel-opt'); if (li) light(visible().indexOf(li)); });
    wrap.addEventListener('keydown', function (e) {
      var isOpen = wrap.classList.contains('is-open');
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (!isOpen) { open(); return; }
        light(active + (e.key === 'ArrowDown' ? 1 : -1));
      } else if (e.key === 'Enter' && isOpen) {
        e.preventDefault();
        var vis = visible(); if (vis[active]) choose(vis[active]);
      } else if (e.key === 'Escape' && isOpen) {
        e.preventDefault(); close(); btn.focus();
      } else if (e.key === 'Tab' && isOpen) { close(); }
    });
    document.addEventListener('pointerdown', function (e) { if (!wrap.contains(e.target)) close(); });
    sel.addEventListener('change', paint);
    paint();
  });

  /* THE CALENDAR (client, 24 Oct 2026: "the schedule dates is not working...
     make sure the calendar looks great"). Every input[data-gx-cal] — a
     datetime-local — gets a button that opens a month to pick a day from,
     hours and minutes as chips, and a few quick picks. It writes the same
     "YYYY-MM-DDTHH:MM" back into the real input (and fires input/change), so
     the form sends exactly what it did before. data-gx-cal-future greys out
     the days already gone. */
  var MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  var DOW = ['Mo','Tu','We','Th','Fr','Sa','Su'];
  var CAL_IC = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="5" width="17" height="15" rx="3" fill="none" stroke="currentColor" stroke-width="2"/><path d="M3.5 10h17M8 3v4M16 3v4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function toVal(d) { return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + 'T' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()); }
  function fromVal(v) { var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(v || ''); return m ? new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5]) : null; }
  function nice(d) {
    var h = d.getHours() % 12 || 12;
    return ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][d.getDay()] + ' ' + d.getDate() + ' ' + MONTHS[d.getMonth()].slice(0, 3) + ' ' + d.getFullYear() +
      ' · ' + h + ':' + pad2(d.getMinutes()) + ' ' + (d.getHours() < 12 ? 'am' : 'pm');
  }
  var openCal = null;
  document.querySelectorAll('input[data-gx-cal]').forEach(function (inp) {
    var future = inp.hasAttribute('data-gx-cal-future');
    var wrap = document.createElement('div');
    wrap.className = 'gx-cal';
    inp.parentNode.insertBefore(wrap, inp);
    wrap.appendChild(inp);
    inp.tabIndex = -1;
    var btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'gx-cal-btn'; btn.setAttribute('aria-haspopup', 'dialog'); btn.setAttribute('aria-expanded', 'false');
    btn.innerHTML = '<span class="gx-cal-ic">' + CAL_IC + '</span><span class="gx-cal-val"></span><span class="gx-cal-x" role="button" aria-label="Clear" title="Clear">&times;</span>';
    wrap.appendChild(btn);
    var pop = document.createElement('div');
    pop.className = 'gx-cal-pop'; pop.setAttribute('role', 'dialog'); pop.setAttribute('aria-label', 'Choose a date and time');
    pop.innerHTML =
      '<div class="gx-cal-quick"></div>' +
      '<div class="gx-cal-head"><button type="button" class="gx-cal-nav" data-d="-1" aria-label="Previous month">&#8249;</button><b></b><button type="button" class="gx-cal-nav" data-d="1" aria-label="Next month">&#8250;</button></div>' +
      '<div class="gx-cal-dow">' + DOW.map(function (d) { return '<span>' + d + '</span>'; }).join('') + '</div>' +
      '<div class="gx-cal-grid"></div>' +
      '<div class="gx-cal-time"><p class="gx-cal-k">Time</p><div class="gx-cal-hours"></div><div class="gx-cal-mins"></div><div class="gx-cal-ampm"></div></div>' +
      '<div class="gx-cal-foot"><span class="gx-cal-sum"></span><button type="button" class="gx-cal-done">Done</button></div>';
    wrap.appendChild(pop);

    var sel = fromVal(inp.value), view = sel ? new Date(sel) : new Date();
    view.setDate(1);
    var today = new Date(); today.setHours(0, 0, 0, 0);

    function write(d) {
      sel = d;
      inp.value = d ? toVal(d) : '';
      inp.dispatchEvent(new Event('input', { bubbles: true }));
      inp.dispatchEvent(new Event('change', { bubbles: true }));
      paint();
    }
    function paint() {
      var v = btn.querySelector('.gx-cal-val');
      v.textContent = sel ? nice(sel) : 'Pick a date and time';
      v.classList.toggle('is-empty', !sel);
      wrap.classList.toggle('has-value', !!sel);
      pop.querySelector('.gx-cal-head b').textContent = MONTHS[view.getMonth()] + ' ' + view.getFullYear();
      var grid = pop.querySelector('.gx-cal-grid'), html = '';
      var lead = (view.getDay() + 6) % 7, days = new Date(view.getFullYear(), view.getMonth() + 1, 0).getDate();
      for (var i = 0; i < lead; i++) html += '<span></span>';
      for (var d = 1; d <= days; d++) {
        var day = new Date(view.getFullYear(), view.getMonth(), d);
        var past = future && day < today;
        var cls = 'gx-cal-day' + (day.getTime() === today.getTime() ? ' is-today' : '') +
          (sel && sel.getFullYear() === day.getFullYear() && sel.getMonth() === day.getMonth() && sel.getDate() === d ? ' is-on' : '');
        html += '<button type="button" class="' + cls + '" data-day="' + d + '"' + (past ? ' disabled' : '') + '>' + d + '</button>';
      }
      grid.innerHTML = html;
      var h = sel ? (sel.getHours() % 12 || 12) : null, m = sel ? sel.getMinutes() : null, pm = sel ? sel.getHours() >= 12 : null;
      pop.querySelector('.gx-cal-hours').innerHTML = [12,1,2,3,4,5,6,7,8,9,10,11].map(function (x) {
        return '<button type="button" data-h="' + x + '"' + (x === h ? ' class="is-on"' : '') + '>' + x + '</button>'; }).join('');
      pop.querySelector('.gx-cal-mins').innerHTML = [0,15,30,45].map(function (x) {
        return '<button type="button" data-m="' + x + '"' + (x === m ? ' class="is-on"' : '') + '>:' + pad2(x) + '</button>'; }).join('');
      pop.querySelector('.gx-cal-ampm').innerHTML = ['am','pm'].map(function (x) {
        return '<button type="button" data-p="' + x + '"' + ((pm === true && x === 'pm') || (pm === false && x === 'am') ? ' class="is-on"' : '') + '>' + x + '</button>'; }).join('');
      pop.querySelector('.gx-cal-sum').textContent = sel ? nice(sel) : 'Nothing chosen';
    }
    function quick() {
      var q = pop.querySelector('.gx-cal-quick'), now = new Date();
      var t9 = new Date(now); t9.setDate(now.getDate() + 1); t9.setHours(9, 0, 0, 0);
      var mon = new Date(now); mon.setDate(now.getDate() + ((8 - now.getDay()) % 7 || 7)); mon.setHours(9, 0, 0, 0);
      var wk = new Date(now); wk.setDate(now.getDate() + 7); wk.setHours(12, 0, 0, 0);
      var hr = new Date(now.getTime() + 3600e3); hr.setMinutes(Math.ceil(hr.getMinutes() / 15) * 15, 0, 0);
      var opts = [['In an hour', hr], ['Tomorrow 9 am', t9], ['Monday 9 am', mon], ['In a week', wk]];
      q.innerHTML = opts.map(function (o, i) { return '<button type="button" data-q="' + i + '">' + o[0] + '</button>'; }).join('');
      q.querySelectorAll('[data-q]').forEach(function (b) {
        b.addEventListener('click', function () { var d = opts[+b.dataset.q][1]; view = new Date(d); view.setDate(1); write(new Date(d)); });
      });
    }
    function open() {
      if (openCal && openCal !== close) openCal();
      quick(); paint();
      wrap.classList.add('is-open'); btn.setAttribute('aria-expanded', 'true'); openCal = close;
    }
    function close() { wrap.classList.remove('is-open'); btn.setAttribute('aria-expanded', 'false'); if (openCal === close) openCal = null; }
    btn.addEventListener('click', function (e) {
      if (e.target.closest('.gx-cal-x')) { e.stopPropagation(); write(null); close(); return; }
      wrap.classList.contains('is-open') ? close() : open();
    });
    pop.addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      if (b.dataset.d) { view.setMonth(view.getMonth() + +b.dataset.d); paint(); return; }
      var base = sel ? new Date(sel) : (function () { var d = new Date(view); d.setHours(9, 0, 0, 0); return d; })();
      if (b.dataset.day) { base.setFullYear(view.getFullYear(), view.getMonth(), +b.dataset.day); write(base); return; }
      if (b.dataset.h) { var pmNow = base.getHours() >= 12; base.setHours((+b.dataset.h % 12) + (pmNow ? 12 : 0)); write(base); return; }
      if (b.dataset.m) { base.setMinutes(+b.dataset.m); write(base); return; }
      if (b.dataset.p) { var hh = base.getHours() % 12; base.setHours(hh + (b.dataset.p === 'pm' ? 12 : 0)); write(base); return; }
      if (b.classList.contains('gx-cal-done')) { close(); btn.focus(); }
    });
    wrap.addEventListener('keydown', function (e) { if (e.key === 'Escape' && wrap.classList.contains('is-open')) { e.preventDefault(); close(); btn.focus(); } });
    document.addEventListener('pointerdown', function (e) { if (!wrap.contains(e.target)) close(); });
    paint();
  });

  /* THE BACKUP COUNTDOWN ([data-gx-countdown] — the sync bridge and the
     "Next backup" tiles). Ticks every second: mm:ss to the next run, the ring
     filling as it gets closer. At zero it says so rather than going
     negative — the timer has a two-minute random delay, so the run lands a
     little after. */
  var CIRC = 2 * Math.PI * 52;
  function tickAll() {
    document.querySelectorAll('[data-gx-countdown]').forEach(function (el) {
      var at = el.getAttribute('data-gx-countdown'), out = el.querySelector('[data-gx-left]');
      if (!at || !out) return;
      var every = (+el.getAttribute('data-every') || 3600) * 1000;
      var left = new Date(at).getTime() - Date.now();
      var ring = el.querySelector('[data-gx-ring]');
      if (left <= 0) {
        out.textContent = 'due now';
        el.classList.add('is-due');
        if (ring) ring.style.strokeDashoffset = 0;
        return;
      }
      var m = Math.floor(left / 60000), sec = Math.floor(left / 1000) % 60;
      out.textContent = (m >= 60 ? Math.floor(m / 60) + ':' + ('0' + (m % 60)).slice(-2) + ':' : m + ':') + ('0' + sec).slice(-2);
      if (ring) {
        ring.style.strokeDasharray = CIRC;
        ring.style.strokeDashoffset = CIRC * Math.max(0, Math.min(1, left / every));
      }
    });
  }
  if (document.querySelector('[data-gx-countdown]')) { tickAll(); setInterval(tickAll, 1000); }

  /* HTML IN A PLAIN FORM ([data-gx-rich] — a textarea holding markup, on a
     record's Edit tab): edited as formatted words, with a small toolbar,
     and written back into the textarea as it changes. */
  document.querySelectorAll('textarea[data-gx-rich]').forEach(function (ta) {
    var box = document.createElement('div');
    box.className = 'gx-rich';
    box.innerHTML =
      '<div class="gx-rich-bar" role="toolbar">' +
      '<button type="button" data-c="bold" title="Bold"><b>B</b></button>' +
      '<button type="button" data-c="italic" title="Italic"><i>I</i></button>' +
      '<button type="button" data-c="underline" title="Underline"><u>U</u></button>' +
      '<button type="button" data-c="formatBlock" data-v="H2" title="Subtitle">H2</button>' +
      '<button type="button" data-c="formatBlock" data-v="P" title="Paragraph">¶</button>' +
      '<button type="button" data-c="insertUnorderedList" title="Bullet list">• list</button>' +
      '<button type="button" data-c="createLink" title="Link">Link</button>' +
      '<button type="button" data-c="removeFormat" title="Clear formatting">Clear</button></div>' +
      '<div class="gx-rich-surface" contenteditable="true"></div>';
    ta.parentNode.insertBefore(box, ta.nextSibling);
    ta.hidden = true;
    var surf = box.querySelector('.gx-rich-surface');
    surf.innerHTML = ta.value;
    function sync() { ta.value = surf.innerHTML; }
    surf.addEventListener('input', sync);
    box.querySelector('.gx-rich-bar').addEventListener('mousedown', function (e) { e.preventDefault(); });
    box.querySelectorAll('[data-c]').forEach(function (b) {
      b.addEventListener('click', function () {
        var c = b.getAttribute('data-c'), v = b.getAttribute('data-v') || null;
        if (c === 'createLink') { v = window.prompt('Link address', 'https://'); if (!v) return; }
        surf.focus(); document.execCommand(c, false, v); sync();
      });
    });
  });

  /* A sandboxed preview (a blog story's own HTML) grows to its content. */
  document.querySelectorAll('iframe[data-gx-autoheight]').forEach(function (f) {
    function fit() { try { f.style.height = (f.contentDocument.documentElement.scrollHeight + 4) + 'px'; } catch (e) {} }
    f.addEventListener('load', function () { fit(); setTimeout(fit, 400); });
  });

  /* A table whose rows open a page (data-gx-rowlinks): the whole row is the
     target, not just the name — the name stays a real link for keyboards. */
  document.querySelectorAll('[data-gx-rowlinks] tr[data-href]').forEach(function (tr) {
    tr.addEventListener('click', function (e) {
      if (e.target.closest('a, button, input')) return;
      var a = tr.querySelector('a[href]');
      if (a) a.click();
    });
  });

  var sheets = Array.prototype.slice.call(document.querySelectorAll('[data-gx-tabs]'));
  if (!sheets.length && !document.querySelector('[data-gx-answer]')) return;
  document.documentElement.classList.add('gx-js');

  function open(sheet, name, push) {
    var tabs = sheet.querySelectorAll('[data-gx-tab]'), panes = sheet.querySelectorAll('[data-gx-pane]');
    var hit = false;
    tabs.forEach(function (t) {
      var on = t.getAttribute('data-gx-tab') === name;
      if (on) hit = true;
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    if (!hit) return false;
    panes.forEach(function (p) { p.classList.toggle('is-on', p.getAttribute('data-gx-pane') === name); });
    if (push) history.replaceState(null, '', location.pathname + location.search + '#' + name);
    return true;
  }
  function fromHash() {
    var name = decodeURIComponent(location.hash.slice(1));
    return sheets.some(function (s) { return name && open(s, name, false); });
  }
  sheets.forEach(function (sheet) {
    sheet.querySelectorAll('[data-gx-tab]').forEach(function (t) {
      t.addEventListener('click', function (e) { e.preventDefault(); open(sheet, t.getAttribute('data-gx-tab'), true); });
    });
    var first = sheet.querySelector('[data-gx-tab][aria-selected="true"]') || sheet.querySelector('[data-gx-tab]');
    if (first) open(sheet, first.getAttribute('data-gx-tab'), false);
  });
  fromHash();
  sheets.forEach(function (s) { s.classList.add('is-ready'); });
  window.addEventListener('hashchange', fromHash);
  document.querySelectorAll('[data-gx-open]').forEach(function (b) {
    b.addEventListener('click', function (e) {
      var name = b.getAttribute('data-gx-open');
      if (sheets.some(function (s) { return open(s, name, true); })) {
        e.preventDefault();
        var sheet = sheets.filter(function (s) { return s.querySelector('[data-gx-pane="' + name + '"]'); })[0];
        if (sheet) sheet.scrollIntoView({ behavior: 'smooth', block: 'start' });
        var field = sheet && sheet.querySelector('[data-gx-pane="' + name + '"] textarea');
        if (field) setTimeout(function () { field.focus({ preventScroll: true }); }, 350);
      }
    });
  });

  /* The answer card on a custom-price request: two choices, quick prices,
     and the price read back per person as it is typed. */
  var box = document.querySelector('[data-gx-answer]');
  if (box) {
    var seg = box.querySelectorAll('[data-gx-choice]'), forms = box.querySelectorAll('[data-gx-choice-pane]');
    seg.forEach(function (b) {
      b.addEventListener('click', function () {
        seg.forEach(function (o) { o.setAttribute('aria-selected', o === b ? 'true' : 'false'); });
        forms.forEach(function (f) { f.hidden = f.getAttribute('data-gx-choice-pane') !== b.getAttribute('data-gx-choice'); });
      });
    });
    var form = box.querySelector('[data-gx-offer]');
    if (form) {
      var price = form.querySelector('[name=price_aud]'), people = form.querySelector('[name=seat_limit]'), live = form.querySelector('[data-gx-live]');
      var read = function () {
        var p = parseFloat(price.value), n = parseInt(people.value, 10);
        live.innerHTML = p > 0 && n > 0 ? '<b>$' + (p / n).toFixed(2) + '</b> a person a year, at full size' : 'Type a price to see it per person.';
      };
      box.querySelectorAll('[data-gx-fill]').forEach(function (b) {
        b.addEventListener('click', function () {
          price.value = b.getAttribute('data-gx-fill');
          if (!people.value && b.getAttribute('data-gx-seats')) people.value = b.getAttribute('data-gx-seats');
          read(); price.focus();
        });
      });
      form.addEventListener('input', read);
    }
  }

  /* A package's preview card, rebuilt as its form is filled in. */
  var pkg = document.querySelector('[data-gx-pkg]');
  if (pkg) {
    var pp = pkg.querySelector('[data-gx-pp]');
    var sync = function () {
      pkg.querySelectorAll('[data-pv]').forEach(function (f) {
        var out = document.querySelector('[data-pv-out="' + f.getAttribute('data-pv') + '"]');
        if (!out) return;
        var v = f.value;
        if (f.getAttribute('data-pv') === 'price' && v) v = Number(v).toLocaleString('en-AU');
        if (v) out.textContent = v;
      });
      var p = parseFloat(pkg.querySelector('[name=price_aud]').value), n = parseInt(pkg.querySelector('[name=seat_limit]').value, 10);
      if (pp) pp.innerHTML = p > 0 && n > 0 ? '<b>$' + (p / n).toFixed(2) + '</b> a person a year, at full size' : '';
    };
    pkg.addEventListener('input', sync);
    sync();
  }
})();
