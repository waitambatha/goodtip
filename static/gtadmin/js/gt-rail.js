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
