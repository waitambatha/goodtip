"""Tests for the Postmark backend, the shared mail helper, and who gets what."""
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from catalog.models import Charity, Season, Series, Sport
from goodtip.email_backends import PostmarkEmailBackend
from goodtip.mail import build, email_configured, send_template, site_url
from orgs.models import (
    CharityVote, CharityVoteBallot, CharityVoteOption, OrgMember, Organisation,
)
from orgs.notifications import (
    send_election_reminders, send_election_result, send_round_results, send_welcome,
)
from tipping.models import Match, Round, Team, Tip
from tipping.services import record_match_result

POSTMARK = override_settings(
    EMAIL_BACKEND="goodtip.email_backends.PostmarkEmailBackend",
    POSTMARK_SERVER_TOKEN="test-token",
    DEFAULT_FROM_EMAIL="GoodTip <no-reply@goodtip.com.au>",
)


def _postmark_ok(n=1):
    return MagicMock(
        status_code=200,
        json=MagicMock(return_value=[{"ErrorCode": 0, "To": f"a{i}@b.com"} for i in range(n)]),
        raise_for_status=MagicMock(return_value=None),
    )


class PostmarkBackendTests(TestCase):
    def _send(self, messages, response=None):
        backend = PostmarkEmailBackend()
        backend.token = "test-token"
        with patch("goodtip.email_backends.requests.post") as post:
            post.return_value = response or _postmark_ok(len(messages))
            count = backend.send_messages(messages)
            payload = json.loads(post.call_args.kwargs["data"]) if post.called else None
            headers = post.call_args.kwargs["headers"] if post.called else {}
        return count, payload, headers, post

    def _msg(self, **kw):
        m = EmailMultiAlternatives(
            subject=kw.get("subject", "Hi"), body=kw.get("body", "plain"),
            to=kw.get("to", ["a0@b.com"]), reply_to=kw.get("reply_to"),
        )
        if kw.get("html", "<b>hi</b>"):
            m.attach_alternative(kw.get("html", "<b>hi</b>"), "text/html")
        return m

    def test_sends_both_parts_with_token_header(self):
        count, payload, headers, _ = self._send([self._msg()])
        self.assertEqual(count, 1)
        self.assertEqual(headers["X-Postmark-Server-Token"], "test-token")
        self.assertEqual(payload[0]["TextBody"], "plain")
        self.assertEqual(payload[0]["HtmlBody"], "<b>hi</b>")
        self.assertEqual(payload[0]["MessageStream"], "outbound")

    def test_reply_to_is_passed_through(self):
        _, payload, _, _ = self._send([self._msg(reply_to=["member@x.com"])])
        self.assertEqual(payload[0]["ReplyTo"], "member@x.com")

    def test_batches_into_one_request(self):
        msgs = [self._msg(to=[f"a{i}@b.com"]) for i in range(25)]
        count, payload, _, post = self._send(msgs, response=_postmark_ok(25))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(len(payload), 25)
        self.assertEqual(count, 25)

    def test_missing_token_drops_without_raising(self):
        backend = PostmarkEmailBackend()
        backend.token = ""
        with patch("goodtip.email_backends.requests.post") as post:
            self.assertEqual(backend.send_messages([self._msg()]), 0)
        post.assert_not_called()

    def test_per_message_rejection_is_not_counted(self):
        response = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[
                {"ErrorCode": 0, "To": "ok@b.com"},
                {"ErrorCode": 300, "To": "bad", "Message": "Invalid email"},
            ]),
            raise_for_status=MagicMock(return_value=None),
        )
        count, _, _, _ = self._send(
            [self._msg(to=["ok@b.com"]), self._msg(to=["bad"])], response=response,
        )
        # A 200 on the batch does not mean every message was accepted.
        self.assertEqual(count, 1)

    @POSTMARK
    def test_email_configured_requires_token(self):
        self.assertTrue(email_configured())
        with override_settings(POSTMARK_SERVER_TOKEN=""):
            self.assertFalse(email_configured())


class MailHelperTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="ada@x.com", password="x", display_name="Ada",
        )

    def test_build_renders_html_and_text(self):
        msg = build("welcome", subject="Welcome", to=self.user.email,
                    context={"user": self.user, "dashboard_url": "https://x/d/"})
        self.assertEqual(msg.body.count("GOODTIP"), 0)  # text part is plain
        self.assertIn("Ada", msg.body)
        html = msg.alternatives[0][0]
        self.assertIn("GOOD", html)
        self.assertIn("Ada", html)

    def test_templates_leave_no_unrendered_tags(self):
        msg = build("welcome", subject="W", to=self.user.email,
                    context={"user": self.user, "dashboard_url": "https://x/d/"})
        for part in (msg.body, msg.alternatives[0][0]):
            for token in ("{#", "{%", "{{"):
                self.assertNotIn(token, part)

    def test_build_returns_none_for_empty_recipient(self):
        self.assertIsNone(build("welcome", subject="W", to=[], context={}))

    def test_missing_template_is_logged_not_raised(self):
        self.assertIsNone(
            build("no_such_template", subject="W", to="a@b.com", context={})
        )

    def test_send_template_survives_a_bad_template(self):
        self.assertFalse(
            send_template("no_such_template", subject="W", to="a@b.com", context={})
        )

    def test_site_url_joins_cleanly(self):
        with override_settings(SITE_BASE_URL="https://goodtip.com.au/"):
            self.assertEqual(site_url("/dashboard/"), "https://goodtip.com.au/dashboard/")
            self.assertEqual(site_url("dashboard/"), "https://goodtip.com.au/dashboard/")
            self.assertEqual(site_url(), "https://goodtip.com.au")


class NotificationRecipientTests(TestCase):
    """The recipient rules are the part worth pinning down — mailing the wrong
    people is what gets a transactional sender flagged as spam."""

    def setUp(self):
        self.season = Season.objects.create(year=2098, label="2098")
        self.org = Organisation.objects.create(name="Mail League", season=self.season)
        self.voted = User.objects.create_user(
            email="voted@x.com", password="x", display_name="Voted",
        )
        self.not_voted = User.objects.create_user(
            email="notvoted@x.com", password="x", display_name="Waiting",
        )
        self.no_email = User.objects.create_user(
            email="blank@x.com", password="x", display_name="NoMail",
        )
        for u in (self.voted, self.not_voted, self.no_email):
            OrgMember.objects.create(user=u, org=self.org)
        # An account with no address at all must simply be skipped. Set via
        # update() to bypass the form/model validation, then refresh so the
        # in-memory instance matches the row.
        User.objects.filter(pk=self.no_email.pk).update(email="")
        self.no_email.refresh_from_db()

        self.charity_a = Charity.objects.create(name="Lifeline AU", slug="lifeline-au")
        self.charity_b = Charity.objects.create(name="Beyond Blue AU", slug="beyond-blue-au")
        self.vote = CharityVote.objects.create(
            org=self.org, status=CharityVote.STATUS_OPEN,
            scheduled_close_at=timezone.now() + timedelta(hours=20),
        )
        self.opt_a = CharityVoteOption.objects.create(vote=self.vote, charity=self.charity_a)
        self.opt_b = CharityVoteOption.objects.create(vote=self.vote, charity=self.charity_b)
        CharityVoteBallot.objects.create(vote=self.vote, user=self.voted, option=self.opt_a)

    def test_reminder_skips_members_who_already_voted(self):
        sent = send_election_reminders(self.vote, urgency="day")
        self.assertEqual(sent, 1)
        recipients = [addr for m in mail.outbox for addr in m.to]
        self.assertEqual(recipients, ["notvoted@x.com"])

    def test_reminder_stamps_the_vote_so_it_cannot_repeat(self):
        send_election_reminders(self.vote, urgency="day")
        self.vote.refresh_from_db()
        self.assertIsNotNone(self.vote.reminder_day_sent_at)
        self.assertIsNone(self.vote.reminder_hour_sent_at)

    def test_hour_reminder_wording_differs(self):
        send_election_reminders(self.vote, urgency="hour")
        self.assertIn("Last call", mail.outbox[0].subject)

    def test_reminder_rejects_unknown_urgency(self):
        with self.assertRaises(ValueError):
            send_election_reminders(self.vote, urgency="whenever")

    def test_result_email_goes_to_everyone_with_an_address(self):
        self.vote.status = CharityVote.STATUS_CLOSED
        self.vote.winning_charity = self.charity_a
        self.vote.save()
        sent = send_election_result(self.vote)
        self.assertEqual(sent, 2)  # the blank-email member is skipped
        self.assertIn("Lifeline AU", mail.outbox[0].subject)
        self.vote.refresh_from_db()
        self.assertIsNotNone(self.vote.result_email_sent_at)

    def test_result_email_skipped_without_a_winner(self):
        self.vote.status = CharityVote.STATUS_CLOSED
        self.vote.save()
        self.assertEqual(send_election_result(self.vote), 0)
        self.assertEqual(mail.outbox, [])

    def test_welcome_needs_an_address(self):
        self.assertEqual(send_welcome(self.no_email), 0)
        self.assertEqual(send_welcome(self.not_voted), 1)


class RoundResultEmailTests(TestCase):
    def setUp(self):
        self.sport, _ = Sport.objects.get_or_create(
            name="Result Footy", defaults={"slug": "result-footy"},
        )
        self.series, _ = Series.objects.get_or_create(
            name="Result Series", defaults={"sport": self.sport, "slug": "result-series"},
        )
        self.season = Season.objects.create(year=2097, label="2097")
        self.org = Organisation.objects.create(name="Scorecard League", season=self.season)
        self.tipper = User.objects.create_user(
            email="tipper@x.com", password="x", display_name="Tipper",
        )
        self.lurker = User.objects.create_user(
            email="lurker@x.com", password="x", display_name="Lurker",
        )
        OrgMember.objects.create(user=self.tipper, org=self.org)
        OrgMember.objects.create(user=self.lurker, org=self.org)
        self.home = Team.objects.create(name="Reds", slug="reds", series=self.series)
        self.away = Team.objects.create(name="Blues", slug="blues", series=self.series)
        self.round = Round.objects.create(
            org=self.org, round_number=5, series=self.series,
            lockout_at=timezone.now() - timedelta(days=2),
        )
        self.match = Match.objects.create(
            round=self.round, home_team=self.home, away_team=self.away,
            kickoff_at=timezone.now() - timedelta(days=1), venue="Suncorp",
        )
        Tip.objects.create(user=self.tipper, match=self.match, org=self.org, selection="home")
        record_match_result(self.match, 20, 10)

    def test_only_members_who_tipped_get_a_scorecard(self):
        sent = send_round_results(self.round)
        self.assertEqual(sent, 1)
        self.assertEqual(mail.outbox[0].to, ["tipper@x.com"])
        self.assertIn("Round 5", mail.outbox[0].subject)

    def test_scorecard_reports_the_correct_tally(self):
        send_round_results(self.round)
        body = mail.outbox[0].body
        self.assertIn("1 of 1", body)
        self.assertIn("Reds", body)
        self.assertIn("Suncorp", body)

    def test_stamps_the_round_so_it_cannot_repeat(self):
        send_round_results(self.round)
        self.round.refresh_from_db()
        self.assertIsNotNone(self.round.results_email_sent_at)
