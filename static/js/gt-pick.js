/* The quote break: four organisations, four Charity Votes.

   Each coloured card opens into a wide panel, its members vote, one charity
   wins, and the panel closes back to the card, which now says who it is
   supporting. Then the next organisation. Loops while on screen. Clicking a
   card runs that organisation's vote next.

   The markup is the finished picture: every card already supporting its
   winner, every bar at its result. That is what shows with JavaScript off or
   with reduced motion (the site's own gate, which ?motion=1 overrides). This
   script takes it apart and plays it back. Cancelling is a token bump, as in
   gt-how.js. */
(function () {
  'use strict';

  var sec = document.querySelector('[data-qb]');
  if (!sec) return;

  var forced = document.documentElement.classList.contains('gt-motion');
  if (!forced && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  var stage = sec.querySelector('.qb-stage');
  var strip = sec.querySelector('.qb-orgs');
  var orgs = [].slice.call(sec.querySelectorAll('.qb-org'));
  var panels = [].slice.call(sec.querySelectorAll('.qb-panel'));
  var l2 = sec.querySelector('.qb-l2');
  var token = 0, inView = false, running = false;

  function wait(ms, t) { return new Promise(function (res) { setTimeout(function () { res(t === token); }, ms); }); }

  function place(el, r) {
    el.style.left = r.x + 'px'; el.style.top = r.y + 'px';
    el.style.width = r.w + 'px'; el.style.height = r.h + 'px';
  }
  function cardRect(k) {
    var s = stage.getBoundingClientRect(), c = orgs[k].getBoundingClientRect();
    return { x: c.left - s.left, y: c.top - s.top, w: c.width, h: c.height };
  }
  var TOTAL = 50;

  function rowsOf(p) { return [].slice.call(p.querySelectorAll('.qb-row')); }
  function bars(p, on) {
    [].forEach.call(p.querySelectorAll('.qb-bar i'), function (i) { i.style.width = on ? '' : '0'; });
  }
  /* what each row's figure reads at rest, kept so the vote can end exactly on it */
  panels.forEach(function (p) {
    rowsOf(p).forEach(function (r) { r.__pct = r.querySelector('em').textContent; });
  });

  /* split the 50 votes between the rows in proportion to their result, then
     deal them out in a fixed shuffled order so the poll tugs back and forth */
  function ballots(p) {
    var rows = rowsOf(p), sum = 0;
    var n = rows.map(function (r) { var v = +r.getAttribute('data-v'); sum += v; return v; });
    var c = n.map(function (v) { return Math.floor(v / sum * TOTAL); });
    var left = TOTAL - c.reduce(function (a, b) { return a + b; }, 0);
    for (var i = 0; left > 0; i = (i + 1) % c.length, left--) c[i]++;
    var deck = [];
    c.forEach(function (k, ri) { for (var j = 0; j < k; j++) deck.push(ri); });
    var seed = 7 + rows.length * 13 + sum;
    for (var a = deck.length - 1; a > 0; a--) {
      seed = (seed * 9301 + 49297) % 233280;
      var b = Math.floor(seed / 233280 * (a + 1)), t = deck[a]; deck[a] = deck[b]; deck[b] = t;
    }
    return deck;
  }
  function paint(p, tally, cast) {
    rowsOf(p).forEach(function (r, i) {
      var pct = cast ? Math.round(tally[i] / cast * 100) : 0;
      r.querySelector('em').textContent = pct + '%';
      r.querySelector('.qb-bar i').style.width = pct + '%';
    });
    p.querySelector('[data-count]').textContent = cast;
  }
  function settle(p) {                           /* land on the resting figures */
    rowsOf(p).forEach(function (r) {
      r.querySelector('em').textContent = r.__pct;
      r.querySelector('.qb-bar i').style.width = '';
    });
    p.querySelector('[data-count]').textContent = TOTAL;
  }
  function clear() {
    strip.classList.remove('dim');
    orgs.forEach(function (o) { o.classList.remove('busy', 'voted'); });
    panels.forEach(function (p) {
      p.classList.remove('show', 'done', 'fly', 'closed', 'won', 'ask', 'press', 'sent', 'counting');
      rowsOf(p).forEach(function (r) { r.classList.remove('sel', 'hov'); });
      p.querySelector('.qb-cursor').classList.remove('show');
      settle(p);
      p.style.visibility = ''; p.style.left = p.style.top = p.style.width = p.style.height = '';
      bars(p, false);
    });
  }

  async function vote(k, t) {
    var p = panels[k], o = orgs[k];
    p.classList.remove('done', 'show', 'closed', 'won', 'ask', 'press', 'sent', 'counting');
    rowsOf(p).forEach(function (r) { r.classList.remove('sel', 'hov'); });
    paint(p, [0, 0, 0], 0);
    o.classList.remove('voted');
    /* open: from the card's own box to the whole stage */
    p.classList.add('fly');
    place(p, cardRect(k));
    p.style.visibility = 'visible';
    void p.offsetWidth;
    o.classList.add('busy');
    strip.classList.add('dim');
    p.classList.remove('fly');
    place(p, { x: 0, y: 0, w: stage.offsetWidth, h: stage.offsetHeight });
    if (!(await wait(900, t))) return false;
    p.classList.add('show');
    if (!(await wait(450, t))) return false;
    /* the ballot: a pointer looks over the list, picks a charity, confirms */
    var rows = rowsOf(p), cur = p.querySelector('.qb-cursor'), pick = p.querySelector('.qb-row.win');
    var decoy = rows.filter(function (r) { return r !== pick; })[0];
    function pt(el, fx, fy) {
      var pr = p.getBoundingClientRect(), r = el.getBoundingClientRect();
      cur.style.transform = 'translate(' + (r.left - pr.left + r.width * fx) + 'px,' + (r.top - pr.top + r.height * fy) + 'px)';
    }
    function tap() { cur.classList.remove('tap'); void cur.offsetWidth; cur.classList.add('tap'); }
    cur.style.transition = 'none';
    cur.style.transform = 'translate(' + (p.offsetWidth - 70) + 'px,' + (p.offsetHeight - 40) + 'px)';
    void cur.offsetWidth; cur.style.transition = '';
    cur.classList.add('show');
    if (!(await wait(700, t))) return false;
    pt(decoy, .6, .5); decoy.classList.add('hov');            /* a look at one... */
    if (!(await wait(1100, t))) return false;
    decoy.classList.remove('hov');
    pt(pick, .55, .5); pick.classList.add('hov');              /* ...then the one they want */
    if (!(await wait(1000, t))) return false;
    tap(); pick.classList.remove('hov'); pick.classList.add('sel');
    if (!(await wait(650, t))) return false;
    p.classList.add('ask');
    if (!(await wait(450, t))) return false;
    pt(p.querySelector('.qb-confirm'), .55, .55);
    if (!(await wait(1000, t))) return false;
    tap(); p.classList.add('press');
    if (!(await wait(160, t))) return false;
    p.classList.remove('press'); p.classList.add('sent');
    if (!(await wait(800, t))) return false;
    cur.classList.remove('show');
    if (!(await wait(250, t))) return false;

    /* the quick poll: the room's votes land fast and the bars race */
    p.classList.add('counting');
    var deck = ballots(p), tally = rows.map(function () { return 0; });
    for (var v = 0; v < deck.length; v++) {
      var ri = deck[v];
      tally[ri]++;
      paint(p, tally, v + 1);
      if (!(await wait(v < 36 ? 34 : 60 + (v - 36) * 14, t))) return false;
    }
    settle(p);
    if (!(await wait(450, t))) return false;
    p.classList.add('closed', 'done');           /* voting closed, one row ahead */
    if (!(await wait(1300, t))) return false;
    p.classList.add('won');                      /* and the announcement */
    if (!(await wait(2600, t))) return false;
    /* close: back into the card, which now names its charity */
    p.classList.remove('show', 'won');
    if (!(await wait(250, t))) return false;
    place(p, cardRect(k));
    o.classList.add('voted');
    if (!(await wait(850, t))) return false;
    p.style.visibility = '';
    o.classList.remove('busy');
    strip.classList.remove('dim');
    return true;
  }

  async function play(t, first) {
    for (var k = first || 0; ; k = (k + 1) % orgs.length) {
      if (k === 0) { clear(); void stage.offsetWidth; if (!(await wait(500, t))) return; }
      if (!(await vote(k, t))) return;
      l2.classList.add('in');
      if (!(await wait(650, t))) return;
      if (k === orgs.length - 1 && !(await wait(2200, t))) return;
    }
  }

  function begin(first) {
    var t = ++token;
    running = true;
    if (first) { clear(); }
    play(t, first).then(function () { if (t === token) running = false; });
  }
  function halt() { token++; running = false; }

  sec.classList.add('qb-js');
  clear();

  orgs.forEach(function (o, k) {
    o.style.cursor = 'pointer';
    o.setAttribute('tabindex', '0'); o.setAttribute('role', 'button');
    o.setAttribute('aria-label', 'Watch ' + o.querySelector('b').textContent + ' hold its vote');
    function go() { if (inView) { token++; clear(); begin(k); } }
    o.addEventListener('click', go);
    o.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
  });

  if (!('IntersectionObserver' in window)) { begin(0); return; }
  new IntersectionObserver(function (es) {
    inView = es[0].isIntersecting;
    if (inView && !running) begin(0);
    else if (!inView) { halt(); clear(); }
  }, { threshold: 0.4 }).observe(sec);
})();
