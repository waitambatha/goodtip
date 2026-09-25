import re
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core import mail
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings

GATE_ON = dict(
    STAGING_GATE=True,
    STAGING_GATE_USERS="team:Team-Pass-1234,client:Client-Pass-5678",
)


@override_settings(STAGING_GATE=False)
class StagingGateOffTests(TestCase):
    def test_site_open_when_gate_disabled(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_gate_page_redirects_home_when_disabled(self):
        resp = self.client.get("/gate/")
        self.assertRedirects(resp, "/")


@override_settings(**GATE_ON)
class StagingGateOnTests(TestCase):
    def unlock(self, username="team", password="Team-Pass-1234", next_url="/"):
        return self.client.post("/gate/", {
            "username": username, "password": password, "next": next_url,
        })

    def test_locked_site_redirects_to_gate(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/gate/?next=/")

    def test_gate_page_renders(self):
        resp = self.client.get("/gate/?next=/pricing/")
        self.assertEqual(resp.status_code, 401)
        self.assertContains(resp, "Private preview", status_code=401)

    def test_wrong_credentials_rejected(self):
        resp = self.unlock(password="wrong")
        self.assertEqual(resp.status_code, 401)
        self.assertContains(resp, "didn't match", status_code=401)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_team_credentials_unlock_site(self):
        resp = self.unlock()
        self.assertRedirects(resp, "/")
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_client_credentials_unlock_site(self):
        resp = self.unlock(username="client", password="Client-Pass-5678")
        self.assertRedirects(resp, "/")
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_external_next_url_ignored(self):
        resp = self.unlock(next_url="https://evil.example.com/")
        self.assertRedirects(resp, "/")

    def test_stripe_webhook_exempt(self):
        # Stripe's servers can't pass the gate; the view must stay reachable.
        # (400 = signature check failed, which means the view itself ran.)
        resp = self.client.post("/stripe/webhook/", data="{}", content_type="application/json")
        self.assertNotEqual(resp.status_code, 302)

    def test_signup_reachable_after_unlock(self):
        self.unlock()
        resp = self.client.get("/signup/")
        self.assertEqual(resp.status_code, 200)


@override_settings(HOLDING_PAGE=True, **GATE_ON)
class HoldingModeTests(TestCase):
    """The trailer page in front of a locked site (client, 24 Sep 2026)."""

    def test_visitor_sees_the_trailer_at_the_front_door(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "tr-hero")
        self.assertContains(resp, 'name="org_type"')

    def test_the_real_pages_send_a_visitor_to_their_showcase(self):
        # A shared link or an old bookmark lands on the trailer with that
        # page's showcase open, not on a password box.
        for path, slug in (
            ("/how-it-works/", "how-it-works"), ("/news/", "blog"), ("/news/some-story/", "blog"),
            ("/wall/", "wall"), ("/about/", "about"), ("/terms/", "terms"), ("/privacy/", "privacy"),
        ):
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(resp.url, f"/coming-soon/#{slug}")

    def test_the_menu_and_footer_open_showcases_and_offer_no_login_or_signup(self):
        body = self.client.get("/").content.decode()
        nav = body[body.index('<nav class="nav"'):body.index("</nav>")]
        self.assertNotIn("/login/", nav)
        self.assertNotIn("/signup/", nav)
        self.assertIn("#join", nav)
        foot = body[body.index('<footer class="footer"'):body.index("</footer>")]
        for slug in ("how-it-works", "blog", "wall", "about", "terms", "privacy"):
            with self.subTest(slug=slug):
                self.assertIn(f'href="#{slug}"', nav + foot)
                self.assertContains(self.client.get("/"), f'data-peek="{slug}"')

    def test_a_visitor_is_not_offered_the_real_pages_from_a_showcase(self):
        body = self.client.get("/").content.decode()
        self.assertNotIn("Open the live page", body)
        for href in ("/how-it-works/", "/news/", "/wall/", "/about/", "/terms/", "/privacy/"):
            self.assertNotIn(f'href="{href}"', body)

    def test_everything_else_is_still_gated(self):
        for path in ("/login/", "/signup/", "/dashboard/", "/pricing/", "/media/messages/x.pdf", "/media/news/x.jpg"):
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 302)
                self.assertTrue(resp.url.startswith("/gate/"))

    def test_only_a_read_is_redirected_to_a_showcase(self):
        resp = self.client.post("/wall/1/reply/", {})
        self.assertTrue(resp.url.startswith("/gate/"))

    def test_the_team_reaches_the_real_pages_and_is_offered_them(self):
        self.client.post("/gate/", {"username": "team", "password": "Team-Pass-1234", "next": "/"})
        for path in ("/how-it-works/", "/about/", "/terms/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        body = self.client.get("/coming-soon/").content.decode()
        self.assertIn("Open the live page", body)
        self.assertIn('href="/how-it-works/"', body)

    def test_a_post_to_the_front_door_is_not_swallowed(self):
        self.assertEqual(self.client.post("/", {}).status_code, 302)

    def test_team_with_the_gate_password_still_sees_the_real_home_page(self):
        self.client.post("/gate/", {"username": "team", "password": "Team-Pass-1234", "next": "/"})
        body = self.client.get("/").content.decode()
        self.assertNotIn("tr-hero", body)

    def test_the_waiting_list_form_works_through_the_gate(self):
        from accounts.models import LaunchSignup
        resp = self.client.post("/coming-soon/", {
            "name": "Pat Visitor", "email": "pat@example.com", "source_page": "coming-soon",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(LaunchSignup.objects.filter(email="pat@example.com").exists())


@override_settings(HOLDING_PAGE=False, **GATE_ON)
class HoldingModeOffTests(TestCase):
    def test_off_means_the_gate_is_a_plain_wall(self):
        for path in ("/", "/coming-soon/", "/news/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 302)


class TrailerClipTests(SimpleTestCase):
    """Every chapter on the page has its recording and its poster on disk."""

    def test_each_chapter_has_a_video_and_a_poster(self):
        from accounts.trailer import TRAILER_CHAPTERS, TRAILER_HERO_CLIPS
        folder = Path(settings.BASE_DIR) / "static" / "video" / "trailer"
        stems = [c["clip"] for c in TRAILER_CHAPTERS]
        self.assertEqual(len(stems), len(set(stems)))
        for stem in stems:
            with self.subTest(clip=stem):
                self.assertTrue((folder / f"{stem}.mp4").is_file())
                self.assertTrue((folder / f"{stem}.jpg").is_file())
        for stem in TRAILER_HERO_CLIPS:
            self.assertIn(stem, stems)

    def test_each_showcase_has_its_tour_and_its_stills(self):
        from accounts.trailer import TRAILER_PEEKS
        folder = Path(settings.BASE_DIR) / "static" / "video" / "trailer"
        slugs = [p["slug"] for p in TRAILER_PEEKS]
        self.assertEqual(len(slugs), len(set(slugs)))
        for p in TRAILER_PEEKS:
            with self.subTest(slug=p["slug"]):
                self.assertTrue((folder / f"{p['clip']}.mp4").is_file())
                self.assertTrue((folder / f"{p['clip']}.jpg").is_file())
                self.assertEqual(len(p["shots"]), 3)
                for shot in p["shots"]:
                    self.assertTrue((folder / f"{shot['file']}.jpg").is_file())
                self.assertIn(p["prev"], slugs)
                self.assertIn(p["next"], slugs)


ALLOWLIST_ON = dict(
    EMAIL_BACKEND="goodtip.email_backends.AllowlistEmailBackend",
    EMAIL_ALLOWLIST_DELEGATE="django.core.mail.backends.locmem.EmailBackend",
    EMAIL_ALLOWLIST="me@example.com,@client.com.au",
)


@override_settings(**ALLOWLIST_ON)
class EmailAllowlistTests(SimpleTestCase):
    """Staging runs on a scrubbed clone of production, so "email every member
    of this org" is a live code path over thousands of real-shaped rows. These
    are the cases that decide whether one of those messages leaves the box."""

    def send(self, *recipients, subject="s"):
        from django.core.mail import EmailMessage, get_connection
        return get_connection().send_messages(
            [EmailMessage(subject, "body", "from@example.com", list(recipients))]
        )

    def test_listed_address_delivered(self):
        self.assertEqual(self.send("me@example.com"), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_listed_domain_delivered(self):
        self.assertEqual(self.send("anyone@client.com.au"), 1)

    def test_match_is_case_insensitive(self):
        self.assertEqual(self.send("ME@Example.COM"), 1)

    def test_unlisted_address_dropped(self):
        self.assertEqual(self.send("member@bigcorp.example"), 0)
        self.assertEqual(mail.outbox, [])

    def test_lookalike_domain_not_matched(self):
        # The domain test compares whole domains, so "@client.com.au" must not
        # let "notclient.com.au" through on a suffix match.
        self.assertEqual(self.send("evil@notclient.com.au"), 0)

    def test_mixed_recipients_dropped_entirely(self):
        # Not partially delivered: a message that reached half its To: line is
        # harder to reason about after the fact than one that was dropped.
        self.assertEqual(self.send("me@example.com", "member@bigcorp.example"), 0)
        self.assertEqual(mail.outbox, [])

    def test_no_recipients_dropped(self):
        self.assertEqual(self.send(), 0)

    @override_settings(EMAIL_ALLOWLIST="*")
    def test_wildcard_delivers_to_any_real_address(self):
        # The gate is the access boundary: everyone who can reach staging was
        # let in by whoever holds the gate password, so a second list of who
        # may be emailed only blocks testing invites, signup and sign-in.
        self.assertEqual(self.send("anyone@bigcorp.example"), 1)
        self.assertEqual(self.send("someone.else@another.example"), 1)

    @override_settings(EMAIL_ALLOWLIST="*")
    def test_wildcard_still_drops_scrubbed_addresses(self):
        # scrub_for_staging mints these for every anonymised member. They can
        # only hard-bounce, and staging shares production's Postmark token, so
        # the bounce would be charged against the live site.
        self.assertEqual(self.send("member42@staging.invalid"), 0)
        self.assertEqual(mail.outbox, [])

    @override_settings(EMAIL_ALLOWLIST="*")
    def test_wildcard_drops_a_batch_containing_a_scrubbed_address(self):
        # The whole-message rule still holds: one unroutable recipient sinks
        # the message rather than delivering it to the rest.
        self.assertEqual(self.send("real@example.com", "member42@staging.invalid"), 0)

    @override_settings(EMAIL_ALLOWLIST="@staging.invalid")
    def test_scrubbed_domain_cannot_be_allowlisted_back_in(self):
        self.assertEqual(self.send("member42@staging.invalid"), 0)

    @override_settings(EMAIL_ALLOWLIST="")
    def test_empty_allowlist_blocks_everything(self):
        # The failure mode of a forgotten env var must be silence, not a send
        # to the entire membership.
        self.assertEqual(self.send("me@example.com"), 0)
        self.assertEqual(mail.outbox, [])


class ScrubGuardTests(SimpleTestCase):
    """`scrub_for_staging` is irreversible, so what it refuses matters more
    than what it rewrites. There is no --force: these must not be bypassable."""

    def run_scrub(self):
        from django.core.management import call_command
        call_command("scrub_for_staging")

    @override_settings(IS_STAGING=False, GOODTIP_ENV="production")
    def test_refuses_outside_staging(self):
        with self.assertRaises(CommandError) as caught:
            self.run_scrub()
        self.assertIn("staging", str(caught.exception))

    @override_settings(IS_STAGING=True, GOODTIP_ENV="staging")
    def test_refuses_when_pointed_at_the_production_database(self):
        # The env var says staging but DATABASE_URL still says goodtip_db --
        # a copied .env, which is the realistic way this goes wrong.
        with mock.patch.dict(connection.settings_dict, {"NAME": "goodtip_db"}):
            with self.assertRaises(CommandError) as caught:
                self.run_scrub()
        self.assertIn("goodtip_db", str(caught.exception))


@override_settings(**GATE_ON)
class StagingRobotsTests(SimpleTestCase):
    """staging.goodtip.com.au must be able to say "do not index me" to a
    crawler that has not passed the gate -- and production must not gain a
    robots.txt it never had."""

    def test_robots_is_exempt_from_the_gate(self):
        # The whole point: a crawler redirected to the lock page never reads
        # the Disallow, so this path has to answer without the cookie.
        from django.test import RequestFactory

        from goodtip.staging_gate import StagingGateMiddleware

        sentinel = object()
        middleware = StagingGateMiddleware(lambda request: sentinel)
        request = RequestFactory().get("/robots.txt")
        self.assertIs(middleware(request), sentinel)

    def test_gate_still_intercepts_everything_else(self):
        from django.test import RequestFactory

        from goodtip.staging_gate import StagingGateMiddleware

        middleware = StagingGateMiddleware(lambda request: self.fail("gate let it through"))
        response = middleware(RequestFactory().get("/dashboard/"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/gate/"))

    def test_robots_disallows_everything(self):
        from django.test import RequestFactory

        from goodtip.staging_gate import robots_view

        response = robots_view(RequestFactory().get("/robots.txt"))
        body = response.content.decode()
        self.assertEqual(response["Content-Type"], "text/plain")
        self.assertIn("User-agent: *", body)
        self.assertIn("Disallow: /", body)
        # Nothing that could be read as permitting a path.
        self.assertNotIn("Allow:", body)

    def test_robots_route_is_registered_only_on_staging(self):
        # goodtip/urls.py adds this route at import time, so override_settings
        # cannot flip it -- whichever environment the suite runs in decides
        # which half of the invariant is checkable here, and we assert that
        # half rather than demanding a non-staging env. The half that matters,
        # "production must not gain a robots.txt it never had", is still
        # enforced: production's own deploy gate runs this same suite with
        # IS_STAGING false. Asserting it unconditionally is what made the
        # staging deploy gate unusable above `warn`.
        from django.urls import NoReverseMatch, reverse

        if settings.IS_STAGING:
            self.assertEqual(reverse("robots"), "/robots.txt")
        else:
            with self.assertRaises(NoReverseMatch):
                reverse("robots")


class NoTestMayDeleteRealUploadsTests(SimpleTestCase):
    """No test class may remove MEDIA_ROOT without first replacing it.

    This is a scan of the source, not a behavioural test, and it is here
    because the failure it prevents is invisible and unrecoverable.

    What happened: `MessageVideoTests` called
    `shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)` in tearDownClass
    with no MEDIA_ROOT override, so it deleted the real uploads directory of
    whatever checkout ran the suite. Staging's deploy gate runs the suite on
    every deploy — so every deploy silently destroyed every file anybody had
    uploaded since the last one, and `ignore_errors=True` meant nothing ever
    reported it. Blog images, avatars, charity logos, organisation logos.

    `goodtip.testing.drop_temp_media` now refuses to delete a path outside the
    system temp directory, which stops it at runtime. This stops it at review
    time, and names the two things that are actually forbidden: a raw rmtree
    of MEDIA_ROOT anywhere, and a class that drops its media without having
    overridden it.
    """

    #: The two files that STATE the rule, which a scan for the rule will
    #: always match on. A rule cannot police the file that defines it.
    EXEMPT = {"goodtip/testing.py", "goodtip/tests.py"}

    def _test_sources(self):
        root = Path(settings.BASE_DIR)
        for path in sorted(root.rglob("test*.py")):
            rel = str(path.relative_to(root))
            if "venv" in path.parts or "site-packages" in path.parts:
                continue
            # testing.py is the helper, not a suite.
            if path.name == "testing.py" or rel in self.EXEMPT:
                continue
            yield path, path.read_text(encoding="utf-8")

    def test_nothing_rmtrees_media_root_directly(self):
        """Use goodtip.testing.drop_temp_media, which checks where it is
        pointed before it deletes anything."""
        offenders = [
            str(p.relative_to(settings.BASE_DIR))
            for p, src in self._test_sources()
            if "rmtree(settings.MEDIA_ROOT" in src
        ]
        self.assertEqual(offenders, [], "raw rmtree of the real MEDIA_ROOT")

    def test_every_class_that_drops_media_has_replaced_it_first(self):
        """Walked line by line rather than split with a regex.

        The first attempt split the file on a lookahead for optional
        decorators followed by `class`, which also matches at the newline
        BETWEEN a decorator and its class — so every decorated class was split
        away from its own decorator and reported as an offender. A scan that
        cries wolf on the correct code is worse than no scan.
        """
        offenders = []
        for path, src in self._test_sources():
            lines = src.split("\n")
            # Where each top-level class starts, and the decorators above it.
            starts = [i for i, ln in enumerate(lines) if ln.startswith("class ")]
            for n, start in enumerate(starts):
                end = starts[n + 1] if n + 1 < len(starts) else len(lines)
                body = "\n".join(lines[start:end])
                if "drop_temp_media()" not in body:
                    continue
                # Decorators are the unbroken run of @-lines directly above.
                head, i = [], start - 1
                while i >= 0 and (lines[i].startswith("@") or lines[i].startswith(")")):
                    head.append(lines[i])
                    i -= 1
                if "MEDIA_ROOT" not in "\n".join(head):
                    name = lines[start].split("(")[0].replace("class ", "")
                    offenders.append(f"{path.relative_to(settings.BASE_DIR)}::{name}")
        self.assertEqual(
            offenders, [],
            "these delete MEDIA_ROOT without @override_settings(MEDIA_ROOT=temp_media())",
        )

    #: How a test says "write a file through a model field". Not exhaustive by
    #: construction — it is the set that appears in this codebase, and a new
    #: field name is a line here.
    WRITES = (".image.save(", ".avatar.save(", ".logo.save(", ".file.save(",
              "SimpleUploadedFile")

    def test_every_class_that_writes_an_upload_has_replaced_media_root_first(self):
        """Deleting the real MEDIA_ROOT is the loud version of this mistake.

        The quiet version is writing to it: `StoryFallbackImageTests` saved a
        `real.png` through an ImageField with no override, so every run of the
        suite left one more file in the running instance's own uploads
        directory — staging's `media/news/real.png`, and a `real_nOSIpc3.png`
        beside it from the second run. Nothing breaks, which is why it would
        have gone on indefinitely; a test's files belong in a temporary
        directory whether or not it cleans them up.
        """
        offenders = []
        for path, src in self._test_sources():
            lines = src.split("\n")
            starts = [i for i, ln in enumerate(lines) if ln.startswith("class ")]
            for n, start in enumerate(starts):
                end = starts[n + 1] if n + 1 < len(starts) else len(lines)
                body = "\n".join(lines[start:end])
                if not any(mark in body for mark in self.WRITES):
                    continue
                head, i = [], start - 1
                while i >= 0 and (lines[i].startswith("@") or lines[i].startswith(")")):
                    head.append(lines[i])
                    i -= 1
                if "MEDIA_ROOT" not in "\n".join(head):
                    name = lines[start].split("(")[0].replace("class ", "")
                    offenders.append(f"{path.relative_to(settings.BASE_DIR)}::{name}")
        self.assertEqual(
            offenders, [],
            "these write uploads into the real MEDIA_ROOT; add "
            "@override_settings(MEDIA_ROOT=temp_media())",
        )


class ContrastTests(SimpleTestCase):
    """No text in any theme may fall under WCAG AA.

    THE BUG THIS IS FOR. The client, Sep 2026: "make sure on the different
    themes text — and I mean ALL text — can be seen. That has been an issue
    where, let's say it's a light theme, the text has a shade that is not easy
    to see, even on the cream. So go page by page confirming that."

    Page by page is how the first six are found and the seventh is missed.
    There are four palettes — the member app's green and light, the admin
    shell's light and dark — and the same rule paints on a different ground in
    each, so a screen that looks right is evidence about one of the four. The
    arithmetic is in scripts/check_contrast.py; this is the part that keeps
    the answer true, because the way this regresses is a new rule written
    against whichever theme its author had open.

    WHEN THIS FAILS: run `venv/bin/python scripts/check_contrast.py` and it
    will name the rule, the value, the ground and the ratio. The fix is
    usually to darken or lighten the value; where it is a token that many
    rules read, move the token rather than the rules. Where the checker is
    wrong — the ground is set by a parent, or the mark is an icon rather than
    a word — say so in PAINTED_ON, NON_TEXT or DECORATION with the reason,
    which is a decision rather than a suppression.
    """

    def test_every_theme_clears_wcag_aa(self):
        import sys

        sys.path.insert(0, str(settings.BASE_DIR / "scripts"))
        try:
            import check_contrast
        finally:
            sys.path.pop(0)

        bad = []
        for theme in check_contrast.themes():
            for f in check_contrast.findings(theme):
                bad.append(
                    f"{theme.name}: {f['ratio']}:1  {f['file']}:{f['line']}  "
                    f"{f['selector']}  {f['value']}  on {f['ground']} "
                    f"(needs {f['bar']}:1)"
                )
        self.assertEqual(bad, [], "\n" + "\n".join(bad))

    def test_every_story_tag_chip_is_readable_when_ticked(self):
        """A ticked tag in the story editor is white text on that code's own
        colour (--ntc, one per code). The checker cannot follow a variable set
        per chip, so it is measured here directly — and it needed to be: AFLW
        was 3.5:1 and NRLW 4.1:1 until Sep 2026, so two of the five codes an
        editor ticks every day were labels you had to squint at."""
        import re
        import sys

        sys.path.insert(0, str(settings.BASE_DIR / "scripts"))
        try:
            from check_contrast import parse_colour, ratio
        finally:
            sys.path.pop(0)

        css = (settings.BASE_DIR / "static/css/goodtip.css").read_text()
        chips = re.findall(
            r"\.ned-tag-check\.(t-[\w-]+)\s+span\s*\{\s*--ntc:\s*(#[0-9A-Fa-f]{6})", css)
        self.assertGreaterEqual(len(chips), 5, "the tag chip colours have moved")
        bad = [
            f"{cls} {hexv} {ratio((255, 255, 255), parse_colour(hexv)[:3])}:1"
            for cls, hexv in chips
            if ratio((255, 255, 255), parse_colour(hexv)[:3]) < 4.5
        ]
        self.assertEqual(bad, [])



class LoaderSceneTests(TestCase):
    """The eight competition loaders.

    Client, 20 Sep 2026: "let's only have NRL and AFL ... they will be like 8
    loaders. For the NRLW, either male or female, on their loader we can add
    the sign of male or female."
    """

    #: Four AFL, four NRL. Not one per Series: Super Netball and Super League
    #: are rows in the database with zero rounds and zero fixtures between
    #: them, and State of Origin is three games a year inside the NRL.
    SCENES = ("afl-set", "afl-bounce", "afl-mark", "afl-flags",
              "nrl-convert", "nrl-count", "nrl-try", "nrl-pass")

    def test_every_scene_reaches_the_public_site(self):
        body = self.client.get("/pricing/").content
        for key in self.SCENES:
            self.assertIn(('data-lscene="%s"' % key).encode(), body, key)

    def test_every_scene_reaches_the_sign_in_screen(self):
        body = self.client.get("/login/").content
        for key in self.SCENES:
            self.assertIn(('data-lscene="%s"' % key).encode(), body, key)

    def test_the_codes_that_carry_no_fixtures_are_gone(self):
        """A splash advertising a competition the product does not run — and a
        round ball through a ring reads as basketball long before netball."""
        body = self.client.get("/pricing/").content
        for key in ("super-netball", "super-league", "state-of-origin"):
            self.assertNotIn(('data-lscene="%s"' % key).encode(), body, key)

    def test_both_sex_marks_are_drawn(self):
        """Drawing one only on the women's scenes would make the women's
        competition read as the variant and the men's as the default."""
        body = self.client.get("/pricing/").content
        self.assertIn(b'class="lsex-w"', body)
        self.assertIn(b'class="lsex-m"', body)
        # One mark for the stage, not one per scene — eight copies is seven
        # chances for them to drift apart.
        self.assertEqual(body.count(b'class="lsex"'), 1)

    def test_no_two_scenes_share_a_motion(self):
        """Client, on the first attempt: "zero new animation, just the old one
        and some text showing colour change." Four of six were a ball kicked
        through something. Each scene names its own motion class now."""
        from pathlib import Path

        from django.conf import settings

        markup = (
            Path(settings.BASE_DIR) / "templates/partials/_loader_scenes.html"
        ).read_text()
        for motion in ("l-arc", "l-bounce", "l-fall", "l-convert",
                       "l-roll", "l-place", "l-chain"):
            self.assertEqual(
                markup.count(" " + motion + '"'), 1,
                f"{motion} should be used by exactly one scene",
            )
        # The goal umpire has no ball at all — it is what happens after one,
        # and it is the only scene that is a person signalling.
        flags = markup[markup.index('data-lscene="afl-flags"'):]
        flags = flags[:flags.index("</div>")]
        self.assertNotIn("lball", flags)

    def test_every_moving_part_is_exempt_from_the_reduced_motion_sweep(self):
        """The blanket rule in goodtip.css freezes anything without `.lanim`.
        The first six shipped invisible because the exemption still named the
        OLD loader's classes, so this pins the two halves together."""
        from pathlib import Path

        from django.conf import settings

        base = Path(settings.BASE_DIR)
        css = (base / "static/css/goodtip.css").read_text()
        self.assertIn(":not(.lanim)", css)
        markup = (base / "templates/partials/_loader_scenes.html").read_text()
        # Anything carrying an animation class is also marked lanim.
        for motion in ("l-arc", "l-bounce", "l-fall", "l-convert",
                       "l-roll", "l-place", "l-chain"):
            line = [l for l in markup.splitlines() if " " + motion + '"' in l][0]
            self.assertIn("lanim", line, motion)

    def test_the_splash_is_given_time_to_be_seen(self):
        """Client, 20 Sep 2026: "also give time for the loaders to load so we
        can see the loader." At 400ms on a warm cache it was gone before the
        shot had left the boot."""
        import re

        body = self.client.get("/pricing/").content.decode()
        held = int(re.search(r'data-min="(\d+)"', body).group(1))
        self.assertGreaterEqual(held, 2000)
        self.assertIn("data-full-shot", body)

    def test_the_public_site_cycles_and_names_no_scene_up_front(self):
        """The choice is the script's, one step per load. A server-rendered
        data-scene would pin every visitor to the same competition."""
        body = self.client.get("/pricing/").content
        self.assertNotIn(b"data-scene-fixed", body)

    def test_the_member_app_gets_a_bar_and_not_a_splash(self):
        """Client, 20 Sep 2026: "the loaders in the private pages are
        horrible ... we need a clean loader from page to page in the private
        page."

        The scenes are a front door — a first impression and the six
        competitions in rotation. Inside the app, where a member crosses
        between screens twenty times a day, a full-screen splash covers the
        furniture they just used to navigate. So the app gets a bar and the
        public site keeps the splash, and this pins both halves: if the scenes
        ever come back in here, or the bar leaks out there, one of these fails.
        """
        from django.contrib.auth import get_user_model
        from catalog.models import Competition, Season, Sport
        from orgs.models import OrgMember, Organisation

        season = Season.objects.create(year=2098, label="2098")
        sport, _ = Sport.objects.get_or_create(
            slug="rugby-league", defaults={"name": "Rugby League"},
        )
        comp = Competition.objects.create(
            sport=sport, season=season, name="NRL 2098", slug="nrl-2098",
        )
        org = Organisation.objects.create(name="Leaguies", season=season)
        org.competitions.add(comp)
        user = get_user_model().objects.create_user(
            email="scene@x.com", password="x", display_name="Scene",
        )
        OrgMember.objects.create(user=user, org=org)
        self.client.force_login(user)

        body = self.client.get("/dashboard/").content
        self.assertIn(b"loader-slim", body)
        # No scene, no stage, no competition caption — the bar IS the loader.
        self.assertNotIn(b"data-lscene", body)
        self.assertNotIn(b"data-scene-fixed", body)
        self.assertNotIn(b'id="loaderSport"', body)

    def test_the_public_site_still_gets_the_scenes(self):
        body = self.client.get("/pricing/").content
        self.assertNotIn(b"loader-slim", body)
        self.assertIn(b"data-lscene", body)
