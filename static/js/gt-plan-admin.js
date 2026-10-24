/* The Plans hub's discount field — either a percentage or a flat amount
 * drives the other, live, as the admin types (client, 19 Oct 2026: "either
 * a flat amount or a percentage, and it auto-calculates and saves the
 * other").
 *
 * The server is still the one that decides what gets saved — see
 * sysadmin.plan_admin_views.plan_override_save — this only keeps the other
 * field's number honest on screen and tells the server which one the admin
 * actually typed into, via the hidden discount_mode field. A form submitted
 * with this script not running still works; it just keeps whichever
 * discount was already saved unless discount_mode says otherwise.
 */
(function () {
  function basePrice(form) {
    var priceField = form.querySelector(".gta-plan-price");
    var typed = priceField && priceField.value ? parseFloat(priceField.value) : NaN;
    if (!isNaN(typed) && typed > 0) return typed;
    var attr = parseFloat(form.dataset.basePrice);
    return isNaN(attr) ? 0 : attr;
  }

  function wire(form) {
    var pct = form.querySelector(".gta-plan-discount-pct");
    var amt = form.querySelector(".gta-plan-discount-amt");
    var mode = form.querySelector(".gta-plan-discount-mode");
    if (!pct || !amt || !mode) return;

    pct.addEventListener("input", function () {
      mode.value = "percent";
      var base = basePrice(form);
      var p = parseFloat(pct.value);
      amt.value = (!isNaN(p) && base > 0) ? ((base * p) / 100).toFixed(2) : "";
    });

    amt.addEventListener("input", function () {
      mode.value = "amount";
      var base = basePrice(form);
      var a = parseFloat(amt.value);
      pct.value = (!isNaN(a) && base > 0) ? ((a / base) * 100).toFixed(2) : "";
    });

    [pct, amt].forEach(function (field) {
      field.addEventListener("input", function () {
        if (field.value === "") mode.value = "clear";
      });
    });
  }

  document.querySelectorAll(".gta-plan-form").forEach(wire);
}());
