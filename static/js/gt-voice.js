/* GoodTip voice notes — recording one, and playing one back.
 *
 * Two halves that share nothing but a file:
 *
 *   THE RECORDER, in a room's composer. Press the microphone, talk, press
 *   send. Hard-stops at ninety seconds, which is the ceiling the client set
 *   ("max of 1 and half of a minute") and which the server enforces again,
 *   because a limit only the browser knows is a convenience and not a rule.
 *
 *   THE PLAYER, on every voice note in a conversation. A round button, a bar
 *   that fills, and the length beside it — because <audio controls> is a
 *   different grey box in every browser and looks like nothing else here.
 *
 * WHY THE RECORDING IS PUT INTO A FILE INPUT rather than posted with fetch:
 * the composer is an ordinary multipart <form> that already handles the reply
 * id, the text, the attachments and the CSRF token. Handing it a File through
 * a DataTransfer means the recording travels by exactly the same path as an
 * attached photo — one code path on the server, one on the client, and the
 * no-JavaScript form still works because none of this is load-bearing for it.
 */
(function () {
  'use strict';

  var MAX_SECONDS = 90;

  /* ======================================================================
     THE RECORDER
     ====================================================================== */

  /* One recording at a time, page-wide. There is only ever one composer on
     screen, and a second recorder started while the first was running would
     leave a live microphone nobody can see. */
  var live = null;

  function fmt(total) {
    total = Math.max(0, Math.floor(total));
    return Math.floor(total / 60) + ':' + ('0' + (total % 60)).slice(-2);
  }

  /* What the browser will actually record. Chrome and Firefox give webm/opus,
     Safari gives mp4/aac, and nothing lets you choose — so this asks for what
     is supported rather than naming one and hoping. The suffix has to match
     the type, or the server's allowlist rejects a file it can play perfectly
     well. */
  function pickFormat() {
    var options = [
      ['audio/webm;codecs=opus', 'webm'],
      ['audio/webm', 'webm'],
      ['audio/mp4', 'm4a'],
      ['audio/ogg;codecs=opus', 'ogg'],
    ];
    for (var i = 0; i < options.length; i++) {
      if (!window.MediaRecorder.isTypeSupported ||
          window.MediaRecorder.isTypeSupported(options[i][0])) {
        return { mime: options[i][0], ext: options[i][1] };
      }
    }
    /* No isTypeSupported and nothing matched: let the browser choose and take
       whatever the blob says it is. */
    return { mime: '', ext: 'webm' };
  }

  function show(form, which) {
    ['idle', 'bar', 'ready'].forEach(function (name) {
      var el = form.querySelector(
        name === 'idle' ? '[data-rec-idle]' :
        name === 'bar' ? '[data-rec-bar]' : '[data-rec-ready]'
      );
      if (el) el.hidden = name !== which;
    });
    var hint = form.querySelector('[data-rec-idle-hint]');
    if (hint) hint.hidden = which !== 'idle';
  }

  function stopTracks(stream) {
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
  }

  function teardown(keepFile) {
    if (!live) return;
    if (live.tick) clearInterval(live.tick);
    stopTracks(live.stream);
    if (!keepFile && live.url) URL.revokeObjectURL(live.url);
    var form = live.form;
    live = null;
    return form;
  }

  function start(form) {
    if (live) return;
    if (!navigator.mediaDevices || !window.MediaRecorder) return;

    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      var fmtPick = pickFormat();
      var rec = new MediaRecorder(stream, fmtPick.mime ? { mimeType: fmtPick.mime } : undefined);
      var chunks = [];
      var began = Date.now();

      live = { form: form, rec: rec, stream: stream, ext: fmtPick.ext, url: null, tick: null };

      rec.addEventListener('dataavailable', function (e) {
        if (e.data && e.data.size) chunks.push(e.data);
      });
      rec.addEventListener('stop', function () {
        /* CANCEL BEATS STOP, and it has to be checked here rather than by
           clearing the field afterwards. `rec.stop()` fires this handler on a
           later tick, so a cancel that stopped the recorder and then cleared
           the file input would be undone a moment later by this very
           function putting the blob back. */
        if (live && live.discard) { discard(form); return; }
        var seconds = Math.round((Date.now() - began) / 1000);
        /* A tap on the microphone that stops before anything was said. Better
           to say nothing happened than to attach half a second of silence. */
        if (!chunks.length || seconds < 1) { show(form, 'idle'); teardown(false); return; }

        var type = (chunks[0] && chunks[0].type) || fmtPick.mime || 'audio/webm';
        var blob = new Blob(chunks, { type: type });
        var file = new File([blob], 'voice-note.' + fmtPick.ext, { type: type });

        /* Into the form's own file input, so it posts with everything else.
           DataTransfer is the only way to set .files, and it is supported
           everywhere MediaRecorder is. */
        var field = form.querySelector('[data-rec-file]');
        var dt = new DataTransfer();
        dt.items.add(file);
        field.files = dt.files;

        var secs = form.querySelector('[data-rec-seconds]');
        if (secs) secs.value = Math.min(seconds, MAX_SECONDS);

        if (live) live.url = URL.createObjectURL(blob);
        var preview = form.querySelector('[data-rec-preview-audio]');
        if (!preview) {
          preview = document.createElement('audio');
          preview.setAttribute('data-rec-preview-audio', '');
          form.appendChild(preview);
        }
        preview.src = live ? live.url : '';

        var label = form.querySelector('[data-rec-ready-time]');
        if (label) label.textContent = fmt(Math.min(seconds, MAX_SECONDS));
        show(form, 'ready');
        /* The stream is released here and not on send: a recording that has
           finished must not leave the browser's recording indicator lit while
           somebody decides whether to send it. */
        if (live) { clearInterval(live.tick); stopTracks(live.stream); live.stream = null; }
      });

      rec.start();
      show(form, 'bar');
      var clock = form.querySelector('[data-rec-time]');
      live.tick = setInterval(function () {
        var seconds = Math.round((Date.now() - began) / 1000);
        if (clock) clock.textContent = fmt(seconds);
        /* THE CEILING, enforced by stopping rather than by refusing to send.
           A recorder that lets you talk for four minutes and then says no is
           a recorder that has wasted four minutes of somebody's time. */
        if (seconds >= MAX_SECONDS) stop();
      }, 200);
    }).catch(function () {
      /* Refused, or no microphone. Nothing to recover from and nothing worth
         a dialog about — the text box is right there. */
      show(form, 'idle');
    });
  }

  function stop() {
    if (live && live.rec && live.rec.state === 'recording') live.rec.stop();
  }

  function cancel() {
    if (!live) return;
    var form = live.form;
    if (live.rec && live.rec.state === 'recording') {
      /* Flagged, then stopped. The stop handler reads the flag and throws the
         recording away instead of attaching it — see the comment there. */
      live.discard = true;
      if (live.tick) { clearInterval(live.tick); live.tick = null; }
      live.rec.stop();
      return;
    }
    discard(form);
  }

  function discard(form) {
    if (!form) return;
    var field = form.querySelector('[data-rec-file]');
    if (field) field.value = '';
    var secs = form.querySelector('[data-rec-seconds]');
    if (secs) secs.value = '0';
    var preview = form.querySelector('[data-rec-preview-audio]');
    if (preview) { preview.pause(); preview.removeAttribute('src'); }
    show(form, 'idle');
    teardown(false);
  }

  document.addEventListener('click', function (e) {
    var form = e.target.closest && e.target.closest('[data-gtm-composer]');
    if (!form) return;
    if (e.target.closest('[data-rec-start]')) { start(form); return; }
    if (e.target.closest('[data-rec-stop]')) { stop(); return; }
    if (e.target.closest('[data-rec-cancel]')) { cancel(); return; }
    if (e.target.closest('[data-rec-discard]')) { discard(form); return; }
    if (e.target.closest('[data-rec-play]')) {
      var preview = form.querySelector('[data-rec-preview-audio]');
      if (!preview) return;
      if (preview.paused) preview.play(); else preview.pause();
    }
  });

  /* A recording still running when the page goes away releases the microphone
     on its own; a page that navigates mid-recording would otherwise leave the
     indicator on until the tab is closed. */
  window.addEventListener('pagehide', function () { teardown(false); });

  /* ======================================================================
     THE PLAYER
     ======================================================================
     Delegated, because a conversation is replaced whole every twelve seconds
     and a listener bound to a player at load stops existing at the first
     poll. */

  function playerOf(el) { return el.closest('[data-voice]'); }

  /* One at a time. Two voice notes playing over each other is never what
     anybody meant, and it is the easiest thing in the world to do by accident
     in a list of them. */
  function pauseOthers(except) {
    document.querySelectorAll('[data-voice] audio').forEach(function (a) {
      if (a !== except && !a.paused) a.pause();
    });
  }

  document.addEventListener('click', function (e) {
    var wrap = e.target.closest && e.target.closest('[data-voice]');
    if (!wrap) return;
    var audio = wrap.querySelector('[data-voice-audio]');
    if (!audio) return;

    if (e.target.closest('.cvn-play')) {
      if (audio.paused) { pauseOthers(audio); audio.play(); } else { audio.pause(); }
      return;
    }
    /* Seek by pressing the bar. Uses the element's own duration, and does
       nothing when that is not a finite number yet — an unloaded clip reports
       Infinity, and seeking to Infinity throws. */
    var track = e.target.closest('[data-voice-seek]');
    if (track && isFinite(audio.duration) && audio.duration > 0) {
      var rect = track.getBoundingClientRect();
      var pct = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
      audio.currentTime = pct * audio.duration;
    }
  });

  /* The three events that change what a player should look like. Captured,
     because media events do not bubble — without `true` here none of these
     ever reach the document. */
  ['play', 'pause', 'ended', 'timeupdate'].forEach(function (name) {
    document.addEventListener(name, function (e) {
      if (!e.target || e.target.tagName !== 'AUDIO') return;
      var wrap = playerOf(e.target);
      if (!wrap) return;
      wrap.classList.toggle('is-playing', !e.target.paused && !e.target.ended);
      var fill = wrap.querySelector('[data-voice-fill]');
      var time = wrap.querySelector('[data-voice-time]');
      var d = e.target.duration;
      if (fill && isFinite(d) && d > 0) {
        fill.style.width = Math.round((e.target.currentTime / d) * 100) + '%';
      }
      if (name === 'ended' && fill) fill.style.width = '0%';
      /* While it plays the label counts UP through the clip; stopped, it goes
         back to the length, which is what a stopped player should say. */
      if (time) {
        if (!e.target.paused && !e.target.ended && isFinite(d)) {
          time.textContent = fmt(e.target.currentTime);
        } else if (isFinite(d) && d > 0 && name === 'ended') {
          time.textContent = fmt(d);
        }
      }
    }, true);
  });
})();
