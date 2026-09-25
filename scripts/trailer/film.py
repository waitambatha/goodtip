"""Film the trailer clips: headless Chromium records the PAGE and nothing else.

No browser bar, no tab strip, no extension banner -- the recording is the page's
own pixels, so nothing in the footage says how it was made. Headless video has
no mouse pointer either, so one is drawn into the page (see CURSOR_JS).

Quality: Playwright's own recorder encodes VP8 at 1 Mbit/s, which turns UI text
to mush. Raise it in the venv's driver (once, outside the repo):

    sed -i 's/-qmax 50 -crf 8 -deadline realtime -speed 8 -b:v 1M -threads 1/-qmax 12 -crf 4 -deadline realtime -speed 8 -b:v 30M -threads 4/' \
        ~/trailer-work/env/lib/python3*/site-packages/playwright/driver/package/lib/coreBundle.js

The clips are kept at the full 1600x1000 the page was drawn at.

Needs, outside the project's own venv (it is not a dependency of the site):

    python3 -m venv ~/trailer-work/env
    ~/trailer-work/env/bin/pip install playwright==1.62.0 imageio-ffmpeg

Usage, with scripts/trailer/serve.sh running on :8002:

    ~/trailer-work/env/bin/python scripts/trailer/film.py home signup ...
    ~/trailer-work/env/bin/python scripts/trailer/film.py all
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8002"
RAW = Path.home() / "trailer-work" / "raw"
OUT = ROOT / "static" / "video" / "trailer"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
# A little wider than a laptop, so the trailer's box shows more of each page
# (16:10, like the clips have always been).
W, H = 1600, 1000

# A pointer, drawn in the page. Follows real mouse events, so Playwright's own
# mouse.move() drives it; a lime ring blooms on every press.
CURSOR_JS = """
(() => {
  if (window.__gtCursor) return; window.__gtCursor = true;
  const css = document.createElement('style');
  css.textContent = `
    #gt-cur{position:fixed;left:0;top:0;width:26px;height:26px;z-index:2147483647;pointer-events:none;
      transform:translate(-100px,-100px);filter:drop-shadow(0 2px 3px rgba(0,0,0,.45))}
    #gt-ring{position:fixed;left:0;top:0;width:14px;height:14px;margin:-7px 0 0 -7px;border-radius:50%;
      border:3px solid #CFF93A;z-index:2147483646;pointer-events:none;opacity:0}
    #gt-ring.go{animation:gtring .55s ease-out}
    @keyframes gtring{0%{opacity:.95;transform:scale(.6)}100%{opacity:0;transform:scale(4.2)}}`;
  const add = () => {
    document.head.appendChild(css);
    const c = document.createElement('div'); c.id = 'gt-cur';
    c.innerHTML = '<svg viewBox="0 0 24 24" width="26" height="26"><path d="M4 2l15 9-6.5 1.6L9.6 19z" fill="#fff" stroke="#0F2E22" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    const r = document.createElement('div'); r.id = 'gt-ring';
    document.body.append(r, c);
    addEventListener('mousemove', e => { c.style.transform = `translate(${e.clientX-4}px,${e.clientY-2}px)`; }, true);
    addEventListener('mousedown', e => { r.style.left = e.clientX+'px'; r.style.top = e.clientY+'px';
      r.classList.remove('go'); void r.offsetWidth; r.classList.add('go'); }, true);
  };
  document.body ? add() : addEventListener('DOMContentLoaded', add);
  window.__glideScroll = (y, ms) => new Promise(done => {
    const el = document.scrollingElement, y0 = el.scrollTop, t0 = performance.now();
    const step = t => { const p = Math.min(1, (t - t0) / ms);
      const e = p < .5 ? 4*p*p*p : 1 - Math.pow(-2*p + 2, 3) / 2;
      el.scrollTo({top: y0 + (y - y0) * e, behavior: 'instant'});
      p < 1 ? requestAnimationFrame(step) : done(); };
    requestAnimationFrame(step);
  });
})();
"""


class Reel:
    """One recording. `ready()` marks where the clip really begins, so the
    loader and any warm-up before it are trimmed away."""

    def __init__(self, browser, name, cookies=None, size=(W, H)):
        self.name = name
        RAW.mkdir(parents=True, exist_ok=True)
        for old in RAW.glob(f"{name}-*.webm"):
            old.unlink()
        shutil.rmtree(RAW / f"{name}-tmp", ignore_errors=True)
        self.ctx = browser.new_context(
            viewport={"width": size[0], "height": size[1]},
            record_video_dir=str(RAW / f"{name}-tmp"),
            record_video_size={"width": size[0], "height": size[1]},
            reduced_motion="no-preference", locale="en-AU",
            timezone_id="Australia/Melbourne",
        )
        self.ctx.add_init_script(CURSOR_JS)
        if cookies:
            self.ctx.add_cookies(cookies)
        self.page = self.ctx.new_page()
        self.t0 = time.time()
        self.start = 0.0
        self.mx, self.my = size[0] * 0.6, size[1] * 0.55

    # -- timing ---------------------------------------------------------
    def ready(self):
        self.start = time.time() - self.t0

    def wait(self, s):
        self.page.wait_for_timeout(int(s * 1000))

    # -- movement -------------------------------------------------------
    def glide(self, x, y, ms=650):
        n = max(8, int(ms / 16))
        x0, y0 = self.mx, self.my
        for i in range(1, n + 1):
            p = i / n
            e = p * p * (3 - 2 * p)
            self.page.mouse.move(x0 + (x - x0) * e, y0 + (y - y0) * e)
            self.page.wait_for_timeout(ms // n)
        self.mx, self.my = x, y

    def _box(self, loc):
        loc.scroll_into_view_if_needed()
        b = loc.bounding_box()
        return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2

    def click(self, sel, ms=650, pause=0.25):
        loc = self.page.locator(sel).first if isinstance(sel, str) else sel
        x, y = self._box(loc)
        self.glide(x, y, ms)
        self.wait(pause)
        self.page.mouse.down()
        self.page.wait_for_timeout(70)
        self.page.mouse.up()

    def hover(self, sel, ms=650):
        x, y = self._box(self.page.locator(sel).first)
        self.glide(x, y, ms)

    def type(self, sel, text, delay=70):
        self.click(sel, ms=500, pause=0.15)
        self.page.keyboard.type(text, delay=delay)

    def scroll(self, y, ms=1600):
        self.page.evaluate("([y, ms]) => window.__glideScroll(y, ms)", [y, ms])

    def scroll_to(self, sel, offset=120, ms=1600):
        y = self.page.evaluate(
            "([s, o]) => document.querySelector(s).getBoundingClientRect().top"
            " + document.scrollingElement.scrollTop - o", [sel, offset])
        self.scroll(max(0, y), ms)

    def goto(self, path, settle=0.6):
        self.page.goto(BASE + path, wait_until="load")
        self.wait(settle)

    def shot(self, n):
        """A still of what is on screen now, kept beside the clip as
        `<name>-<n>.jpg` (960 wide). The showcase overlays caption these."""
        OUT.mkdir(parents=True, exist_ok=True)
        big = RAW / f"{self.name}-{n}-full.jpg"
        self.page.screenshot(path=str(big), type="jpeg", quality=88)
        subprocess.run([
            FFMPEG, "-y", "-loglevel", "error", "-i", str(big),
            "-vf", "scale=960:-2", "-q:v", "4", str(OUT / f"{self.name}-{n}.jpg")], check=True)
        big.unlink()

    # -- finish ---------------------------------------------------------
    def save(self, hold=0.6, poster_at=None, speed=1.0):
        self.wait(hold)
        vid = self.page.video
        self.ctx.close()
        src = Path(vid.path())
        raw = RAW / f"{self.name}-raw.webm"
        src.replace(raw)
        shutil.rmtree(RAW / f"{self.name}-tmp", ignore_errors=True)
        OUT.mkdir(parents=True, exist_ok=True)
        mp4 = OUT / f"{self.name}.mp4"
        subprocess.run([
            FFMPEG, "-y", "-loglevel", "error", "-ss", f"{max(0, self.start - 0.05):.2f}",
            "-i", str(raw), "-vf", f"setpts=PTS/{speed},fps=30",
            "-c:v", "libx264", "-crf", "23", "-preset", "slow", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-an", str(mp4)], check=True)
        dur = duration(mp4)
        at = poster_at if poster_at is not None else min(2.0, dur * 0.3)
        subprocess.run([
            FFMPEG, "-y", "-loglevel", "error", "-ss", f"{at:.2f}", "-i", str(mp4),
            "-frames:v", "1", "-q:v", "4", str(OUT / f"{self.name}.jpg")], check=True)
        print(f"  {self.name}: {dur:.1f}s, {mp4.stat().st_size/1024:.0f} KB")


def duration(path):
    err = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True).stderr
    h, m, sec = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err).groups()
    return int(h) * 3600 + int(m) * 60 + float(sec)


def session_cookie(email, org_id=None):
    """A logged-in session for `email`, minted with the test client so no
    password or sign-in code is typed. Returns Playwright cookies."""
    code = f"""
import json
from django.test import Client
from django.utils import timezone
from accounts.models import User
c = Client(SERVER_NAME='localhost')
u = User.objects.get(email={email!r})
c.force_login(u)
s = c.session
{"from orgs.context import ORG_KEY; s[ORG_KEY] = " + str(org_id) if org_id else ""}
s.save()
print(json.dumps(c.cookies['sessionid'].value))
"""
    env = dict(os.environ)
    out = subprocess.run(
        ["bash", "-c", f'source "{ROOT}/scripts/trailer/env.sh" && "$PY" "{ROOT}/manage.py" shell -c "$CODE"'],
        env={**env, "CODE": code}, capture_output=True, text=True, check=True).stdout
    val = json.loads(out.strip().splitlines()[-1])
    return [{"name": "sessionid", "value": val, "url": BASE}]


def django_shell(code):
    """Run `code` in the trailer database's Django shell."""
    subprocess.run(
        ["bash", "-c", f'source "{ROOT}/scripts/trailer/env.sh" && "$PY" "{ROOT}/manage.py" shell -c "$CODE"'],
        env={**os.environ, "CODE": code}, capture_output=True, text=True, check=True)


def main(argv):
    from scenes import SCENES
    names = list(SCENES) if argv == ["all"] else argv
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for n in names:
            print(f"filming {n}")
            SCENES[n](browser, Reel, session_cookie)
        browser.close()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    main(sys.argv[1:])
