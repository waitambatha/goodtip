"""The waiting list: sign-up with a code, sign-in without one, the member page,
and the super-admin screens that list the people and send the invitation.

Client, 25 Sep 2026. goodtip.com.au shows only the coming-soon page while the
product is finished on staging, so a person who joins needs somewhere to come
back to that is NOT the product.
"""
import re
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase as DjangoTestCase, override_settings
from django.utils import timezone

from accounts import waitlist
from accounts.models import LaunchSignup, WaitlistCampaign
from accounts.waitlist_views import MEMBER_REEL

XHR = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
GATE_ON = dict(STAGING_GATE=True, STAGING_GATE_USERS="team:Team-Pass-1234")
PW = "correct-horse-9"


class TestCase(DjangoTestCase):
    """The per-address rate limits live in the cache, which outlives a test."""

    def setUp(self):
        super().setUp()
        cache.clear()


def code_from_outbox() -> str:
    body = mail.outbox[-1].body
    return re.search(r"\b(\d{6})\b", body).group(1)


def join(client, email="sam@example.com", name="Sam Tipper", password=PW, **extra):
    return client.post("/coming-soon/join/", {"name": name, "email": email, "password": password, **extra}, **XHR)


def verified(email="sam@example.com", name="Sam Tipper", password=PW, **kw) -> LaunchSignup:
    row = LaunchSignup(name=name, email=email, email_verified_at=timezone.now(), **kw)
    row.set_password(password)
    row.save()
    return row


class SignUpTests(TestCase):
    def test_sign_up_sends_a_code_and_waits_for_it(self):
        r = join(self.client, org_type="business")
        self.assertEqual(r.json(), {"ok": True, "email": "sam@example.com"})
        row = LaunchSignup.objects.get(email="sam@example.com")
        self.assertFalse(row.is_verified)
        self.assertFalse(row.has_account)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Confirm", mail.outbox[0].subject)
        # Nothing about the person can sign in until the code comes back.
        self.assertNotIn("waitlist_member_id", self.client.session)

    def test_the_right_code_verifies_signs_them_in_and_welcomes_them(self):
        join(self.client, org_type="community", current_platform="afl")
        code = code_from_outbox()
        r = self.client.post("/coming-soon/verify/", {"email": "sam@example.com", "code": code}, **XHR)
        self.assertTrue(r.json()["ok"])
        row = LaunchSignup.objects.get(email="sam@example.com")
        self.assertTrue(row.has_account)
        self.assertEqual((row.org_type, row.current_platform), ("community", "afl"))
        self.assertEqual(self.client.session["waitlist_member_id"], row.pk)
        self.assertEqual(len(mail.outbox), 2)                # code + welcome

    def test_a_wrong_code_does_not_verify_and_is_limited(self):
        join(self.client)
        for _ in range(LaunchSignup.CODE_MAX_ATTEMPTS):
            r = self.client.post("/coming-soon/verify/", {"email": "sam@example.com", "code": "000000"}, **XHR)
            self.assertFalse(r.json()["ok"])
        # Even the real code is refused once the attempts are spent.
        r = self.client.post("/coming-soon/verify/", {"email": "sam@example.com", "code": code_from_outbox()}, **XHR)
        self.assertFalse(r.json()["ok"])
        self.assertFalse(LaunchSignup.objects.get().is_verified)

    def test_bad_input_is_refused_in_words(self):
        for data, frag in (
            ({"name": "", "email": "a@b.com", "password": PW}, "name"),
            ({"name": "A", "email": "nope", "password": PW}, "email"),
            ({"name": "A", "email": "a@b.com", "password": "short"}, "password"),
        ):
            with self.subTest(data=data):
                r = self.client.post("/coming-soon/join/", data, **XHR).json()
                self.assertFalse(r["ok"])
                self.assertIn(frag, r["error"].lower())
        self.assertEqual(LaunchSignup.objects.count(), 0)

    def test_a_stranger_cannot_take_over_a_verified_account(self):
        row = verified()
        r = join(self.client, name="Mallory", password="attacker-pass-1")
        self.assertTrue(r.json()["ok"])                       # same answer as any other address
        row.refresh_from_db()
        self.assertEqual(row.name, "Sam Tipper")
        self.assertTrue(row.check_password(PW))
        self.assertFalse(row.check_password("attacker-pass-1"))
        # Only the inbox owner can apply it, by entering the code.
        self.client.post("/coming-soon/verify/", {"email": row.email, "code": code_from_outbox()}, **XHR)
        row.refresh_from_db()
        self.assertTrue(row.check_password("attacker-pass-1"))

    def test_the_old_lead_form_does_not_overwrite_a_verified_row(self):
        row = verified()
        self.client.post("/coming-soon/", {"name": "Someone Else", "email": row.email})
        row.refresh_from_db()
        self.assertEqual(row.name, "Sam Tipper")
        self.assertTrue(row.is_verified)

    def test_the_answer_does_not_reveal_whether_the_address_is_on_the_list(self):
        verified("here@example.com")
        a = join(self.client, email="here@example.com").json()
        b = join(self.client, email="new@example.com").json()
        self.assertTrue(a["ok"] and b["ok"])

    def test_resend_sends_a_fresh_code_after_the_wait(self):
        join(self.client)
        LaunchSignup.objects.update(code_sent_at=timezone.now() - timedelta(minutes=2))
        r = self.client.post("/coming-soon/resend/", {"email": "sam@example.com"}, **XHR)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(len(mail.outbox), 2)

    def test_resend_is_refused_straight_after_a_send(self):
        join(self.client)
        r = self.client.post("/coming-soon/resend/", {"email": "sam@example.com"}, **XHR)
        self.assertFalse(r.json()["ok"])
        self.assertEqual(len(mail.outbox), 1)

    def test_endpoints_do_not_answer_a_plain_get(self):
        for path in ("join", "verify", "resend", "signin"):
            self.assertEqual(self.client.get(f"/coming-soon/{path}/").status_code, 302)


class SignInTests(TestCase):
    def setUp(self):
        super().setUp()
        self.row = verified()

    def test_sign_in_needs_no_code(self):
        r = self.client.post("/coming-soon/signin/", {"email": "SAM@example.com ", "password": PW}, **XHR)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(self.client.session["waitlist_member_id"], self.row.pk)
        self.assertEqual(mail.outbox, [])

    def test_wrong_password_and_unknown_address_get_the_same_answer(self):
        a = self.client.post("/coming-soon/signin/", {"email": "sam@example.com", "password": "nope-nope-1"}, **XHR).json()
        b = self.client.post("/coming-soon/signin/", {"email": "ghost@example.com", "password": "nope-nope-1"}, **XHR).json()
        self.assertEqual(a, b)
        self.assertFalse(a["ok"])

    def test_an_unconfirmed_sign_up_cannot_sign_in(self):
        join(self.client, email="new@example.com")
        r = self.client.post("/coming-soon/signin/", {"email": "new@example.com", "password": PW}, **XHR)
        self.assertFalse(r.json()["ok"])

    def test_repeated_failures_lock_the_account_even_for_the_right_password(self):
        for _ in range(LaunchSignup.SIGNIN_MAX_FAILS):
            self.client.post("/coming-soon/signin/", {"email": "sam@example.com", "password": "wrong-pass-1"}, **XHR)
        r = self.client.post("/coming-soon/signin/", {"email": "sam@example.com", "password": PW}, **XHR).json()
        self.assertFalse(r["ok"])
        self.assertIn("Too many", r["error"])

    def test_sign_out_ends_the_session(self):
        self.client.post("/coming-soon/signin/", {"email": "sam@example.com", "password": PW}, **XHR)
        self.client.post("/coming-soon/signout/")
        self.assertNotIn("waitlist_member_id", self.client.session)
        self.assertEqual(self.client.get("/coming-soon/me/").status_code, 302)


class MemberPageTests(TestCase):
    def setUp(self):
        super().setUp()
        self.row = verified(org_type="business")
        self.client.post("/coming-soon/signin/", {"email": "sam@example.com", "password": PW}, **XHR)

    def test_it_needs_a_waiting_list_session(self):
        self.client.post("/coming-soon/signout/")
        r = self.client.get("/coming-soon/me/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, "/coming-soon/#signin")

    def test_it_shows_place_details_and_what_is_next(self):
        verified("first@example.com")
        LaunchSignup.objects.filter(email="first@example.com").update(created_at=timezone.now() - timedelta(days=2))
        body = self.client.get("/coming-soon/me/").content.decode()
        self.assertIn("#2", body)
        self.assertIn("What happens next", body)
        self.assertIn("Sam", body)
        self.assertIn("sam@example.com", body)

    def test_it_has_no_way_into_the_product(self):
        body = self.client.get("/coming-soon/me/").content.decode()
        for href in ("/login/", "/signup/", "/dashboard/", "/pricing/"):
            self.assertNotIn(f'href="{href}"', body)

    def test_it_offers_the_signup_link_only_once_invited_and_open(self):
        LaunchSignup.objects.update(notified_at=timezone.now())
        with override_settings(HOLDING_PAGE=True, **GATE_ON):
            self.assertNotIn('href="/signup/"', self.client.get("/coming-soon/me/").content.decode())
        with override_settings(STAGING_GATE=False, HOLDING_PAGE=False):
            self.assertIn('href="/signup/"', self.client.get("/coming-soon/me/").content.decode())

    def test_details_can_be_edited(self):
        r = self.client.post("/coming-soon/me/", {"name": "Samantha T", "org_type": "education", "current_platform": "nrl"})
        self.assertContains(r, "Saved.")
        self.row.refresh_from_db()
        self.assertEqual((self.row.name, self.row.org_type, self.row.current_platform), ("Samantha T", "education", "nrl"))

    def _states(self):
        r = self.client.get("/coming-soon/me/")
        return [(s["key"], s["state"]) for s in r.context["steps"]]

    def test_progress_for_someone_still_waiting(self):
        with override_settings(HOLDING_PAGE=True, **GATE_ON):
            self.assertEqual(self._states(), [
                ("joined", "done"), ("confirmed", "done"), ("invited", "current"), ("live", "todo")])

    def test_progress_once_invited_but_the_site_is_still_closed(self):
        LaunchSignup.objects.update(notified_at=timezone.now())
        with override_settings(HOLDING_PAGE=True, **GATE_ON):
            self.assertEqual(self._states(), [
                ("joined", "done"), ("confirmed", "done"), ("invited", "done"), ("live", "current")])

    def test_progress_when_the_site_is_open(self):
        LaunchSignup.objects.update(notified_at=timezone.now())
        with override_settings(STAGING_GATE=False, HOLDING_PAGE=False):
            self.assertTrue(all(state == "done" for _, state in self._states()))

    def test_the_countdown_shows_only_for_a_future_launch_date(self):
        # No date: no countdown. It never invents one.
        self.assertNotContains(self.client.get("/coming-soon/me/"), "data-countdown")
        camp = WaitlistCampaign.current()
        camp.launch_at = timezone.now() - timedelta(hours=1)
        camp.save()
        self.assertNotContains(self.client.get("/coming-soon/me/"), "data-countdown")
        camp.launch_at = timezone.now() + timedelta(days=3)
        camp.save()
        r = self.client.get("/coming-soon/me/")
        self.assertContains(r, "data-countdown")
        self.assertContains(r, "Sydney time")

    def test_the_theatre_and_the_trailer_link_are_there(self):
        r = self.client.get("/coming-soon/me/")
        self.assertContains(r, "Full trailer")
        self.assertContains(r, 'href="/coming-soon/"')
        self.assertContains(r, "A look inside")
        self.assertEqual(len(r.context["reel"]), len(MEMBER_REEL))

    def test_every_reel_clip_exists_as_a_chapter_and_as_files(self):
        from pathlib import Path

        from django.conf import settings

        from accounts.trailer import TRAILER_CHAPTERS

        chapters = {c["clip"] for c in TRAILER_CHAPTERS}
        folder = Path(settings.BASE_DIR) / "static" / "video" / "trailer"
        for clip in MEMBER_REEL:
            self.assertIn(clip, chapters)
            self.assertTrue((folder / f"{clip}.mp4").exists(), clip)
            self.assertTrue((folder / f"{clip}.jpg").exists(), clip)

    def test_a_waiting_list_session_is_not_a_product_session(self):
        # The whole point of keeping it separate: it must not open the app.
        with override_settings(STAGING_GATE=False, HOLDING_PAGE=False):
            r = self.client.get("/dashboard/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.url)


@override_settings(HOLDING_PAGE=True, **GATE_ON)
class HoldingPageIntegrationTests(TestCase):
    def test_the_gate_lets_the_waiting_list_endpoints_through(self):
        for path in ("/coming-soon/me/", "/coming-soon/join/", "/coming-soon/signin/"):
            with self.subTest(path=path):
                self.assertNotIn("/gate/", self.client.get(path).get("Location", ""))
        self.assertEqual(join(self.client).json()["ok"], True)

    def test_the_page_has_the_popups_and_no_team_login_line(self):
        body = self.client.get("/").content.decode()
        self.assertIn('id="wl"', body)
        self.assertIn('data-wl-open="join"', body)
        self.assertIn('data-wl-open="signin"', body)
        self.assertNotIn("Already part of the GoodTip team", body)
        self.assertNotIn('href="/login/"', body)

    def test_a_signed_in_member_is_offered_their_place(self):
        verified()
        self.client.post("/coming-soon/signin/", {"email": "sam@example.com", "password": PW}, **XHR)
        self.assertContains(self.client.get("/"), "Your place on the list")


class AdminScreenTests(TestCase):
    def setUp(self):
        super().setUp()
        from sysadmin.test_control_plane import sign_in_to_admin

        self.boss = get_user_model().objects.create_superuser(email="root@goodtip.test", password="x", display_name="Root")
        sign_in_to_admin(self.client, self.boss)
        self.a = verified("a@example.com", "Ann Able")
        self.b = LaunchSignup.objects.create(name="Bo Baker", email="b@example.com", notified_at=timezone.now())

    def test_the_list_shows_everyone_with_counts(self):
        body = self.client.get("/admin/waitlist/").content.decode()
        self.assertIn("Ann Able", body)
        self.assertIn("Bo Baker", body)

    def test_filters_and_search(self):
        body = self.client.get("/admin/waitlist/", {"show": "waiting"}).content.decode()
        self.assertIn("Ann Able", body)
        self.assertNotIn("Bo Baker", body)
        body = self.client.get("/admin/waitlist/", {"q": "baker"}).content.decode()
        self.assertIn("Bo Baker", body)
        self.assertNotIn("Ann Able", body)

    def test_csv_export(self):
        r = self.client.get("/admin/waitlist/", {"export": "csv"})
        self.assertEqual(r["Content-Type"], "text/csv")
        text = r.content.decode()
        self.assertIn("a@example.com", text)
        self.assertTrue(text.startswith("Place,Name,Email"))

    def test_someone_without_the_capability_gets_a_404(self):
        from sysadmin.test_control_plane import sign_in_to_admin

        other = get_user_model().objects.create_user(email="staff@goodtip.test", password="x", display_name="S")
        other.is_staff = True
        other.save()
        self.client.logout()
        sign_in_to_admin(self.client, other)
        self.assertEqual(self.client.get("/admin/waitlist/").status_code, 404)
        self.assertEqual(self.client.get("/admin/waitlist/invitation/").status_code, 404)

    def test_the_launch_date_can_be_set_and_cleared(self):
        when = (timezone.localtime() + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M")
        self.client.post("/admin/waitlist/invitation/", {"action": "launch_date", "launch_at": when})
        self.assertIsNotNone(WaitlistCampaign.current().launch_at)
        self.assertContains(self.client.get("/admin/waitlist/invitation/"), f'value="{when}"')
        self.client.post("/admin/waitlist/invitation/", {"action": "launch_date", "launch_at": ""})
        self.assertIsNone(WaitlistCampaign.current().launch_at)

    def test_a_bad_launch_date_changes_nothing_and_sends_nothing(self):
        self.client.post("/admin/waitlist/invitation/", {"action": "launch_date", "launch_at": "not a date"})
        self.assertIsNone(WaitlistCampaign.current().launch_at)
        self.assertEqual(len(mail.outbox), 0)

    def test_the_composer_opens_with_the_drafted_message(self):
        r = self.client.get("/admin/waitlist/invitation/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, waitlist.DEFAULT_SUBJECT)
        self.assertContains(r, "{first_name}")

    def test_the_preview_renders_the_edited_words(self):
        r = self.client.post("/admin/waitlist/invitation/preview/", {
            "subject": "S", "heading": "Big news", "body": "Hi {first_name},\n\nWe are open.", "button_label": "Go now",
        })
        html = r.content.decode()
        self.assertIn("Big news", html)
        self.assertIn("Hi Alex,", html)
        self.assertIn("Go now", html)

    def _post(self, **data):
        base = {"subject": "Hello {first_name}", "heading": "It's on", "body": "Hi {first_name},\n\nOpen now.", "button_label": "Sign up"}
        base.update(data)
        return self.client.post("/admin/waitlist/invitation/", base)

    def test_save_stores_the_edit_and_sends_nothing(self):
        self._post(action="save", heading="Edited heading")
        self.assertEqual(WaitlistCampaign.current().heading, "Edited heading")
        self.assertEqual(mail.outbox, [])

    def test_empty_fields_are_refused(self):
        self._post(action="save", subject="")
        self.assertEqual(WaitlistCampaign.current().subject, waitlist.DEFAULT_SUBJECT)

    def test_reset_restores_the_draft(self):
        self._post(action="save", heading="Edited")
        self._post(action="reset")
        self.assertEqual(WaitlistCampaign.current().heading, waitlist.DEFAULT_HEADING)

    def test_a_test_send_goes_only_to_the_admin(self):
        self._post(action="test")
        self.assertEqual([m.to for m in mail.outbox], [["root@goodtip.test"]])
        self.assertEqual(LaunchSignup.objects.filter(email="a@example.com", notified_at__isnull=False).count(), 0)

    def test_send_now_needs_the_confirm_box(self):
        self._post(action="send_now")
        self.assertEqual(mail.outbox, [])

    def test_send_now_mails_the_uninvited_once_using_the_edited_words(self):
        self._post(action="send_now", confirm="yes")
        self.assertEqual([m.to for m in mail.outbox], [["a@example.com"]])   # Bo was already invited
        self.assertEqual(mail.outbox[0].subject, "Hello Ann")
        self.a.refresh_from_db()
        self.assertIsNotNone(self.a.notified_at)
        self.assertEqual(WaitlistCampaign.current().status, WaitlistCampaign.STATUS_SENT)
        self._post(action="send_now", confirm="yes")
        self.assertEqual(len(mail.outbox), 1)                                # never twice

    def test_schedule_rejects_the_past_and_stores_a_future_time(self):
        past = (timezone.localtime() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        self._post(action="schedule", scheduled_for=past, confirm="yes")
        self.assertEqual(WaitlistCampaign.current().status, WaitlistCampaign.STATUS_DRAFT)
        future = (timezone.localtime() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        self._post(action="schedule", scheduled_for=future, confirm="yes")
        c = WaitlistCampaign.current()
        self.assertEqual(c.status, WaitlistCampaign.STATUS_SCHEDULED)
        self.assertGreater(c.scheduled_for, timezone.now())
        self.assertEqual(mail.outbox, [])
        self._post(action="unschedule")
        c.refresh_from_db()
        self.assertEqual((c.status, c.scheduled_for), (WaitlistCampaign.STATUS_DRAFT, None))


class ScheduledSendTests(TestCase):
    def setUp(self):
        super().setUp()
        verified("a@example.com", "Ann Able")
        self.c = WaitlistCampaign.current()

    def _run(self):
        out = StringIO()
        call_command("send_waitlist_invites", stdout=out)
        return out.getvalue()

    def test_nothing_is_sent_before_the_time(self):
        self.c.status, self.c.scheduled_for = WaitlistCampaign.STATUS_SCHEDULED, timezone.now() + timedelta(hours=1)
        self.c.save()
        self._run()
        self.assertEqual(mail.outbox, [])

    def test_nothing_is_sent_for_a_draft(self):
        self._run()
        self.assertEqual(mail.outbox, [])

    def test_it_sends_once_when_due(self):
        self.c.status, self.c.scheduled_for = WaitlistCampaign.STATUS_SCHEDULED, timezone.now() - timedelta(minutes=1)
        self.c.save()
        self._run()
        self._run()
        self.assertEqual(len(mail.outbox), 1)
        self.c.refresh_from_db()
        self.assertEqual((self.c.status, self.c.sent_count), (WaitlistCampaign.STATUS_SENT, 1))

    def test_it_runs_from_the_jobs_timer(self):
        from orgs.management.commands.run_due_jobs import JOBS

        self.assertIn("send_waitlist_invites", [j[0] if isinstance(j, (list, tuple)) else j for j in JOBS])

    def test_the_email_templates_render(self):
        row = LaunchSignup(name="Ann Able", email="a@example.com")
        msg = waitlist.build_invitation(self.c, row)
        html = next(c for c, m in msg.alternatives if m == "text/html")
        self.assertIn("/signup/", html)
        self.assertIn("Hi Ann,", msg.body)
