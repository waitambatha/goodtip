/* ==========================================================================
   gt-charts.js — the chart renderer for the GoodTip admin.

   Deliberately dependency-free. The admin is served by Django and WhiteNoise
   with a strict-ish asset story (hashed filenames, long immutable caching) and
   it is the one part of the site that has to keep working when the box has no
   outbound internet — pulling Chart.js or d3 off a CDN would put a third-party
   request in front of the superuser's own control plane. Everything drawn here
   is plain SVG built from the numbers Django already computed.

   Usage — a container plus the data next to it:

     <div class="gt-chart" id="c-activity"></div>
     <script type="application/json" data-chart-for="c-activity">
       {"type":"area","labels":[...],"series":[{"name":"...","data":[...]}]}
     </script>

   Charts re-render on resize (via ResizeObserver) rather than scaling a fixed
   viewBox, so axis labels stay at their real size instead of stretching.
   ========================================================================== */
(function () {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var SERIES_VARS = ["--gt-c1", "--gt-c2", "--gt-c3", "--gt-c4", "--gt-c5", "--gt-c6"];

  function el(name, attrs, parent) {
    var node = document.createElementNS(NS, name);
    if (attrs) {
      for (var k in attrs) {
        if (attrs[k] !== null && attrs[k] !== undefined) node.setAttribute(k, attrs[k]);
      }
    }
    if (parent) parent.appendChild(node);
    return node;
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function fmt(n) {
    if (n === null || n === undefined) return "—";
    if (Math.abs(n) >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, "") + "M";
    if (Math.abs(n) >= 10000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    return String(Math.round(n * 100) / 100);
  }

  /* A "nice" axis maximum: the smallest 1/2/5 x 10^n step that fits `count`
     gridlines over the data. Without this the y-axis lands on 37.4 and the
     reader has to do arithmetic to compare two charts. */
  function niceScale(max, count) {
    if (!isFinite(max) || max <= 0) return { max: 1, step: 1 };
    var raw = max / count;
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var norm = raw / mag;
    var step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
    return { max: Math.ceil(max / step) * step, step: step };
  }

  function seriesColor(s, i) {
    if (s.color) return s.color.charAt(0) === "-" ? cssVar(s.color) : s.color;
    return cssVar(SERIES_VARS[i % SERIES_VARS.length]);
  }

  /* ---- tooltip ---------------------------------------------------------- */
  function makeTip(host) {
    var tip = document.createElement("div");
    tip.className = "gt-tip";
    host.appendChild(tip);
    return {
      show: function (html, x, y) {
        tip.innerHTML = html;
        tip.classList.add("on");
        // Keep the bubble inside the card: past either edge it stops tracking
        // the pointer and pins to the edge instead of overflowing the panel.
        var w = tip.offsetWidth;
        var hw = w / 2;
        var maxX = host.clientWidth - hw - 4;
        tip.style.left = Math.max(hw + 4, Math.min(x, maxX)) + "px";
        tip.style.top = Math.max(tip.offsetHeight + 6, y - 12) + "px";
      },
      hide: function () { tip.classList.remove("on"); }
    };
  }

  function swatch(color) {
    return '<span class="sw" style="background:' + color + '"></span>';
  }

  /* ======================================================================
     LINE / AREA
     ====================================================================== */
  function drawLine(host, cfg) {
    var W = host.clientWidth || 600;
    var H = cfg.height || 240;
    var padL = 42, padR = 12, padT = 12, padB = 26;
    var iw = Math.max(10, W - padL - padR);
    var ih = Math.max(10, H - padT - padB);
    var labels = cfg.labels || [];
    var live = cfg.series.filter(function (s) { return !s._off; });

    var max = 0;
    live.forEach(function (s) {
      s.data.forEach(function (v) { if (v > max) max = v; });
    });
    var sc = niceScale(max, 4);

    var svg = el("svg", {
      width: W, height: H, viewBox: "0 0 " + W + " " + H,
      role: "img", "aria-label": cfg.summary || cfg.title || "Chart"
    });

    var x = function (i) {
      return labels.length < 2 ? padL + iw / 2 : padL + (i / (labels.length - 1)) * iw;
    };
    var y = function (v) { return padT + ih - (v / sc.max) * ih; };

    /* grid + y ticks */
    var grid = el("g", { class: "gt-grid" }, svg);
    var axis = el("g", { class: "gt-axis" }, svg);
    for (var v = 0; v <= sc.max + 1e-9; v += sc.step) {
      var gy = y(v);
      el("line", { x1: padL, y1: gy, x2: padL + iw, y2: gy }, grid);
      var t = el("text", { x: padL - 8, y: gy + 4, "text-anchor": "end" }, axis);
      t.textContent = fmt(v);
    }

    /* x labels — thinned so they never collide */
    var every = Math.max(1, Math.ceil(labels.length / Math.max(2, Math.floor(iw / 68))));
    labels.forEach(function (lab, i) {
      if (i % every !== 0 && i !== labels.length - 1) return;
      var xt = el("text", {
        x: x(i), y: H - 8,
        "text-anchor": i === 0 ? "start" : (i === labels.length - 1 ? "end" : "middle")
      }, axis);
      xt.textContent = lab;
    });

    /* series */
    live.forEach(function (s, si) {
      var color = seriesColor(s, cfg.series.indexOf(s));
      var g = el("g", { class: "gt-series" }, svg);
      var pts = s.data.map(function (val, i) { return x(i) + "," + y(val || 0); });

      if (cfg.type === "area") {
        var gid = "gtgrad-" + (cfg.uid || "0") + "-" + si;
        var defs = el("defs", null, g);
        var lg = el("linearGradient", { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
        el("stop", { offset: "0%", "stop-color": color, "stop-opacity": ".30" }, lg);
        el("stop", { offset: "100%", "stop-color": color, "stop-opacity": "0" }, lg);
        el("path", {
          d: "M" + x(0) + "," + (padT + ih) + " L" + pts.join(" L") + " L" + x(s.data.length - 1) + "," + (padT + ih) + " Z",
          fill: "url(#" + gid + ")", stroke: "none"
        }, g);
      }

      el("path", {
        d: "M" + pts.join(" L"),
        fill: "none", stroke: color, "stroke-width": 2.25,
        "stroke-linejoin": "round", "stroke-linecap": "round"
      }, g);

      s._dot = el("circle", {
        class: "gt-dot", r: 4.5, cx: -99, cy: -99,
        fill: cssVar("--gt-surface") || "#fff", stroke: color, "stroke-width": 2.5
      }, svg);
      s._color = color;
    });

    /* hover */
    var cross = el("line", { class: "gt-crosshair", x1: -9, y1: padT, x2: -9, y2: padT + ih }, svg);
    var hit = el("rect", { class: "gt-hit", x: padL, y: padT, width: iw, height: ih }, svg);
    host.appendChild(svg);

    var tip = host._tip || (host._tip = makeTip(host));

    function at(evt) {
      var box = svg.getBoundingClientRect();
      var px = (evt.touches ? evt.touches[0].clientX : evt.clientX) - box.left;
      var i = labels.length < 2 ? 0 : Math.round(((px - padL) / iw) * (labels.length - 1));
      i = Math.max(0, Math.min(labels.length - 1, i));
      host.classList.add("is-hover");
      cross.setAttribute("x1", x(i));
      cross.setAttribute("x2", x(i));
      var rows = "";
      var topY = padT + ih;
      live.forEach(function (s) {
        var val = s.data[i] || 0;
        s._dot.setAttribute("cx", x(i));
        s._dot.setAttribute("cy", y(val));
        topY = Math.min(topY, y(val));
        rows += '<div class="r"><span class="k">' + swatch(s._color) + s.name +
                '</span><span class="v">' + fmt(val) + "</span></div>";
      });
      tip.show("<b>" + (labels[i] || "") + "</b>" + rows, x(i), topY);
    }
    function off() {
      host.classList.remove("is-hover");
      tip.hide();
      live.forEach(function (s) { s._dot.setAttribute("cx", -99); });
    }
    hit.addEventListener("mousemove", at);
    hit.addEventListener("touchstart", at, { passive: true });
    hit.addEventListener("touchmove", at, { passive: true });
    hit.addEventListener("mouseleave", off);
    hit.addEventListener("touchend", off);
  }

  /* ======================================================================
     BARS (grouped or stacked)
     ====================================================================== */
  function drawBar(host, cfg) {
    var W = host.clientWidth || 600;
    var H = cfg.height || 240;
    var padL = 42, padR = 12, padT = 12, padB = 26;
    var iw = Math.max(10, W - padL - padR);
    var ih = Math.max(10, H - padT - padB);
    var labels = cfg.labels || [];
    var live = cfg.series.filter(function (s) { return !s._off; });
    var stacked = !!cfg.stacked;

    var max = 0;
    labels.forEach(function (_, i) {
      if (stacked) {
        var sum = 0;
        live.forEach(function (s) { sum += s.data[i] || 0; });
        if (sum > max) max = sum;
      } else {
        live.forEach(function (s) { if ((s.data[i] || 0) > max) max = s.data[i] || 0; });
      }
    });
    var sc = niceScale(max, 4);

    var svg = el("svg", {
      width: W, height: H, viewBox: "0 0 " + W + " " + H,
      role: "img", "aria-label": cfg.summary || cfg.title || "Chart"
    });
    var y = function (v) { return padT + ih - (v / sc.max) * ih; };

    var grid = el("g", { class: "gt-grid" }, svg);
    var axis = el("g", { class: "gt-axis" }, svg);
    for (var v = 0; v <= sc.max + 1e-9; v += sc.step) {
      el("line", { x1: padL, y1: y(v), x2: padL + iw, y2: y(v) }, grid);
      var t = el("text", { x: padL - 8, y: y(v) + 4, "text-anchor": "end" }, axis);
      t.textContent = fmt(v);
    }

    var slot = iw / Math.max(1, labels.length);
    var gap = Math.min(10, slot * 0.28);
    var bandW = Math.max(3, slot - gap);
    var barW = stacked ? bandW : Math.max(3, bandW / Math.max(1, live.length));

    var every = Math.max(1, Math.ceil(labels.length / Math.max(2, Math.floor(iw / 68))));
    var bars = el("g", null, svg);

    labels.forEach(function (lab, i) {
      var bx = padL + i * slot + gap / 2;
      if (i % every === 0 || i === labels.length - 1) {
        var xt = el("text", { x: bx + bandW / 2, y: H - 8, "text-anchor": "middle" }, axis);
        xt.textContent = lab;
      }
      var acc = 0;
      live.forEach(function (s, si) {
        var val = s.data[i] || 0;
        var color = seriesColor(s, cfg.series.indexOf(s));
        s._color = color;
        var h, ry;
        if (stacked) {
          h = (val / sc.max) * ih;
          ry = y(acc + val);
          acc += val;
        } else {
          h = (val / sc.max) * ih;
          ry = y(val);
        }
        if (h < 0.6 && val === 0) return;
        el("rect", {
          class: "gt-bar",
          x: stacked ? bx : bx + si * barW,
          y: ry,
          width: Math.max(1, barW - (stacked ? 0 : 1.5)),
          height: Math.max(1, h),
          rx: Math.min(3, barW / 3),
          fill: color,
          "data-i": i
        }, bars);
      });
    });

    var hit = el("rect", { class: "gt-hit", x: padL, y: padT, width: iw, height: ih }, svg);
    host.appendChild(svg);
    var tip = host._tip || (host._tip = makeTip(host));
    var allBars = bars.querySelectorAll(".gt-bar");

    function at(evt) {
      var box = svg.getBoundingClientRect();
      var px = (evt.touches ? evt.touches[0].clientX : evt.clientX) - box.left;
      var i = Math.floor((px - padL) / slot);
      if (i < 0 || i >= labels.length) return off();
      host.classList.add("is-hover");
      var rows = "", total = 0;
      live.forEach(function (s) {
        var val = s.data[i] || 0;
        total += val;
        rows += '<div class="r"><span class="k">' + swatch(s._color) + s.name +
                '</span><span class="v">' + fmt(val) + "</span></div>";
      });
      if (stacked && live.length > 1) {
        rows += '<div class="r"><span class="k">Total</span><span class="v">' + fmt(total) + "</span></div>";
      }
      allBars.forEach(function (b) {
        b.classList.toggle("dim", b.getAttribute("data-i") !== String(i));
      });
      tip.show("<b>" + (labels[i] || "") + "</b>" + rows, padL + i * slot + slot / 2, padT + 4);
    }
    function off() {
      host.classList.remove("is-hover");
      tip.hide();
      allBars.forEach(function (b) { b.classList.remove("dim"); });
    }
    hit.addEventListener("mousemove", at);
    hit.addEventListener("touchstart", at, { passive: true });
    hit.addEventListener("touchmove", at, { passive: true });
    hit.addEventListener("mouseleave", off);
    hit.addEventListener("touchend", off);
  }

  /* ======================================================================
     DONUT
     ====================================================================== */
  function drawDonut(host, cfg) {
    var W = host.clientWidth || 300;
    var H = cfg.height || 240;
    var cx = W / 2, cy = H / 2;
    var r = Math.max(30, Math.min(W, H) / 2 - 12);
    var inner = r * 0.62;
    var slices = (cfg.slices || []).filter(function (s) { return s.value > 0; });
    var total = slices.reduce(function (a, s) { return a + s.value; }, 0);

    var svg = el("svg", {
      width: W, height: H, viewBox: "0 0 " + W + " " + H,
      role: "img", "aria-label": cfg.summary || cfg.title || "Chart"
    });

    if (!total) {
      host.innerHTML = '<div class="gt-chart-empty">' + (cfg.empty || "Nothing to plot yet") + "</div>";
      return;
    }

    function pt(ang, rad) {
      return [cx + rad * Math.cos(ang), cy + rad * Math.sin(ang)];
    }

    var a0 = -Math.PI / 2;
    var arcs = [];
    slices.forEach(function (s, i) {
      var frac = s.value / total;
      // A full circle cannot be drawn as one arc — its start and end points are
      // identical, so the path collapses to nothing. Nudge it just short.
      var a1 = a0 + frac * Math.PI * 2 * (frac >= 0.9999 ? 0.9999 : 1);
      var color = s.color ? (s.color.charAt(0) === "-" ? cssVar(s.color) : s.color) : cssVar(SERIES_VARS[i % SERIES_VARS.length]);
      var large = (a1 - a0) > Math.PI ? 1 : 0;
      var p0 = pt(a0, r), p1 = pt(a1, r), p2 = pt(a1, inner), p3 = pt(a0, inner);
      var path = el("path", {
        class: "gt-arc",
        d: "M" + p0 + " A" + r + "," + r + " 0 " + large + " 1 " + p1 +
           " L" + p2 + " A" + inner + "," + inner + " 0 " + large + " 0 " + p3 + " Z",
        fill: color
      }, svg);
      s._color = color;
      arcs.push(path);
      a0 = a1;

      path.addEventListener("mouseenter", function () {
        arcs.forEach(function (p) { p.classList.add("dim"); });
        path.classList.remove("dim");
        tip.show("<b>" + s.label + '</b><div class="r"><span class="k">' + swatch(color) +
                 (cfg.unit || "Count") + '</span><span class="v">' + fmt(s.value) + "</span></div>" +
                 '<div class="r"><span class="k">Share</span><span class="v">' +
                 Math.round(frac * 1000) / 10 + "%</span></div>", cx, cy - r * 0.2);
      });
      path.addEventListener("mouseleave", function () {
        arcs.forEach(function (p) { p.classList.remove("dim"); });
        tip.hide();
      });
    });

    var centre = el("text", {
      x: cx, y: cy - 2, "text-anchor": "middle",
      fill: cssVar("--body-loud-color"),
      style: "font-family:var(--gt-num);font-weight:800;font-size:30px"
    }, svg);
    centre.textContent = fmt(cfg.centreValue !== undefined ? cfg.centreValue : total);
    var cap = el("text", {
      x: cx, y: cy + 18, "text-anchor": "middle",
      fill: cssVar("--body-quiet-color"),
      style: "font-family:var(--gt-body);font-size:11px;letter-spacing:.1em;text-transform:uppercase"
    }, svg);
    cap.textContent = cfg.centreLabel || "total";

    host.appendChild(svg);
    var tip = host._tip || (host._tip = makeTip(host));
  }

  /* ======================================================================
     Sparkline — the shape behind a stat tile

     No axes, no grid, no tooltip, and deliberately no scale: the tile already
     states the number, and what it cannot say is whether that number has been
     climbing all fortnight or spiked this morning. Drawn edge to edge so it
     reads as a texture rather than as a chart somebody forgot to label.
     ====================================================================== */
  function drawSpark(host, cfg) {
    var W = host.clientWidth || 140;
    var H = cfg.height || 46;
    var s = (cfg.series || [])[0];
    var data = (s && s.data) || [];
    if (data.length < 2) return;

    // One pixel of headroom top and bottom so a flat run and a peak are both
    // visible instead of being clipped against the edge.
    var padT = 5, padB = 3;
    var ih = Math.max(4, H - padT - padB);
    var max = Math.max.apply(null, data) || 1;
    var color = seriesColor(s, 0);
    var uid = "gtspark-" + (cfg.uid || "0");

    var svg = el("svg", {
      viewBox: "0 0 " + W + " " + H, width: W, height: H,
      preserveAspectRatio: "none", class: "gt-spark", "aria-hidden": "true",
    }, host);

    var x = function (i) { return (i / (data.length - 1)) * W; };
    var y = function (v) { return padT + ih - (v / max) * ih; };
    var pts = data.map(function (v, i) { return x(i) + "," + y(v || 0); });

    var defs = el("defs", null, svg);
    var lg = el("linearGradient", { id: uid, x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
    el("stop", { offset: "0%", "stop-color": color, "stop-opacity": ".30" }, lg);
    el("stop", { offset: "100%", "stop-color": color, "stop-opacity": "0" }, lg);

    el("polygon", {
      points: "0," + H + " " + pts.join(" ") + " " + W + "," + H,
      fill: "url(#" + uid + ")",
    }, svg);
    el("polyline", {
      points: pts.join(" "), fill: "none", stroke: color,
      "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round",
      "vector-effect": "non-scaling-stroke",
    }, svg);
    el("circle", {
      cx: x(data.length - 1), cy: y(data[data.length - 1] || 0), r: 2.6,
      fill: color, stroke: "var(--gt-surface)", "stroke-width": 1.5,
    }, svg);
  }

  /* ======================================================================
     Mount / legend / resize
     ====================================================================== */
  var mounted = [];

  function render(host, cfg) {
    host.innerHTML = "";
    host._tip = null;
    if (cfg.type === "donut") drawDonut(host, cfg);
    else if (cfg.type === "bar") drawBar(host, cfg);
    else if (cfg.type === "spark") drawSpark(host, cfg);
    else drawLine(host, cfg);
  }

  function mountLegend(host, cfg) {
    if (cfg.type === "donut") {
      if (cfg.legend === false) return;
      var dl = document.createElement("div");
      dl.className = "gt-legend";
      (cfg.slices || []).forEach(function (s, i) {
        var color = s.color ? (s.color.charAt(0) === "-" ? cssVar(s.color) : s.color) : cssVar(SERIES_VARS[i % SERIES_VARS.length]);
        var span = document.createElement("span");
        span.className = "gt-legend-item";
        span.innerHTML = '<button type="button" aria-pressed="true" disabled style="cursor:default">' +
          swatch(color) + "<span>" + s.label + '</span><span class="n">' + fmt(s.value) + "</span></button>";
        dl.appendChild(span.firstChild);
      });
      host.parentNode.insertBefore(dl, host.nextSibling);
      return;
    }
    if (cfg.legend === false || !cfg.series || cfg.series.length < 2) return;
    var wrap = document.createElement("div");
    wrap.className = "gt-legend";
    cfg.series.forEach(function (s, i) {
      var b = document.createElement("button");
      b.type = "button";
      b.setAttribute("aria-pressed", "true");
      var total = s.data.reduce(function (a, v) { return a + (v || 0); }, 0);
      b.innerHTML = swatch(seriesColor(s, i)) + "<span>" + s.name + '</span><span class="n">' + fmt(total) + "</span>";
      b.addEventListener("click", function () {
        // Never let the last visible series be switched off — an empty chart
        // with no way back is a dead end.
        var visible = cfg.series.filter(function (x) { return !x._off; });
        if (!s._off && visible.length === 1) return;
        s._off = !s._off;
        b.setAttribute("aria-pressed", String(!s._off));
        render(host, cfg);
      });
      wrap.appendChild(b);
    });
    host.parentNode.insertBefore(wrap, host.nextSibling);
  }

  function boot() {
    var uid = 0;
    document.querySelectorAll(".gt-chart[id]").forEach(function (host) {
      // Either an explicit data-chart-for tag, or the id Django's |json_script
      // filter produces when the config is built server-side.
      var src = document.querySelector('script[type="application/json"][data-chart-for="' + host.id + '"]')
             || document.getElementById(host.id + "-data");
      if (!src) return;
      var cfg;
      try { cfg = JSON.parse(src.textContent); } catch (e) { return; }
      cfg.uid = ++uid;
      cfg.series = cfg.series || [];

      var hasData = cfg.type === "donut"
        ? (cfg.slices || []).some(function (s) { return s.value > 0; })
        : cfg.series.some(function (s) { return (s.data || []).some(function (v) { return v; }); });
      if (!hasData) {
        host.innerHTML = '<div class="gt-chart-empty">' + (cfg.empty || "No activity in this window yet") + "</div>";
        return;
      }

      render(host, cfg);
      mountLegend(host, cfg);
      mounted.push({ host: host, cfg: cfg });

      if (window.ResizeObserver) {
        var w = host.clientWidth, t;
        new ResizeObserver(function () {
          if (Math.abs(host.clientWidth - w) < 12) return;
          w = host.clientWidth;
          clearTimeout(t);
          t = setTimeout(function () { render(host, cfg); }, 90);
        }).observe(host);
      }
    });

  }

  /* The admin's light/dark toggle swaps the CSS variables the charts sampled at
     draw time, so every mounted chart is redrawn — re-running boot() would
     append a second legend to each one. */
  function redrawAll() {
    mounted.forEach(function (m) { render(m.host, m.cfg); });
  }
  window.addEventListener("gt:theme", redrawAll);
  document.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest(".theme-toggle")) setTimeout(redrawAll, 30);
  });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
