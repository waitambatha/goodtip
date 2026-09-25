"""One function per clip. Each takes (browser, Reel, session_cookie) and films
one short journey, ending on a still frame so the trailer page can hold it.

Everything here runs against the throwaway trailer database (fictional people
only -- see seed_trailer_db.py). Sam is the owner; the password is the one the
seed script sets.
"""
import re
import time
from pathlib import Path

PASSWORD = "Trailer#2026"
SAM = "sam@riverbend.example"
SERVER_LOG = Path("/tmp/trailer-server.log")
ORG = 1


def _otp(email, timeout=10):
    """The newest six-digit code the trailer server printed for `email`."""
    end = time.time() + timeout
    while time.time() < end:
        text = SERVER_LOG.read_text(errors="ignore") if SERVER_LOG.exists() else ""
        hits = re.findall(r"ONE-TIME CODE: (\d{6}).*?\n\s+for: (\S+)", text)
        mine = [c for c, e in hits if e == email]
        if mine:
            return mine[-1]
        time.sleep(0.4)
    raise RuntimeError(f"no code printed for {email}")


def _loader(r, s=3.0):
    """Public pages open with the 2.6s sport loader; ride it out, then mark
    the real start of the clip."""
    r.wait(s)
    r.ready()


def _member(browser, Reel, cookie, name, path, settle=1.2):
    """A clip that opens on a signed-in page as Sam."""
    r = Reel(browser, name, cookies=cookie(SAM, ORG))
    r.goto(path, settle=0)
    r.wait(settle)
    r.ready()
    return r


# The home page, section by section. Each stop puts the first words of a
# section just under the nav, so everything the section shows sits below them.
HOME_STOPS = [
    ("Live this season", 1.5),
    ("Every other tipping comp ends the same way.", 1.5),
    ("How it works", 1.5),
    ("Who it's for", 1.4),
    ("Women's sport included, always", 1.2),
    ("New for 2026", 2.0),      # The Wall: two seconds, then on
    ("Supporters", 1.3),
    ("Charity partners", 1.4),
    ("Pricing", 1.4),
    ("How the donation works", 1.4),
    ("Not quite open yet", 1.4),
    ("FAQ", 1.4),
]


def _to_words(r, words, offset=92, ms=950):
    """Scroll so the heading or eyebrow starting with `words` is at the top."""
    found = r.page.evaluate(
        """(w) => {
          const el = [...document.querySelectorAll('.eyebrow, .section-heading, h2')]
            .find(e => e.textContent.replace(/\\s+/g, ' ').trim().startsWith(w));
          if (!el) return false;
          document.querySelectorAll('[data-reel-stop]').forEach(e => e.removeAttribute('data-reel-stop'));
          el.setAttribute('data-reel-stop', '1');
          return true;
        }""", words)
    if found:
        r.scroll_to("[data-reel-stop]", offset=offset, ms=ms)
    return found


def home(browser, Reel, cookie):
    r = Reel(browser, "home")
    r.goto("/", settle=0)
    _loader(r)
    r.wait(1.6)
    for words, hold in HOME_STOPS:
        if _to_words(r, words):
            r.wait(hold)
    # and down to the footer
    r.scroll(r.page.evaluate("document.scrollingElement.scrollHeight - innerHeight"), 1300)
    r.wait(1.0)
    r.save(hold=0.4, poster_at=1.2)


def signup(browser, Reel, cookie):
    r = Reel(browser, "signup")
    r.goto("/signup/", settle=0)
    _loader(r)
    r.wait(0.6)
    # A fixed, tidy address on screen; clear any earlier take's account first.
    email = "casey.morgan@riverbend.example"
    from film import django_shell
    django_shell(f"from accounts.models import User; User.objects.filter(email={email!r}).delete()")
    r.type("#id_display_name", "Casey Morgan")
    r.type("#id_email", email)
    r.type("#id_password1", PASSWORD, delay=55)
    r.type("#id_password2", PASSWORD, delay=45)
    r.wait(0.9)
    r.click("button[type=submit]")
    r.page.wait_for_url("**/verify/**", timeout=15000)
    r.wait(3.0)  # the sport loader covers this page too, and eats clicks
    r.type("#id_code", _otp(email), delay=150)
    r.wait(2.2)
    r.save(hold=0.5)


def signin(browser, Reel, cookie):
    r = Reel(browser, "signin")
    r.goto("/login/", settle=0)
    _loader(r)
    r.wait(0.6)
    r.type("#id_email", SAM)
    r.type("#id_password", PASSWORD, delay=55)
    r.wait(0.5)
    r.click("button[type=submit]")
    r.page.wait_for_url("**/verify/**", timeout=15000)
    r.wait(3.0)  # the sport loader covers this page too, and eats clicks
    r.type("#id_code", _otp(SAM), delay=150)
    r.page.wait_for_url(re.compile(r"/dashboard"), timeout=15000)
    r.wait(2.4)
    r.save(hold=0.6)


def create_org(browser, Reel, cookie):
    from film import django_shell
    # The wizard remembers a half-finished draft; start every take from step one.
    django_shell("from orgs.models import OrgDraft; OrgDraft.objects.all().delete()")
    r = _member(browser, Reel, cookie, "create-org", "/leagues/new/")
    r.wait(1.2)
    r.click("label.opt-card:has-text('Informal')")
    r.wait(2.0)
    r.click("label.opt-card:has-text('Formal') >> nth=0")
    r.wait(2.2)
    r.click("button:has-text('Continue')")
    r.wait(4.2)  # the sport loader covers the next step and eats clicks
    # A formal setup names what it is and what kind -- and that opens more to fill.
    r.type("#id_name", "Riverbend Community Bank", delay=55)
    r.wait(0.5)
    r.scroll(430, 1200)
    r.page.select_option("#id_organisation_type", label="Community")
    r.wait(1.3)
    r.scroll(r.page.evaluate("document.scrollingElement.scrollTop") + 240, 1000)
    r.click("#subcatGrid label.opt-card:visible >> nth=0", ms=700)
    r.wait(1.0)
    r.page.select_option("#id_country", label="Australia")
    r.wait(1.8)
    r.save(hold=0.5)

def create_group(browser, Reel, cookie):
    r = _member(browser, Reel, cookie, "create-group", f"/leagues/{ORG}/groups/")
    r.wait(1.0)
    r.click(".dh-cta >> text=Create group")
    r.wait(1.0)
    r.click(".gm-chip >> text=Volunteers")
    r.wait(1.6)
    r.save(hold=0.6)


def _clear_sam_tips():
    """Sam's open picks removed, so a take starts from an empty slip."""
    from film import django_shell
    django_shell(
        "from tipping.models import Tip; from django.utils import timezone; "
        f"Tip.objects.filter(user__email={SAM!r}, match__kickoff_at__gte=timezone.now()).delete()")


def fixtures(browser, Reel, cookie):
    """Three picks in every code, saved, then straight to My Tips. Only open
    games are shown: each code scrolls past what has already kicked off."""
    _clear_sam_tips()
    r = _member(browser, Reel, cookie, "fixtures", f"/dashboard/?org={ORG}")
    for slug in ("afl", "aflw", "nrl", "nrlw"):
        # what has already kicked off cannot be picked, so it is not shown at
        # all (a fresh page after every save, so the rule goes in each time)
        for _ in range(4):      # the save's redirect may still be landing
            try:
                r.page.wait_for_load_state("load")
                r.page.add_style_tag(content=".fxc.is-locked { display: none !important; }")
                break
            except Exception:
                r.wait(0.5)
        r.scroll_to(".sfilter-chips", offset=200, ms=700)
        r.click(f".sfilter-chip[href$='series={slug}']", ms=450, pause=0.1)
        r.page.wait_for_load_state("networkidle")
        r.wait(0.7)
        for k in (0, 3, 4):     # home, away, home
            r.click(f"#slipPanel label.fxc-team >> nth={k}", ms=380, pause=0.08)
            r.wait(0.25)
        r.scroll_to("#slipPanel .slip-bar", offset=100, ms=500)
        r.click("#slipReview", ms=420, pause=0.1)
        r.wait(0.8)
        r.click("#tsNext", ms=380, pause=0.1)
        r.wait(0.7)
        with r.page.expect_navigation(wait_until="load", timeout=30000):
            r.click("#tsConfirm", ms=380, pause=0.1)
        r.wait(1.0)
    r.click("a[href$='/tips/']", ms=600, pause=0.1)
    r.wait(1.4)
    r.scroll(720, 1200)
    r.wait(1.0)
    r.scroll(1500, 1400)
    r.wait(0.8)
    r.save(hold=0.4, speed=1.5)


def matchreader(browser, Reel, cookie):
    _clear_sam_tips()
    r = _member(browser, Reel, cookie, "matchreader", f"/dashboard/?org={ORG}&series=afl")
    r.scroll_to("#slipPanel .fxc-why", offset=320, ms=1300)
    r.wait(0.8)
    r.click("#slipPanel .fxc-why")
    r.wait(3.2)
    r.save(hold=0.5)


def picks(browser, Reel, cookie):
    _clear_sam_tips()
    r = _member(browser, Reel, cookie, "picks", f"/dashboard/?org={ORG}&series=afl")
    r.scroll_to("#slipPanel .slip-bar", offset=80, ms=1300)
    r.wait(0.6)
    for i in range(0, 4):
        r.click(f"#slipPanel .fxc-team >> nth={i * 2 + (i % 2)}", ms=550, pause=0.15)
        r.wait(0.55)
    r.click("#slipReview", ms=700)
    r.wait(2.6)
    r.save(hold=0.5)


def my_tips(browser, Reel, cookie):
    r = _member(browser, Reel, cookie, "my-tips", f"/org/{ORG}/tips/")
    r.wait(1.2)
    r.scroll(700, 1800)
    r.wait(1.0)
    r.scroll(1500, 2000)
    r.wait(1.2)
    r.save(hold=0.5)


def ladder(browser, Reel, cookie):
    r = _member(browser, Reel, cookie, "ladder", f"/org/{ORG}/ladder/")
    r.wait(1.0)
    r.scroll(560, 1600)
    r.wait(1.0)
    # the whole competition, side by side
    r.scroll(0, 1200)
    r.click(".comp-stats-btn", ms=700)
    r.page.wait_for_url("**/ladder/statistics/**", timeout=15000)
    r.wait(2.4)
    r.scroll(620, 1800)
    r.wait(1.2)
    r.scroll(1200, 1800)
    r.wait(1.0)
    # then one club on its own
    r.goto(f"/org/{ORG}/ladder/", settle=1.6)
    r.scroll(560, 1200)
    r.click(".lad-stats >> nth=0", ms=800)
    r.page.wait_for_url("**/ladder/team/**", timeout=15000)
    r.wait(2.4)
    r.scroll(620, 1800)
    r.wait(1.6)
    r.save(hold=0.5)

def leaderboard(browser, Reel, cookie):
    r = _member(browser, Reel, cookie, "leaderboard", f"/org/{ORG}/leaderboard/")
    r.wait(1.2)
    r.scroll(620, 1600)
    r.wait(1.0)
    r.scroll(0, 1400)
    r.wait(0.4)
    r.click("a[href$='/leaderboard/me/']", ms=700)
    r.page.wait_for_url("**/leaderboard/me/**", timeout=15000)
    r.wait(2.4)
    r.scroll(560, 1800)
    r.wait(1.2)
    r.scroll(1100, 1800)
    r.wait(1.4)
    r.save(hold=0.5)

def wall(browser, Reel, cookie):
    r = _member(browser, Reel, cookie, "wall", f"/leagues/{ORG}/wall/")
    r.wait(1.2)
    r.scroll(420, 1500)
    r.wait(0.6)
    r.click(".gwp-react >> nth=0", ms=700)
    r.wait(0.8)
    r.scroll(900, 1800)
    r.wait(1.6)
    r.save(hold=0.5)


def messages(browser, Reel, cookie):
    r = _member(browser, Reel, cookie, "messages", f"/leagues/{ORG}/messages/room/")
    r.wait(1.6)
    box = r.page.locator("textarea:visible").last
    r.type(box, "Who's got the Cats on Saturday? Loser shouts the pies.", delay=48)
    r.wait(0.6)
    r.page.keyboard.press("Enter")
    r.wait(2.4)
    r.save(hold=0.5)


def charity_vote(browser, Reel, cookie):
    from film import django_shell
    # A vote already cast turns the form into "your vote's in"; clear Sam's so
    # every take starts with the ballot open.
    django_shell(f"from orgs.models import CharityVoteBallot; CharityVoteBallot.objects.filter(user__email={SAM!r}).delete()")
    r = _member(browser, Reel, cookie, "charity-vote", f"/leagues/{ORG}/charity-vote/")
    r.wait(1.6)
    r.click(".opt-card >> nth=1", ms=800)
    r.wait(0.9)
    r.click("button:has-text('Save my vote')", ms=700)
    r.wait(2.6)
    r.save(hold=0.5)


def _tour(browser, Reel, name, path, stops):
    """A public page, read top to bottom the way a visitor would: each stop puts
    a heading under the nav, holds, and (if it carries a number) takes a still.
    `stops` is [(heading words or None for the top, hold seconds, still number)]."""
    r = Reel(browser, name)
    r.goto(path, settle=0)
    # The pages are filmed as the open site, but the trailer's own menu and
    # footer are the ones the visitor is looking at, so swap those in from the
    # holding page. (Nothing on the page is real either way.)
    r.page.evaluate("""async () => {
      const doc = new DOMParser().parseFromString(await (await fetch('/coming-soon/')).text(), 'text/html');
      for (const sel of ['nav.nav', 'footer.footer']) {
        const a = document.querySelector(sel), b = doc.querySelector(sel);
        if (a && b) a.replaceWith(b);
      }
    }""")
    _loader(r)
    r.wait(0.4)
    for words, hold, still in stops:
        if words is None:
            r.scroll(0, 900)
        elif not _to_words(r, words):
            raise RuntimeError(f"{name}: no heading starting {words!r}")
        r.wait(hold)
        if still:
            r.shot(still)
    r.scroll(r.page.evaluate("document.scrollingElement.scrollHeight - innerHeight"), 1300)
    r.wait(0.8)
    r.save(hold=0.4, poster_at=1.2)


def how_it_works(browser, Reel, cookie):
    _tour(browser, Reel, "how-it-works", "/how-it-works/", [
        (None, 1.6, 1), ("Feel the flow", 1.8, 2), ("Set up your organisation. Run", 1.6, 0),
        ("Tip and track", 1.6, 0), ("Points that mean", 1.8, 3), ("Two roles", 1.5, 0),
        ("The leaderboard nobody", 1.5, 0)])


def news(browser, Reel, cookie):
    _tour(browser, Reel, "news", "/news/", [
        (None, 1.6, 1), ("What's moving", 3.4, 2), ("Meet this round", 1.4, 0),
        ("The stories only", 1.8, 3)])


def wall_public(browser, Reel, cookie):
    _tour(browser, Reel, "wall-public", "/wall/", [
        (None, 1.6, 1), ("Every room, one feed", 3.0, 2), ("Engagement is the whole", 1.8, 3)])


def about(browser, Reel, cookie):
    _tour(browser, Reel, "about", "/about/", [
        (None, 1.6, 1), ("A tipping comp is already", 1.6, 0), ("Three things we will not", 1.8, 2),
        ("Nobody in the comp", 1.6, 0), ("The questions we get", 1.8, 3)])


def terms(browser, Reel, cookie):
    _tour(browser, Reel, "terms", "/terms/", [
        (None, 1.5, 1), ("Three things worth", 1.7, 2), ("04 The platform fee", 1.4, 0),
        ("05 The donation", 1.6, 3), ("07 Behaving", 1.3, 0), ("10 What we don", 1.3, 0)])


def privacy(browser, Reel, cookie):
    _tour(browser, Reel, "privacy", "/privacy/", [
        (None, 1.5, 1), ("01 What we collect", 1.6, 2), ("03 Who can see", 1.5, 0),
        ("06 Your choices", 1.6, 3), ("07 Cookies", 1.3, 0)])


SCENES = {f.__name__: f for f in (
    home, signup, signin, create_org, create_group, fixtures, matchreader,
    picks, my_tips, ladder, leaderboard, wall, messages, charity_vote,
    how_it_works, news, wall_public, about, terms, privacy)}
