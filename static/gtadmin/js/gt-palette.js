/* ==========================================================================
   gt-palette.js — the bar's jump box.

   WHAT IT SEARCHES, AND WHY THAT IS ALL. The index is built from the menu that
   is already on the page: every screen, every table, every HQ tool, each one
   tagged `data-jump` by the template. It does not search records.

   That is a deliberate stop. A box that looks like it finds a member by email
   but in fact only matches the word "Members" is worse than no box, and a real
   record search across twenty-five models is a server feature with permission
   questions of its own — not something to fake in the chrome. So the
   placeholder says "jump to", and the results are screens.

   Because the index comes from the rendered menu, it is filtered by permission
   for free: a link this user was never shown is a link they cannot jump to.
   ========================================================================== */
(function () {
  "use strict";

  var box = document.querySelector("[data-palette]");
  if (!box) return;

  var input = box.querySelector("input");
  var list = box.querySelector(".gta-palette");
  if (!input || !list) return;

  var items = [];
  document.querySelectorAll("#nav-sidebar [data-jump]").forEach(function (a) {
    var label = a.getAttribute("data-jump") || "";
    if (!label || !a.getAttribute("href")) return;
    items.push({
      label: label,
      href: a.getAttribute("href"),
      // The part before the chevron is the group, and it is worth matching on
      // its own: somebody who types "wall" should find both wall tables even
      // though neither is called that.
      hay: label.toLowerCase(),
    });
  });
  if (!items.length) return;

  var results = [];
  var active = -1;

  /* Ranked, not merely filtered. A prefix match on the visible name is what
     the person meant; a match buried in the group name is a fallback. */
  function score(item, q) {
    var i = item.hay.indexOf(q);
    if (i === -1) return -1;
    var tail = item.hay.split("›").pop().trim();
    if (tail.indexOf(q) === 0) return 0;
    if (i === 0) return 1;
    return 2 + i;
  }

  function close() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    active = -1;
  }

  function paint(q) {
    if (!q) return close();

    results = items
      .map(function (it) { return { it: it, s: score(it, q) }; })
      .filter(function (r) { return r.s >= 0; })
      .sort(function (a, b) { return a.s - b.s; })
      .slice(0, 8)
      .map(function (r) { return r.it; });

    list.textContent = "";
    if (!results.length) {
      var none = document.createElement("li");
      none.className = "gta-palette-none";
      none.textContent = 'Nothing in the menu matches "' + q + '".';
      list.appendChild(none);
    } else {
      results.forEach(function (item, i) {
        var li = document.createElement("li");
        var a = document.createElement("a");
        a.href = item.href;
        a.setAttribute("role", "option");
        a.textContent = item.label;
        if (i === 0) a.className = "on";
        li.appendChild(a);
        list.appendChild(li);
      });
      active = 0;
    }
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  function highlight(next) {
    var links = list.querySelectorAll("a");
    if (!links.length) return;
    if (active >= 0 && links[active]) links[active].classList.remove("on");
    active = (next + links.length) % links.length;
    links[active].classList.add("on");
    links[active].scrollIntoView({ block: "nearest" });
  }

  input.addEventListener("input", function () {
    paint(input.value.trim().toLowerCase());
  });

  input.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      if (list.hidden) input.blur();
      else close();
      return;
    }
    if (list.hidden) return;
    if (e.key === "ArrowDown") { e.preventDefault(); highlight(active + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); highlight(active - 1); }
    else if (e.key === "Enter") {
      var links = list.querySelectorAll("a");
      if (links.length && active >= 0) {
        e.preventDefault();
        window.location.href = links[active].href;
      }
    }
  });

  document.addEventListener("click", function (e) {
    if (!box.contains(e.target)) close();
  });

  /* "/" the way every other tool spells it, and Ctrl/Cmd-K for the people who
     learned it somewhere else. Never while a field already has focus — typing
     a slash into a search filter must put a slash there. */
  document.addEventListener("keydown", function (e) {
    var typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)
      || document.activeElement.isContentEditable;
    var chord = (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k";
    if (chord || (e.key === "/" && !typing)) {
      e.preventDefault();
      input.focus();
      input.select();
    }
  });
})();
