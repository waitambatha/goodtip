/* The home page's "Live this season" board.
 *
 * Left: the top of the real ladder. Right: four charts of the same
 * competition (a line, a bar graph, a histogram and a 3D pie), all in the
 * competition's own colour where a colour says which competition it is. Every four seconds the whole board moves on to the next
 * competition by itself. There is deliberately nothing to click — a visitor
 * watches it; picking a competition and digging in is what a member account is
 * for.
 *
 * Every number comes from the payload data_sync/public_board.py builds. The
 * page arrives with that payload inline (#board-data), so it draws with no
 * request, and then asks /ladder-board/ every half minute for a fresh copy. It
 * is fresh, not streamed: the ladder only moves when a result is synced, and
 * the header says when it last did.
 *
 * It rotates whatever the visitor's motion setting says (the setting only
 * removes the animation, not the content), and it stops while the section is
 * off screen or the tab is in the background so it is not working for nobody.
 */
(function () {
  'use strict';

  var shell = document.querySelector('[data-board]');
  var seed = document.getElementById('board-data');
  if (!shell || !seed) return;

  var data;
  try { data = JSON.parse(seed.textContent); } catch (e) { return; }
  if (!data.codes || !data.codes.length) return;

  var STEP_MS = 4000;       // how long each competition stays up
  var POLL_MS = 30000;      // how often to ask for fresh numbers

  var rowsEl = shell.querySelector('[data-bd-rows]');
  var titleEl = shell.querySelector('[data-bd-title]');
  var updatedEl = shell.querySelector('[data-bd-updated]');
  var chartsEl = shell.querySelector('[data-bd-charts]');
  var nowEl = document.querySelector('.bd-now');
  var nowChips = nowEl ? nowEl.querySelectorAll('span') : [];
  var colsEl = shell.querySelector('.bd-cols');

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches &&
    !document.documentElement.classList.contains('gt-motion');

  var ix = 0;
  var onScreen = true;
  var timer = null;
  var lastSig = {};

  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function num(n) { return Number(n).toLocaleString('en-AU'); }
  function cur() { return data.codes[ix]; }

  function crest(c) {
    if (c.logo) return '<span class="crest has-logo"><img src="' + esc(c.logo) + '" alt="" loading="lazy"></span>';
    return '<span class="crest" style="--crest-bg:' + esc(c.bg) + ';--crest-fg:' + esc(c.fg) + '">' + esc(c.code) + '</span>';
  }
  function pips(form) {
    var out = '';
    for (var i = 0; i < form.length; i++) {
      out += '<i class="' + (form[i] === 'W' ? 'w' : form[i] === 'L' ? 'l' : '') + '"></i>';
    }
    return out;
  }

  /* ---- The ladder ---- */

  function drawLadder(flash) {
    var c = cur();
    var html = '';
    c.ladder.forEach(function (r) {
      var key = c.code + '|' + r.name;
      var sig = r.rank + ':' + r.points;
      var changed = flash && lastSig[key] && lastSig[key] !== sig;
      lastSig[key] = sig;
      html += '<div class="bd-row' + (r.rank === 1 ? ' top' : '') + (changed ? ' chg' : '') + '">' +
        '<span class="rank">' + r.rank + '</span>' + crest(r) +
        '<span class="who">' + esc(r.name) + '</span>' +
        '<span class="form" aria-label="Last five: ' + esc(r.form) + '">' + pips(r.form) + '</span>' +
        '<span class="pl">' + r.played + '</span>' +
        '<span class="rec">' + r.wins + '-' + r.losses + (r.draws ? '-' + r.draws : '') + '</span>' +
        '<span class="df">' + esc(r.diff) + '</span>' +
        '<span class="pts">' + r.points + '</span></div>';
    });
    rowsEl.innerHTML = html;
    shell.setAttribute('data-code', c.code.toLowerCase());
    shell.setAttribute('data-comp', c.code.toLowerCase());
    titleEl.textContent = c.code + ' ' + c.season + ' · after round ' + c.round;
    colsEl.children[6].textContent = c.by_percentage ? '%' : 'Diff';
    if (nowEl) nowEl.setAttribute('data-code', c.code.toLowerCase());
    nowChips.forEach(function (chip) {
      chip.classList.toggle('on', chip.getAttribute('data-code') === c.code);
    });
    drawUpdated();
  }

  function ago(iso) {
    var s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 90) return 'just now';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) { var h = Math.round(s / 3600); return h + (h === 1 ? ' hour ago' : ' hours ago'); }
    var d = Math.round(s / 86400);
    return d + (d === 1 ? ' day ago' : ' days ago');
  }
  function drawUpdated() { updatedEl.textContent = 'Updated ' + ago(cur().updated); }

  /* ---- The charts ---- */

  // One colour a club, by its place in the table, so the bar and the pie agree.
  // Eight hues that hold up on the white panels: blue, orange, rose, violet,
  // teal, olive, red, green.
  var CLUBS = ['#2F86E6', '#E8871E', '#E0457F', '#8F55DB', '#12A39B', '#8DB800', '#E5533A', '#2FA05E'];

  // Chart 1, a line: average points a game, round by round, in the competition's colour.
  function lineChart(c) {
    var pts = c.rounds;
    if (pts.length < 2) return '<p class="bd-empty">Fills in from round two.</p>';
    var W = 300, H = 104, padT = 10, padB = 4;
    var lo = Infinity, hi = -Infinity;
    pts.forEach(function (p) { lo = Math.min(lo, p.avg); hi = Math.max(hi, p.avg); });
    if (hi === lo) { hi += 1; lo -= 1; }
    var span = hi - lo;
    var xy = pts.map(function (p, i) {
      return [(i / (pts.length - 1)) * W, padT + (1 - (p.avg - lo) / span) * (H - padT - padB)];
    });
    var line = xy.map(function (q, i) { return (i ? 'L' : 'M') + q[0].toFixed(1) + ' ' + q[1].toFixed(1); }).join(' ');
    var fill = line + ' L' + W + ' ' + H + ' L0 ' + H + ' Z';
    var last = xy[xy.length - 1];
    return '<svg class="bd-line" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" role="img" aria-label="Points a game by round">' +
      '<defs><linearGradient id="bdfill" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0%" stop-color="currentColor" stop-opacity=".38"/>' +
      '<stop offset="100%" stop-color="currentColor" stop-opacity="0"/></linearGradient></defs>' +
      '<path d="' + fill + '" fill="url(#bdfill)"/>' +
      '<path class="bd-draw" pathLength="1" vector-effect="non-scaling-stroke" d="' + line + '" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
      '<div class="bd-scale"><span>Round ' + pts[0].round + '</span><span>' + lo.toFixed(0) + ' to ' + hi.toFixed(0) + '</span><span>Round ' + pts[pts.length - 1].round + '</span></div>';
  }

  // Chart 2, a bar graph: points scored a game by each club in the table, a colour each.
  function barChart(c) {
    var max = 0;
    c.ladder.forEach(function (r) { max = Math.max(max, r.avg_for); });
    if (!max) return '';
    return '<div class="bd-bars">' + c.ladder.map(function (r, i) {
      return '<div class="bd-bar" style="--c:' + CLUBS[i % CLUBS.length] + '"><span class="lab">' + esc(r.code) + '</span>' +
        '<span class="trk"><i style="--w:' + (r.avg_for / max * 100).toFixed(1) + '%"></i></span>' +
        '<span class="val">' + r.avg_for + '</span></div>';
    }).join('') + '</div>';
  }

  // Chart 3, a histogram: how many games finished by each winning margin.
  function histChart(c) {
    var m = c.margins;
    if (!m || !m.bins) return '';
    var max = Math.max.apply(null, m.bins);
    if (!max) return '';
    var last = m.bins.length - 1;
    return '<div class="bd-hist">' + m.bins.map(function (n, i) {
      return '<div class="bd-col" style="--h:' + (n / max * 100).toFixed(1) + '%;--t:' + (0.55 + 0.45 * n / max).toFixed(2) + '"><div class="z"><b>' + (n || '') + '</b><i></i></div>' +
        '<span>' + (i * m.width) + (i === last ? '+' : '') + '</span></div>';
    }).join('') + '</div>' +
      '<p class="bd-axis">Winning margin, in points</p>';
  }

  // Chart 4, a 3D pie: each club's share of the wins in the table. Slices are
  // drawn on a tilted ellipse; the front rim gets a darker wall for the depth.
  function shade(hex, f) {
    var n = parseInt(hex.slice(1), 16);
    var r = Math.round((n >> 16) * f), g = Math.round(((n >> 8) & 255) * f), b = Math.round((n & 255) * f);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }
  function pieChart(c) {
    var total = 0;
    c.ladder.forEach(function (r) { total += r.wins; });
    if (!total) return '';
    var W = 220, H = 118, cx = 110, cy = 48, rx = 96, ry = 42, depth = 18;
    var pt = function (a, dy) { return (cx + rx * Math.cos(a)).toFixed(1) + ' ' + (cy + dy + ry * Math.sin(a)).toFixed(1); };
    var slices = [], a0 = -Math.PI / 2;
    c.ladder.forEach(function (r, i) {
      var a1 = a0 + (r.wins / total) * 2 * Math.PI;
      slices.push({ a0: a0, a1: a1, col: CLUBS[i % CLUBS.length], r: r });
      a0 = a1;
    });
    var walls = '', tops = '';
    slices.forEach(function (s) {
      var span = s.a1 - s.a0;
      // the part of this slice that faces the viewer: angles between 0 and pi
      var f0 = Math.max(s.a0, 0), f1 = Math.min(s.a1, Math.PI);
      if (f1 > f0) {
        walls += '<path d="M' + pt(f0, 0) + ' A' + rx + ' ' + ry + ' 0 0 1 ' + pt(f1, 0) + ' L' + pt(f1, depth) +
          ' A' + rx + ' ' + ry + ' 0 0 0 ' + pt(f0, depth) + ' Z" fill="' + shade(s.col, 0.55) + '"/>';
      }
      tops += span >= 2 * Math.PI - 0.001
        ? '<ellipse cx="' + cx + '" cy="' + cy + '" rx="' + rx + '" ry="' + ry + '" fill="' + s.col + '"/>'
        : '<path d="M' + cx + ' ' + cy + ' L' + pt(s.a0, 0) + ' A' + rx + ' ' + ry + ' 0 ' + (span > Math.PI ? 1 : 0) + ' 1 ' + pt(s.a1, 0) +
          ' Z" fill="' + s.col + '" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/>';
    });
    var legend = slices.map(function (s) {
      return '<span style="--c:' + s.col + '">' + esc(s.r.code) + ' <b>' + s.r.wins + '</b></span>';
    }).join('');
    return '<svg class="bd-pie" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Share of the wins in the table">' + walls + tops + '</svg>' +
      '<p class="bd-legend">' + legend + '</p>';
  }

  function drawCharts() {
    var c = cur();
    var st = c.stats;
    var home = c.venue.home + c.venue.away ? Math.round(c.venue.home * 100 / (c.venue.home + c.venue.away)) : null;
    chartsEl.innerHTML =
      '<div class="bd-panel bd-p1"><h3>Scoring by round</h3><p class="sub">Averaging <b>' + st.avg_game + '</b> a game over ' + num(st.games) + ' games</p>' + lineChart(c) + '</div>' +
      '<div class="bd-panel bd-p2"><h3>Points a game</h3><p class="sub">Scored, for the top eight</p>' + barChart(c) + '</div>' +
      '<div class="bd-panel bd-p3"><h3>Winning margins</h3><p class="sub">How close the games finish</p>' + histChart(c) + '</div>' +
      '<div class="bd-panel bd-p4"><h3>Share of wins</h3><p class="sub">Top eight' + (home === null ? '' : ' &middot; home sides win <b>' + home + '%</b>') + '</p>' + pieChart(c) + '</div>';
  }

  /* ---- Turning the board over ---- */

  function show(next, animate) {
    ix = next;
    drawLadder(false);
    drawCharts();
    if (animate && !reduce) {
      [rowsEl, chartsEl].forEach(function (el) {
        el.classList.remove('bd-in');
        void el.offsetWidth;   // restart the entrance
        el.classList.add('bd-in');
      });
    }
    if (nowEl && !reduce) {
      nowEl.classList.remove('tick');
      void nowEl.offsetWidth;
      nowEl.classList.add('tick');
    }
  }

  function running() { return onScreen && !document.hidden && data.codes.length > 1; }

  function schedule() {
    clearTimeout(timer);
    if (!running()) return;
    timer = setTimeout(function () {
      show((ix + 1) % data.codes.length, true);
      schedule();
    }, STEP_MS);
  }

  document.addEventListener('visibilitychange', schedule);
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(function (es) {
      onScreen = es[0].isIntersecting;
      schedule();
    }, { threshold: 0.1 }).observe(shell);
  }

  /* ---- Fresh numbers ---- */

  function refresh() {
    if (document.hidden) return;
    fetch('/ladder-board/', { headers: { Accept: 'application/json' }, credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (fresh) {
        if (!fresh || !fresh.codes || !fresh.codes.length) return;
        var keep = cur().code;
        data = fresh;
        var at = data.codes.findIndex(function (c) { return c.code === keep; });
        ix = at < 0 ? 0 : at;
        drawLadder(true);
        drawCharts();
      })
      .catch(function () { /* stay on what we have; the next tick tries again */ });
  }
  setInterval(refresh, POLL_MS);
  setInterval(drawUpdated, 30000);

  // The seed already holds the first table; add the charts and start turning.
  drawLadder(false);
  drawCharts();
  schedule();
})();
