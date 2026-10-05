(function () {
  /* ?motion=1 (base.html) sets html.gt-motion so the full movement can be
     reviewed on a machine with the OS setting on. */
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches &&
    !document.documentElement.classList.contains('gt-motion');
  function wait(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

  /* ---- the two letters, one spot, taking turns ----
     1 Oct 2026 (client): only ever one on screen, the other one never above
     or below it, and "5 different slick ways" of swapping. Both halves sit in
     the same grid cell (CSS); each change uses the next entrance in FX, so a
     full lap shows all five. Reduced motion still swaps — just without the
     movement — because a stacked pair is exactly what the client rejected. */
  (function () {
    var deck = document.querySelector('[data-letter-rotate]');
    if (!deck) return;
    var halves = [].slice.call(deck.querySelectorAll('.ttb-half'));
    if (halves.length < 2) return;
    var dots = [].slice.call(document.querySelectorAll('.ttb-rotate-dots i'));
    var label = document.querySelector('[data-deck-label]');
    /* spin, push and crush were asked for by name (1 Oct 2026); the first
       five stay in the lap after them. */
    var FX = ['spin', 'push', 'crush', 'slide', 'flip', 'zoom', 'rise', 'fade'];
    var current = 0, fx = 0, paused = false;

    deck.classList.add('rotating');

    function show(next) {
      var out = halves[current], inn = halves[next];
      /* Reduced motion (an OS setting — KDE's "Instant" animation speed sets
         it) gets a cross-fade every time: the change still shows, nothing
         travels. Everyone else walks the full list. */
      var name = reduced ? 'fade' : FX[fx % FX.length];
      fx += 1;
      halves.forEach(function (h) { h.className = h.className.replace(/\bfx-\S+/g, '').trim(); });
      out.classList.add('fx-out-' + name);
      inn.classList.add('fx-in-' + name);
      out.classList.remove('is-on');
      inn.classList.add('is-on');
      dots.forEach(function (d, i) { d.classList.toggle('on', i === next); });
      if (label) label.innerHTML = inn.getAttribute('data-label') || '';
      current = next;
    }

    deck.addEventListener('mouseenter', function () { paused = true; });
    deck.addEventListener('mouseleave', function () { paused = false; });
    setInterval(function () {
      if (paused || document.hidden) return;
      show((current + 1) % halves.length);
    }, 4600);
  })();

  /* ---- the email, sent and answered ----
     1 Oct 2026 (client): "a real email interface ... see an email been sent
     on gmail, see the boss getting it". Five steps on a loop, no inputs —
     write, send, delivered, opened, reply — and it only runs while on screen. */
  (function () {
    var demo = document.querySelector('[data-mail-demo]');
    if (!demo) return;

    var scenes = {};
    [].forEach.call(demo.querySelectorAll('[data-mx-scene]'), function (el) {
      scenes[el.getAttribute('data-mx-scene')] = el;
    });
    var steps = [].slice.call(demo.querySelectorAll('[data-mx-step]'));
    var where = demo.querySelector('[data-mx-where]');
    var to = demo.querySelector('[data-mx-type]');
    var toast = demo.querySelector('[data-mx-toast]');
    var fresh = demo.querySelector('[data-mx-new]');
    var reply = demo.querySelector('[data-mx-reply]');
    var replyText = demo.querySelector('[data-mx-type-reply]');

    var visible = false;
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (entries) {
        visible = entries[0].isIntersecting;
      }, { threshold: 0.25 }).observe(demo);
    } else {
      visible = true;
    }
    async function onScreen() { while (!visible || document.hidden) await wait(400); }

    function scene(name) {
      Object.keys(scenes).forEach(function (k) { scenes[k].classList.toggle('is-on', k === name); });
    }
    /* Each tab carries a loader that fills over exactly as long as its step
       runs, so `ms` is that step's real running time (see the loop below).
       Re-adding .is-on on the next frame restarts the fill from empty. */
    function step(n, ms) {
      steps.forEach(function (s, i) {
        s.classList.remove('is-on');
        s.classList.toggle('is-done', i < n);
      });
      demo.setAttribute('data-on', n);
      var li = steps[n];
      li.style.setProperty('--mx-dur', ms + 'ms');
      void li.offsetWidth;
      li.classList.add('is-on');
    }
    async function type(el, text, speed) {
      if (reduced) { el.textContent = text; return; }
      el.textContent = '';
      for (var i = 0; i < text.length; i++) {
        el.textContent += text.charAt(i);
        await wait(speed);
      }
    }

    async function loop() {
      for (;;) {
        await onScreen();
        // 1. write
        demo.classList.remove('mx-pressing');
        toast.classList.remove('is-on');
        fresh.classList.remove('is-on', 'is-hot');
        reply.classList.remove('is-on');
        replyText.textContent = '';
        to.textContent = '';
        where.textContent = 'Your mail · sam@acme.com.au';
        var toText = to.getAttribute('data-mx-type');
        scene('compose'); step(0, 500 + (reduced ? 0 : toText.length * 45) + 700);
        await wait(500);
        await type(to, toText, 45);
        await wait(700);

        // 2. send
        step(1, 2650);
        demo.classList.add('mx-aiming');
        await wait(900);
        demo.classList.add('mx-pressing');
        await wait(250);
        demo.classList.remove('mx-aiming', 'mx-pressing');
        toast.classList.add('is-on');
        await wait(1500);

        // 3. delivered
        await onScreen();
        where.textContent = 'Priya’s inbox · priya.sharma@acme.com.au';
        scene('inbox'); step(2, 2550);
        await wait(450);
        fresh.classList.add('is-on');
        await wait(1300);
        fresh.classList.add('is-hot');
        await wait(800);

        // 4. opened
        scene('read'); step(3, 2200);
        await wait(2200);

        // 5. reply
        var replyMsg = replyText.getAttribute('data-mx-type-reply');
        step(4, 400 + (reduced ? 0 : replyMsg.length * 32) + 3200);
        reply.classList.add('is-on');
        await wait(400);
        await type(replyText, replyMsg, 32);
        await wait(3200);
      }
    }
    loop();
  })();

  /* ---- the Tell the Boss flow ----
     One button opens it. Step one asks how: we send it (six fields) or they
     do (a template to copy). Opens straight onto the send step when the page
     comes back with an error, so the visitor lands on their own fields. */
  (function () {
    var flow = document.querySelector('[data-tb-flow]');
    if (!flow) return;
    var title = flow.querySelector('[data-tb-title]');
    var back = flow.querySelector('[data-tb-back]');
    var stepEls = [].slice.call(flow.querySelectorAll('[data-tb-step]'));
    var TITLES = { choose: 'Tell the Boss', auto: 'We send it for you', manual: 'Send it yourself' };
    var opener = null;

    function go(name) {
      stepEls.forEach(function (el) { el.hidden = el.getAttribute('data-tb-step') !== name; });
      title.textContent = TITLES[name] || TITLES.choose;
      back.hidden = name === 'choose';
      var first = flow.querySelector('[data-tb-step="' + name + '"] input:not([readonly]):not([tabindex="-1"]), [data-tb-step="' + name + '"] button');
      if (first) setTimeout(function () { first.focus(); }, 30);
    }
    function open(start) {
      opener = document.activeElement;
      flow.hidden = false;
      document.documentElement.classList.add('tbf-open');
      go(start || 'choose');
    }
    function close() {
      flow.hidden = true;
      document.documentElement.classList.remove('tbf-open');
      if (opener && opener.focus) opener.focus();
    }

    document.querySelectorAll('[data-tb-open]').forEach(function (btn) {
      btn.addEventListener('click', function () { open(); });
    });
    flow.querySelectorAll('[data-tb-go]').forEach(function (btn) {
      btn.addEventListener('click', function () { go(btn.getAttribute('data-tb-go')); });
    });
    flow.querySelectorAll('[data-tb-close]').forEach(function (btn) {
      btn.addEventListener('click', close);
    });
    back.addEventListener('click', function () { go('choose'); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !flow.hidden) close();
    });

    var start = flow.getAttribute('data-tb-start');
    if (start) open(start);
  })();
})();
