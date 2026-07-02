/* GoodTip auth enhancements: password reveal, strength meter, generator.
   Progressive enhancement — the forms work fine with JS disabled. */
(function () {
  "use strict";

  /* ---- Wrap a password input in a relative group with a reveal button ---- */
  function enhanceReveal(input) {
    if (input.dataset.pwRevealReady) return;
    input.dataset.pwRevealReady = "1";

    var group = document.createElement("div");
    group.className = "input-group";
    input.parentNode.insertBefore(group, input);
    group.appendChild(input);

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pw-toggle";
    btn.setAttribute("aria-label", "Show password");
    btn.innerHTML = '<i data-lucide="eye"></i>';
    group.appendChild(btn);

    btn.addEventListener("click", function () {
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
      btn.innerHTML = '<i data-lucide="' + (show ? "eye-off" : "eye") + '"></i>';
      if (window.lucide) lucide.createIcons();
      input.focus();
    });
  }

  /* ---- Strength scoring: 0..4 ---- */
  function scorePassword(pw) {
    if (!pw) return { score: 0, label: "" };
    var score = 0;
    if (pw.length >= 8) score++;
    if (pw.length >= 12) score++;
    if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) score++;
    if (/\d/.test(pw)) score++;
    if (/[^A-Za-z0-9]/.test(pw)) score++;
    if (pw.length < 8) score = Math.min(score, 1);
    score = Math.min(score, 4);
    var labels = ["Too short", "Weak", "Fair", "Good", "Strong"];
    return { score: score, label: labels[score] };
  }

  function enhanceMeter(input) {
    if (input.dataset.pwMeterReady) return;
    input.dataset.pwMeterReady = "1";

    var group = input.closest(".input-group") || input;

    var meter = document.createElement("div");
    meter.className = "pw-meter";
    meter.innerHTML =
      '<div class="pw-meter-bars">' +
      '<span></span><span></span><span></span><span></span>' +
      "</div>" +
      '<span class="pw-meter-label t-micro text-tertiary"></span>';
    group.parentNode.insertBefore(meter, group.nextSibling);

    var bars = meter.querySelectorAll(".pw-meter-bars span");
    var label = meter.querySelector(".pw-meter-label");

    function render() {
      var res = scorePassword(input.value);
      var level = ["", "weak", "fair", "good", "strong"][res.score];
      meter.setAttribute("data-level", level);
      bars.forEach(function (b, i) {
        b.classList.toggle("is-on", i < res.score);
      });
      label.textContent = input.value ? res.label : "";
    }

    input.addEventListener("input", render);
    render();
  }

  /* ---- Generate a strong, readable password ---- */
  function generatePassword(len) {
    len = len || 16;
    var sets = [
      "abcdefghijkmnopqrstuvwxyz", // no l
      "ABCDEFGHJKLMNPQRSTUVWXYZ", // no I, O
      "23456789", // no 0, 1
      "!@#$%^&*-_=+",
    ];
    var all = sets.join("");
    var out = [];
    var rnd = function (n) {
      var a = new Uint32Array(1);
      crypto.getRandomValues(a);
      return a[0] % n;
    };
    // Guarantee one of each class.
    sets.forEach(function (s) {
      out.push(s[rnd(s.length)]);
    });
    while (out.length < len) {
      out.push(all[rnd(all.length)]);
    }
    // Fisher–Yates shuffle.
    for (var i = out.length - 1; i > 0; i--) {
      var j = rnd(i + 1);
      var t = out[i];
      out[i] = out[j];
      out[j] = t;
    }
    return out.join("");
  }

  function wireGenerator(btn) {
    var targetId = btn.dataset.pwGenerate;
    var target = document.getElementById(targetId);
    if (!target) return;
    btn.addEventListener("click", function () {
      var pw = generatePassword(16);
      target.value = pw;
      // Mirror into a confirm field if one points back at the target.
      var confirm = document.querySelector('[data-pw-match="' + targetId + '"]');
      if (confirm) confirm.value = pw;
      // Reveal so the user can see / copy what was made.
      [target, confirm].forEach(function (el) {
        if (el) {
          el.type = "text";
          el.dispatchEvent(new Event("input", { bubbles: true }));
        }
      });
      target.focus();
    });
  }

  /* ---- Confirm-match hint ---- */
  function wireMatch(input) {
    var targetId = input.dataset.pwMatch;
    var target = document.getElementById(targetId);
    if (!target) return;
    function check() {
      if (!input.value) {
        input.classList.remove("is-mismatch");
        return;
      }
      input.classList.toggle("is-mismatch", input.value !== target.value);
    }
    input.addEventListener("input", check);
    target.addEventListener("input", check);
  }

  function init() {
    document.querySelectorAll('input[type="password"]').forEach(enhanceReveal);
    document.querySelectorAll("[data-pw-strength]").forEach(enhanceMeter);
    document.querySelectorAll("[data-pw-generate]").forEach(wireGenerator);
    document.querySelectorAll("[data-pw-match]").forEach(wireMatch);
    if (window.lucide) lucide.createIcons();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
