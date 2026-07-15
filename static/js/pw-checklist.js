/* GoodTip live password-requirements checklist.
   Wires every .pw-checks[data-pw-checks] list to its password input (and
   optional confirm input) and ticks items green as rules are met. Mirrors
   accounts.validators.PasswordComplexityValidator — progressive enhancement
   only, the server always re-validates. Also drives an adjacent #pwbar /
   .pw-meter strength bar if the page has one. */
(function () {
  "use strict";

  var RULES = {
    len: function (v) { return v.length >= 8; },
    upper: function (v) { return /[A-Z]/.test(v); },
    lower: function (v) { return /[a-z]/.test(v); },
    digit: function (v) { return /[0-9]/.test(v); },
    symbol: function (v) { return /[^A-Za-z0-9]/.test(v); },
  };

  function wire(list) {
    var input = document.getElementById(list.getAttribute("data-pw-checks"));
    if (!input) return;
    var confirm = document.getElementById(list.getAttribute("data-pw-confirm") || "");
    var items = list.querySelectorAll("[data-req]");
    var bar = document.getElementById("pwbar");

    function render() {
      var v = input.value;
      var met = 0, total = 0;
      items.forEach(function (li) {
        var key = li.getAttribute("data-req");
        var ok;
        if (key === "match") {
          ok = !!v && !!confirm && confirm.value === v;
        } else {
          ok = !!v && !!RULES[key] && RULES[key](v);
        }
        li.classList.toggle("ok", ok);
        total++;
        if (ok) met++;
      });
      list.classList.toggle("all-ok", met === total && total > 0);
      if (bar) {
        bar.style.width = (v ? Math.round((met / total) * 100) : 0) + "%";
        bar.style.background = met < 3 ? "#d98b2b" : met < total ? "#C8F135" : "#2D7A3A";
      }
    }

    input.addEventListener("input", render);
    if (confirm) confirm.addEventListener("input", render);
    render();
  }

  function init() {
    document.querySelectorAll("[data-pw-checks]").forEach(wire);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
