/* ==========================================================================
   gt-rail.js — the admin menu's collapse state.

   Applied to <html> from an inline snippet in base_site.html BEFORE the rail
   paints, not from here: setting the class after first paint makes the menu
   visibly jump from wide to narrow on every page load. This file only handles
   the click.
   ========================================================================== */
(function () {
  "use strict";

  var KEY = "gtAdminRailCollapsed";
  var root = document.documentElement;

  function set(collapsed) {
    root.classList.toggle("gta-rail-collapsed", collapsed);
    document.querySelectorAll("[data-rail-toggle]").forEach(function (btn) {
      btn.setAttribute("aria-expanded", String(!collapsed));
      var label = btn.querySelector(".lbl");
      if (label) label.textContent = collapsed ? "Expand menu" : "Collapse menu";
    });
    try { localStorage.setItem(KEY, collapsed ? "1" : "0"); } catch (e) { /* private mode */ }
    // The charts size themselves to their container, and collapsing the menu
    // gives them ~200px more of it. Nudge them once the width transition ends.
    setTimeout(function () { window.dispatchEvent(new Event("resize")); }, 220);
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-rail-toggle]");
    if (!btn) return;
    set(!root.classList.contains("gta-rail-collapsed"));
  });

  // Narrow screens start collapsed — 268px of menu on a laptop leaves the
  // charts too little to be worth drawing.
  if (window.matchMedia("(max-width: 1100px)").matches) {
    var stored = null;
    try { stored = localStorage.getItem(KEY); } catch (e) { /* ignore */ }
    if (stored === null) set(true);
  }
})();

/* ==========================================================================
   Two smaller behaviours that belong to the same chrome.
   ========================================================================== */
(function () {
  "use strict";

  /* ---- Which groups are open ---------------------------------------------
     Remembered per group, because the menu is a working surface and somebody
     who lives in Tipping should not have to re-open it on every page load.
     The server still opens the group holding the current page (the `open`
     attribute is rendered), so a stored "shut" never hides where you are. */
  var KEY = "gtAdminOpenSections";

  function stored() {
    try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { return {}; }
  }

  var state = stored();
  document.querySelectorAll("details.gta-sec[data-sec]").forEach(function (d) {
    var key = d.getAttribute("data-sec");
    if (!d.open && state[key] === true) d.open = true;
    d.addEventListener("toggle", function () {
      state = stored();
      state[key] = d.open;
      try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* private mode */ }
    });
  });

  /* ---- Selects that are really links --------------------------------------
     The dashboard's window picker is a GET form. Submitting it on change is
     what everyone expects from a control that looks like this; the <noscript>
     button next to it is what keeps it working when this never runs. */
  document.querySelectorAll("select[data-autosubmit]").forEach(function (sel) {
    sel.addEventListener("change", function () {
      if (sel.form) sel.form.submit();
    });
  });
})();
