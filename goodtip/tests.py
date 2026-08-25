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

    def test_production_has_no_robots_route(self):
        # The route is registered only under IS_STAGING. This asserts against
        # the real urlconf as imported in this (non-staging) environment, so it
        # fails if that condition is ever dropped.
        from django.urls import NoReverseMatch, reverse

        self.assertFalse(settings.IS_STAGING, "test env should not be staging")
        with self.assertRaises(NoReverseMatch):
            reverse("robots")
