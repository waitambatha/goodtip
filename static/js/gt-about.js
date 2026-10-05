/* About Us: cards that take turns, and the founder's entrance.

   2 Oct 2026 (client): "it should expand and show some nice animation …
   then it contracts and moves to the next — it should not be that dry".

   [data-turns] is a row of [data-turn] cards. While the row is on screen one
   card at a time is lit: it widens (CSS, flex-grow), runs its entrance
   (data-fx → .fx-<name>, restarted every time it lights) and shows its scene,
   holds for data-hold ms, then the next card goes. Hovering the row pauses
   it; clicking or Enter on a card lights that card now. Off screen it stops
   and lets go, and starts again from the first card when it comes back.

   data-fx="qa" cards are a little chat: the question goes up, typing dots,
   then the answer is typed out, and the card holds for data-hold after the
   last character — so a long answer is never cut off mid-sentence. The
   answer is real text in the markup; this only types it, and puts the
   original markup (links and all) back when it finishes.

   [data-founder] adds .is-in once, the first time the section is properly on
   screen: the photo slides in from the left, the letter rises from below.

   Reduced motion (unless ?motion=1 set html.gt-motion) still takes turns —
   that is content changing, not travel — but nothing spins, and answers
   appear whole instead of being typed. */
(function () {
  'use strict';

  var root = document.documentElement;
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches &&
    !root.classList.contains('gt-motion');

  function wait(ms) { return new Promise(function (res) { setTimeout(res, ms); }); }

  /* ---- the founder ------------------------------------------------------ */
  var founder = document.querySelector('[data-founder]');
  if (founder) {
    founder.classList.add('fd-armed');
    if ('IntersectionObserver' in window) {
      var fio = new IntersectionObserver(function (entries) {
        if (entries[0].isIntersecting) { founder.classList.add('is-in'); fio.disconnect(); }
      }, { threshold: 0.25 });
      fio.observe(founder);
    } else {
      founder.classList.add('is-in');
    }
  }

  /* ---- the rows that take turns ----------------------------------------- */
  [].forEach.call(document.querySelectorAll('[data-turns]'), function (row) {
    var cards = [].slice.call(row.querySelectorAll('[data-turn]'));
    if (!cards.length) return;
    var hold = parseInt(row.getAttribute('data-hold'), 10) || 4800;

    row.classList.add('ab-on');
    cards.forEach(function (c) {
      c.setAttribute('tabindex', '0');
      c.setAttribute('role', 'button');
    });

    var token = 0, current = -1, inView = false, hovering = false, running = false;

    /* The chat card: snapshot the answer's markup once, type its text, then
       restore the markup so links come back. */
    cards.forEach(function (c) {
      var a = c.querySelector('[data-answer]');
      if (a) { a.__html = a.innerHTML; a.__text = a.textContent; }
    });

    async function typeAnswer(card, t) {
      var a = card.querySelector('[data-answer]');
      if (!a) return true;
      card.classList.remove('qa-asked', 'qa-typing-on', 'qa-answered');
      a.textContent = '';
      if (reduced) {
        a.innerHTML = a.__html;
        card.classList.add('qa-asked', 'qa-answered');
        return true;
      }
      await wait(250);                         if (t !== token) return false;
      card.classList.add('qa-asked');
      await wait(650);                         if (t !== token) return false;
      card.classList.add('qa-typing-on');
      await wait(1100);                        if (t !== token) return false;
      card.classList.remove('qa-typing-on');
      card.classList.add('qa-answered');
      var text = a.__text;
      for (var i = 0; i < text.length; i += 2) {
        if (t !== token) return false;
        a.textContent = text.slice(0, i + 2);
        await wait(22);
      }
      a.innerHTML = a.__html;
      return true;
    }

    function light(n) {
      current = n;
      row.classList.add('has-lit');
      cards.forEach(function (c, i) {
        var on = i === n;
        c.classList.toggle('is-lit', on);
        c.setAttribute('aria-expanded', on ? 'true' : 'false');
        c.className = c.className.replace(/\bfx-\S+/g, '').replace(/\s+/g, ' ').trim();
        if (!on) {
          c.classList.remove('qa-asked', 'qa-typing-on', 'qa-answered');
          var a = c.querySelector('[data-answer]');
          if (a && a.__html) a.innerHTML = a.__html;
        }
      });
      var card = cards[n];
      var fx = card.getAttribute('data-fx');
      if (fx && !reduced) {
        void card.offsetWidth;                 // restart the entrance
        card.classList.add('fx-' + fx);
      }
    }

    function letGo() {
      row.classList.remove('has-lit');
      cards.forEach(function (c) {
        c.classList.remove('is-lit', 'qa-asked', 'qa-typing-on', 'qa-answered');
        c.className = c.className.replace(/\bfx-\S+/g, '').replace(/\s+/g, ' ').trim();
        var a = c.querySelector('[data-answer]');
        if (a && a.__html) a.innerHTML = a.__html;
      });
      current = -1;
    }

    async function run(t, first) {
      running = true;
      for (var n = first; ; n = (n + 1) % cards.length) {
        if (t !== token) return;
        light(n);
        if (!(await typeAnswer(cards[n], t))) return;
        var left = hold;
        while (left > 0) {                     // hovering holds the clock
          await wait(200);
          if (t !== token) return;
          if (!hovering && !document.hidden) left -= 200;
        }
      }
    }

    function start(first) {
      var t = ++token;
      run(t, first || 0).then(function () { if (t === token) running = false; });
    }
    function stop() { token++; running = false; letGo(); }

    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (entries) {
        var was = inView;
        inView = entries[0].isIntersecting;
        if (inView && !was && !running) start(0);
        if (!inView && was) stop();
      }, { threshold: 0.35 }).observe(row);
    } else {
      start(0);
    }

    row.addEventListener('mouseenter', function () { hovering = true; });
    row.addEventListener('mouseleave', function () { hovering = false; });
    cards.forEach(function (c, i) {
      function pick(e) {
        if (e.target.closest && e.target.closest('a')) return;   // let links work
        if (i === current) return;
        start(i);
      }
      c.addEventListener('click', pick);
      c.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(e); }
      });
    });
  });
})();
