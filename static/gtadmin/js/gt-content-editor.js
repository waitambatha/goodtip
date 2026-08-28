/* ==========================================================================
   gt-content-editor.js — behaviour for the Site content editor.

   Three jobs, none of which needs a framework:

   1. Rich fields. The visible control is a contenteditable; the value that
      actually posts is a hidden input kept in sync on every keystroke, so a
      submit can never race the last character typed.
   2. Media fields. Drag-and-drop onto the picker, and a preview that updates
      from the chosen file before anything is uploaded — otherwise you save,
      reload, and only then find out you picked the wrong photo.
   3. An unsaved-changes guard. The form is long enough to scroll past, and
      navigating away from a page of edited copy with no warning loses work
      that has no other copy anywhere.
   ========================================================================== */
(function () {
  "use strict";

  /* The admin header is sticky and its height changes with the sub-nav
     wrapping, so the editor's own sticky toolbar cannot hard-code an offset.
     Publish the measured height as a variable the stylesheet reads. */
  var header = document.getElementById("header");
  function syncHeaderHeight() {
    if (!header) return;
    document.documentElement.style.setProperty(
      "--gta-header-h", header.offsetHeight + "px"
    );
  }
  syncHeaderHeight();
  window.addEventListener("resize", syncHeaderHeight);

  var form = document.getElementById("gtc-form");
  if (!form) return;

  var dirty = false;
  function markDirty() { dirty = true; }

  /* ---- 1. Rich fields --------------------------------------------------- */
  document.querySelectorAll("[data-rich]").forEach(function (wrap) {
    var area = wrap.querySelector(".gtc-rich-area");
    var hidden = wrap.parentNode.querySelector('input[type="hidden"]');
    if (!area || !hidden) return;

    function sync() {
      // A contenteditable emptied by the user leaves a stray <br>; posting
      // that instead of "" would store an override that looks blank but stops
      // the default from coming back.
      var html = area.innerHTML.trim();
      if (html === "<br>" || html === "<div><br></div>") html = "";
      hidden.value = html;
    }

    area.addEventListener("input", function () { sync(); markDirty(); });
    area.addEventListener("blur", sync);

    // Paste as plain text. Pasting from Word or a browser drags a stylesheet's
    // worth of inline styles and font tags in with it, and the sanitiser on
    // save strips scripts, not <span style="font-family:Calibri">.
    area.addEventListener("paste", function (e) {
      e.preventDefault();
      var text = (e.clipboardData || window.clipboardData).getData("text/plain");
      document.execCommand("insertText", false, text);
    });

    wrap.querySelectorAll(".gtc-rich-tools button").forEach(function (btn) {
      btn.addEventListener("mousedown", function (e) { e.preventDefault(); });
      btn.addEventListener("click", function () {
        var cmd = btn.getAttribute("data-cmd");
        area.focus();
        if (cmd === "createLink") {
          var url = window.prompt("Link to where?", "https://");
          if (!url) return;
          document.execCommand("createLink", false, url);
        } else {
          document.execCommand(cmd, false, null);
        }
        sync();
        markDirty();
      });
    });
  });

  /* ---- 2. Media fields -------------------------------------------------- */
  function previewFor(input) {
    var media = input.closest(".gtc-media");
    if (!media) return null;
    return media.querySelector("[data-preview]");
  }

  function showPicked(input) {
    var label = input.closest("[data-drop]");
    var out = label && label.querySelector("[data-picked]");
    if (!out) return;
    if (input.files && input.files[0]) {
      out.textContent = input.files[0].name;
      out.hidden = false;
    } else {
      out.hidden = true;
    }
  }

  function applyPreview(input) {
    var file = input.files && input.files[0];
    var box = previewFor(input);
    if (!file || !box) return;
    var url = URL.createObjectURL(file);
    var isPoster = input.name.indexOf("poster_") === 0;
    var video = box.querySelector("video");
    var img = box.querySelector("img");

    if (isPoster && video) {
      video.poster = url;
    } else if (video && file.type.indexOf("video") === 0) {
      video.src = url;
      video.load();
    } else if (img) {
      img.src = url;
    }
    var tag = box.querySelector(".gtc-thumb-tag");
    if (tag) tag.textContent = "Not saved yet";
  }

  document.querySelectorAll("[data-file]").forEach(function (input) {
    input.addEventListener("change", function () {
      showPicked(input);
      applyPreview(input);
      markDirty();
      // Choosing a replacement and also ticking "reset to default" is a
      // contradiction; the explicit upload wins and the tick is cleared.
      var slot = input.closest(".gtc-slot");
      var reset = slot && slot.querySelector('input[type="checkbox"][name^="reset_"]');
      if (reset) reset.checked = false;
    });
  });

  document.querySelectorAll("[data-drop]").forEach(function (label) {
    var input = label.querySelector("[data-file]");
    if (!input) return;
    ["dragenter", "dragover"].forEach(function (evt) {
      label.addEventListener(evt, function (e) {
        e.preventDefault();
        label.classList.add("over");
      });
    });
    ["dragleave", "drop"].forEach(function (evt) {
      label.addEventListener(evt, function () { label.classList.remove("over"); });
    });
    label.addEventListener("drop", function (e) {
      e.preventDefault();
      if (!e.dataTransfer || !e.dataTransfer.files.length) return;
      // Assigning a DataTransfer's FileList straight onto the input is the
      // only way to make a dropped file part of the multipart submit.
      input.files = e.dataTransfer.files;
      input.dispatchEvent(new Event("change"));
    });
  });

  document.querySelectorAll('input[type="checkbox"][name^="reset_"]').forEach(function (box) {
    box.addEventListener("change", function () {
      markDirty();
      var slot = box.closest(".gtc-slot");
      var tag = slot && slot.querySelector(".gtc-thumb-tag");
      if (tag) tag.textContent = box.checked ? "Will reset" : "Uploaded";
    });
  });

  /* ---- 3. Dirty guard --------------------------------------------------- */
  form.addEventListener("input", markDirty);
  form.addEventListener("change", markDirty);
  form.addEventListener("submit", function () { dirty = false; });
  window.addEventListener("beforeunload", function (e) {
    if (!dirty) return;
    e.preventDefault();
    e.returnValue = "";
  });
})();
