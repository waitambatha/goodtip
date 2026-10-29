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
   One smaller behaviour that belongs to the same chrome.
   ========================================================================== */
(function () {
  "use strict";

  /* ---- Which groups are open ---------------------------------------------
     Gone with the groups. The rail was ten <details> over the model tables and
     this remembered which of them you had left open, per group, so somebody
     who lived in Tipping did not have to re-open it on every page load.

     The rail is six flat links now — see templates/admin/base_site.html — so
     there is nothing left to remember. The `gtAdminOpenSections` key may still
     be sitting in somebody's localStorage; it is inert, costs a few bytes, and
     clearing it would mean shipping code whose only job is to delete something
     nothing reads. The rail's OTHER preference, whether it is collapsed, is
     `gtAdminRailCollapsed` and is still live at the top of this file. */

  /* ---- The dropdowns, with the menu collapsed -----------------------------
     A dropdown has nowhere to open into when the rail is only icons wide, so
     its row goes to the section's overview instead (the summary's data-go). */
  document.addEventListener("click", function (e) {
    var sum = e.target.closest && e.target.closest(".gta-tree > summary[data-go]");
    if (!sum || !document.documentElement.classList.contains("gta-rail-collapsed")) return;
    e.preventDefault();
    window.location.href = sum.getAttribute("data-go");
  });

  /* The tree opened at the page you are on: bring that row into view inside
     the menu's own scroll, so a table far down Staging's list is not hidden. */
  var on = document.querySelector(".gta-leaves .gta-leaf.on, .gta-twig .gta-twig-a.on");
  if (on && on.scrollIntoView) on.scrollIntoView({ block: "nearest" });

  /* ---- Previous / next page (admin/hub/_history.html) ------------------- */
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-hist]");
    if (!b || b.disabled) return;
    if (b.getAttribute("data-hist") === "back") history.back(); else history.forward();
  });
  var nav = window.navigation;
  function histState() {
    document.querySelectorAll("[data-hist]").forEach(function (b) {
      var back = b.getAttribute("data-hist") === "back";
      // The Navigation API knows; without it, assume back works once there
      // is history and leave forward available.
      var ok = nav && "canGoBack" in nav ? (back ? nav.canGoBack : nav.canGoForward) : (back ? history.length > 1 : true);
      b.disabled = !ok;
    });
  }
  histState();
  if (nav && nav.addEventListener) nav.addEventListener("currententrychange", histState);
  window.addEventListener("pageshow", histState);

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
