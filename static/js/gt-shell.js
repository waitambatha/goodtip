/* Collapse state for the /manage/ and HQ rail. The class itself is applied
   before paint by an inline snippet in _gt_shell_head.html; this only handles
   the click and remembers the answer. */
(function () {
  "use strict";
  var KEY = "gtShellCollapsed";
  var root = document.documentElement;

  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-shell-toggle]");
    if (!btn) return;
    var collapsed = !root.classList.contains("gts-collapsed");
    root.classList.toggle("gts-collapsed", collapsed);
    btn.setAttribute("aria-expanded", String(!collapsed));
    var label = btn.querySelector(".lbl");
    if (label) label.textContent = collapsed ? "Expand menu" : "Collapse menu";
    try { localStorage.setItem(KEY, collapsed ? "1" : "0"); } catch (err) {}
  });
})();
