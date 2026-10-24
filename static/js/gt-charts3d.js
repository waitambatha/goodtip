/* Three-dimensional charts for the billing pages (client, 24 Oct 2026: "if
   you're talking about charts, let it be in 3D, something that is a little
   bit stylish, something that is a little bit creative").

   Drawn as SVG by hand rather than with a library: three shapes, each a few
   dozen lines, and the page already has everything else it needs.

     data-c3d="bars"  isometric prisms — front face, a darker side, a lit top
     data-c3d="tank"  a glass cylinder, filled to a level
     data-c3d="ring"  a donut tilted back, with a wall you can see

   Each reads its numbers from the element's own data-* attributes, or from
   the page's <script type="application/json" id="c3d-data"> under the key in
   data-key. Every mark has a hover/focus tooltip; the numbers are also in a
   table under each chart (the template's job), so nothing is colour alone.
   The bars grow in once, when the chart scrolls into view — nothing loops. */
(function () {
  'use strict';
  var NS = 'http://www.w3.org/2000/svg';
  var store = {};
  try { store = JSON.parse((document.getElementById('c3d-data') || {}).textContent || '{}'); } catch (e) { store = {}; }

  function el(name, attrs, parent) {
    var n = document.createElementNS(NS, name);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function shade(hex, amt) {
    // amt < 0 darkens toward black, > 0 lightens toward white.
    var c = hex.replace('#', '');
    if (c.length === 3) c = c.replace(/(.)/g, '$1$1');
    var n = parseInt(c, 16), r = n >> 16, g = (n >> 8) & 255, b = n & 255;
    var t = amt < 0 ? 0 : 255, p = Math.abs(amt);
    r = Math.round((t - r) * p + r); g = Math.round((t - g) * p + g); b = Math.round((t - b) * p + b);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }
  function fmt(n) { return Number(n).toLocaleString('en-AU', { maximumFractionDigits: 2 }); }

  /* One floating tooltip for the whole page. */
  var tip = document.createElement('div');
  tip.className = 'c3d-tip'; tip.setAttribute('role', 'status');
  document.body.appendChild(tip);
  function showTip(target, html) {
    tip.innerHTML = html;
    var r = target.getBoundingClientRect();
    tip.style.left = (r.left + r.width / 2) + 'px';
    tip.style.top = (r.top - 10) + 'px';
    tip.classList.add('is-on');
  }
  function hideTip() { tip.classList.remove('is-on'); }
  function hoverable(node, html) {
    node.setAttribute('tabindex', '0');
    node.addEventListener('mouseenter', function () { showTip(node, html); });
    node.addEventListener('mouseleave', hideTip);
    node.addEventListener('focus', function () { showTip(node, html); });
    node.addEventListener('blur', hideTip);
  }

  function whenSeen(node, fn) {
    if (!('IntersectionObserver' in window)) { fn(); return; }
    var io = new IntersectionObserver(function (es) {
      if (es[0].isIntersecting) { io.disconnect(); fn(); }
    }, { threshold: 0.25 });
    io.observe(node);
  }

  /* ---- isometric bars ---- */
  function bars(host) {
    var rows = store[host.getAttribute('data-key')] || [];
    if (!rows.length) return;
    var unit = host.getAttribute('data-unit') || '', pre = host.getAttribute('data-prefix') || '';
    var base = host.getAttribute('data-color') || '#2A7337';
    var W = 520, H = 250, padL = 18, padR = 34, floor = 196, top = 34;
    var n = rows.length, slot = (W - padL - padR) / n, w = Math.min(58, slot * 0.56);
    var dx = Math.min(18, w * 0.38), dy = -dx * 0.62;
    var max = Math.max.apply(null, rows.map(function (r) { return r.value; })) || 1;
    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, class: 'c3d-svg', role: 'img', 'aria-label': host.getAttribute('aria-label') || '' }, host);

    // the floor: a slab the bars stand on, with three quiet guide lines
    el('polygon', { points: [padL - 6, floor + 6, padL - 6 + dx * 1.6, floor + 6 + dy * 1.6, W - padR + dx * 1.6 + 6, floor + 6 + dy * 1.6, W - padR + 6, floor + 6].join(' '),
      class: 'c3d-floor' }, svg);
    for (var g = 1; g <= 3; g++) {
      var gy = floor - (floor - top) * g / 3;
      el('line', { x1: padL, y1: gy, x2: W - padR + dx, y2: gy, class: 'c3d-grid' }, svg);
    }

    var groups = [];
    rows.forEach(function (r, i) {
      var color = r.color || base;
      var x = padL + slot * i + (slot - w) / 2;
      var gEl = el('g', { class: 'c3d-bar' + (r.on || r.now ? ' is-on' : '') + (r.fits === false ? ' is-off' : '') }, svg);
      var front = el('rect', { x: x, width: w, rx: 2, fill: color }, gEl);
      var side = el('polygon', { fill: shade(color, -0.35) }, gEl);
      var lid = el('polygon', { fill: shade(color, 0.32) }, gEl);
      var lab = el('text', { x: x + w / 2 + dx / 2, y: floor + 28, class: 'c3d-x', 'text-anchor': 'middle' }, svg);
      lab.textContent = r.label;
      var val = null;
      if (r.on || r.now || i === n - 1) {
        val = el('text', { x: x + w / 2 + dx / 2, class: 'c3d-val', 'text-anchor': 'middle' }, gEl);
        val.textContent = pre + fmt(r.value) + (unit ? ' ' + unit : '');
      }
      if (r.on || r.now) {
        var tag = el('text', { x: x + w / 2 + dx / 2, class: 'c3d-tag', 'text-anchor': 'middle' }, gEl);
        tag.textContent = r.tag || (r.now ? 'so far' : 'yours');
        groups.push({ r: r, x: x, front: front, side: side, lid: lid, val: val, tag: tag });
      } else {
        groups.push({ r: r, x: x, front: front, side: side, lid: lid, val: val, tag: null });
      }
      var tipHtml = '<b>' + r.label + '</b>' + pre + fmt(r.value) + (unit ? ' ' + unit : '') +
        (r.note ? '<small>' + r.note + '</small>' : '');
      hoverable(gEl, tipHtml);
    });

    function draw(k) {
      groups.forEach(function (o) {
        var h = Math.max(3, (floor - top) * (o.r.value / max) * k), y = floor - h, x = o.x;
        o.front.setAttribute('y', y); o.front.setAttribute('height', h);
        o.side.setAttribute('points', [x + w, y, x + w + dx, y + dy, x + w + dx, floor + dy, x + w, floor].join(' '));
        o.lid.setAttribute('points', [x, y, x + dx, y + dy, x + w + dx, y + dy, x + w, y].join(' '));
        if (o.val) o.val.setAttribute('y', y + dy - 8);
        if (o.tag) o.tag.setAttribute('y', y + dy - (o.val ? 24 : 8));
      });
    }
    draw(0.02);
    whenSeen(host, function () {
      var t0 = null;
      function step(t) {
        if (t0 === null) t0 = t;
        var p = Math.min(1, (t - t0) / 900), e = 1 - Math.pow(1 - p, 3);
        draw(Math.max(0.02, e));
        if (p < 1) requestAnimationFrame(step);
      }
      requestAnimationFrame(step);
    });
  }

  /* ---- the tank ---- */
  function tank(host) {
    var used = +host.getAttribute('data-used') || 0, cap = +host.getAttribute('data-cap') || 1;
    var color = host.getAttribute('data-color') || '#2A7337';
    var pct = Math.max(0, Math.min(1, used / cap));
    var W = 200, H = 250, cx = 100, rx = 70, ry = 20, topY = 34, botY = 206;
    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, class: 'c3d-svg c3d-tank', role: 'img',
      'aria-label': used + ' of ' + cap + ' places used' }, host);
    var defs = el('defs', {}, svg);
    var gid = 'tg' + Math.random().toString(36).slice(2, 7);
    var lg = el('linearGradient', { id: gid, x1: 0, x2: 1, y1: 0, y2: 0 }, defs);
    el('stop', { offset: '0', 'stop-color': shade(color, -0.25) }, lg);
    el('stop', { offset: '.45', 'stop-color': shade(color, 0.18) }, lg);
    el('stop', { offset: '1', 'stop-color': shade(color, -0.4) }, lg);
    var glass = el('linearGradient', { id: gid + 'g', x1: 0, x2: 1, y1: 0, y2: 0 }, defs);
    el('stop', { offset: '0', 'stop-color': 'rgba(15,46,34,.10)' }, glass);
    el('stop', { offset: '.5', 'stop-color': 'rgba(15,46,34,.03)' }, glass);
    el('stop', { offset: '1', 'stop-color': 'rgba(15,46,34,.12)' }, glass);

    el('ellipse', { cx: cx, cy: botY + 14, rx: rx + 12, ry: ry * 0.8, class: 'c3d-shadow' }, svg);
    el('path', { d: 'M' + (cx - rx) + ',' + topY + ' V' + botY + ' A' + rx + ',' + ry + ' 0 0 0 ' + (cx + rx) + ',' + botY + ' V' + topY, fill: 'url(#' + gid + 'g)', class: 'c3d-glass' }, svg);
    var liquid = el('path', { fill: 'url(#' + gid + ')' }, svg);
    var surface = el('ellipse', { cx: cx, rx: rx, ry: ry, fill: shade(color, 0.35) }, svg);
    el('ellipse', { cx: cx, cy: topY, rx: rx, ry: ry, class: 'c3d-rim' }, svg);
    // capacity ticks on the glass
    [0.25, 0.5, 0.75].forEach(function (q) {
      var y = botY - (botY - topY) * q;
      el('path', { d: 'M' + (cx + rx - 14) + ',' + (y + ry * 0.9) + ' q 8 -2 14 -' + (ry * 0.6), class: 'c3d-tick' }, svg);
    });
    el('path', { d: 'M' + (cx - rx + 12) + ',' + (topY + 18) + ' V' + (botY - 6), class: 'c3d-shine' }, svg);
    hoverable(svg, '<b>' + fmt(used) + ' of ' + fmt(cap) + '</b>' + fmt(cap - used) + ' places left');

    function draw(k) {
      var level = botY - (botY - topY) * pct * k;
      liquid.setAttribute('d', 'M' + (cx - rx) + ',' + level + ' V' + botY + ' A' + rx + ',' + ry + ' 0 0 0 ' + (cx + rx) + ',' + botY + ' V' + level + ' A' + rx + ',' + ry + ' 0 0 1 ' + (cx - rx) + ',' + level);
      surface.setAttribute('cy', level);
      surface.style.opacity = pct * k > 0.005 ? 1 : 0;
    }
    draw(0);
    whenSeen(host, function () {
      var t0 = null;
      function step(t) {
        if (t0 === null) t0 = t;
        var p = Math.min(1, (t - t0) / 1100), e = 1 - Math.pow(1 - p, 3);
        draw(e);
        if (p < 1) requestAnimationFrame(step);
      }
      requestAnimationFrame(step);
    });
  }

  /* ---- the tilted ring ---- */
  function ring(host) {
    var pct = Math.max(0, Math.min(100, +host.getAttribute('data-pct') || 0)) / 100;
    var color = host.getAttribute('data-color') || '#2A7337';
    var W = 240, H = 200, cx = 120, cy = 92, R = 100, r = 64, tilt = 0.5, wall = 16;
    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, class: 'c3d-svg c3d-ring', role: 'img',
      'aria-label': Math.round(pct * 100) + '% of the way' }, host);
    function pt(a, rad, dz) { return [cx + rad * Math.cos(a), cy + rad * Math.sin(a) * tilt + (dz || 0)]; }
    function arc(a0, a1, rad, dz) {
      var out = [], steps = Math.max(2, Math.ceil(Math.abs(a1 - a0) / 0.05));
      for (var i = 0; i <= steps; i++) out.push(pt(a0 + (a1 - a0) * i / steps, rad, dz));
      return out;
    }
    function poly(points, attrs) {
      attrs.points = points.map(function (p) { return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' ');
      return el('polygon', attrs, svg);
    }
    function band(a0, a1, rad, fill) {   // a vertical wall along an edge
      if (a1 <= a0) return;
      var topE = arc(a0, a1, rad, 0), botE = arc(a1, a0, rad, wall);
      poly(topE.concat(botE), { fill: fill });
    }
    function face(a0, a1, fill) {         // the flat top of a sector
      if (a1 <= a0) return;
      poly(arc(a0, a1, R).concat(arc(a1, a0, r)), { fill: fill });
    }
    function clip(a0, a1, lo, hi) { return [Math.max(a0, lo), Math.min(a1, hi)]; }

    el('ellipse', { cx: cx, cy: cy + wall + 10, rx: R + 6, ry: (R + 6) * tilt * 0.9, class: 'c3d-shadow' }, svg);
    var start = -Math.PI / 2, end = start + Math.PI * 2 * pct, full = start + Math.PI * 2;
    var track = '#E7E9E0', trackSide = '#CBD0C3';
    // inner wall: the far half of the hole shows its inside
    band(Math.PI, Math.PI * 2, r, '#D9DDD0');
    var ib = clip(start + Math.PI * 2, end + Math.PI * 2, Math.PI, Math.PI * 2);   // progress part on the far inner wall
    var ib2 = clip(start, end, Math.PI, Math.PI * 2);
    band(ib2[0], ib2[1], r, shade(color, -0.45));
    band(ib[0], ib[1], r, shade(color, -0.45));
    // outer wall: the near half
    band(0, Math.PI, R, trackSide);
    var ob = clip(start, end, 0, Math.PI);
    band(ob[0], ob[1], R, shade(color, -0.3));
    // tops
    face(start, full, track);
    if (pct > 0) face(start, end, color);
    if (pct > 0) {
      var a = end - 0.0001, p1 = pt(a, R), p2 = pt(a, r);
      el('line', { x1: p1[0], y1: p1[1], x2: p2[0], y2: p2[1], stroke: shade(color, 0.4), 'stroke-width': 2 }, svg);
    }
    hoverable(svg, '<b>' + Math.round(pct * 100) + '%</b>' + (host.getAttribute('data-note') || ''));
  }

  var draw = { bars: bars, tank: tank, ring: ring };
  Array.prototype.forEach.call(document.querySelectorAll('[data-c3d]'), function (host) {
    var fn = draw[host.getAttribute('data-c3d')];
    if (fn) fn(host);
  });
  window.addEventListener('scroll', hideTip, { passive: true });
})();
