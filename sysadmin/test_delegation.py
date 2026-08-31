"""Delegated administration: who may do what, and what waits for approval.

The thing under test is a permission system, so most of these are written as
the refusal rather than the success — "a writer cannot delete members" is the
property that matters, and it is the one that silently stops being true.
"""
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from admin_panel.models import Enquiry, NewsPost
from sysadmin import access, capabilities, otp
from sysadmin.models import (
    AdminAccess, AdminAuditEvent, AdminGrant, AdminInvite, AdminTask, ChangeRequest,
)

User = get_user_model()


def sign_in(client, user):
    """Log in and clear the admin's emailed second factor."""
    client.force_login(user)
    session = client.session
    otp.mark_verified(session)
    session.save()


def make_admin(email, *, full=False, grants=(), reviewed=(), by=None):
    user = User.objects.create_user(email=email, password="pw", display_name=email.split("@")[0])
    user.is_staff = True
    user.is_superuser = full
    user.save()
    row = AdminAccess.objects.create(user=user, is_full_access=full, created_by=by)
    for key in grants:
        AdminGrant.objects.create(access=row, capability=key, requires_approval=key in reviewed)
    return user


# The staging gate sits in front of every request and would bounce the test
# client to /gate/ before any view under test ran.
@override_settings(STAGING_GATE=False)
class DelegationTestCase(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser(
            email="boss@example.com", password="pw", display_name="Boss")
        AdminAccess.objects.create(user=self.boss, is_full_access=True)


class CapabilityCatalogueTests(TestCase):
    """The catalogue is code that other code and several templates index into."""

    def test_every_capability_key_is_unique_and_namespaced(self):
        keys = [c.key for g in capabilities.GROUPS for c in g.capabilities]
        self.assertEqual(len(keys), len(set(keys)))
        for k in keys:
            self.assertIn(".", k, f"{k} should read group.action")

    def test_every_capability_belongs_to_the_group_that_lists_it(self):
        for group in capabilities.GROUPS:
            for cap in group.capabilities:
                self.assertEqual(cap.group, group.key)

    def test_unknown_keys_are_dropped_rather_than_trusted(self):
        """A grant saved against a capability that has since been renamed must
        not quietly become a grant for something else."""
        self.assertEqual(
            capabilities.valid(["news.write", "news.invented", ""]), ["news.write"]
        )

    def test_reading_and_syncing_cannot_be_put_behind_review(self):
        """There is no draft state for them — you have read it or you have
        not — so offering "needs approval" would be a lie in the picker."""
        for key in ("enquiries.read", "orgs.view", "people.view", "data.sync"):
            self.assertFalse(capabilities.ALL[key].reviewable, key)


class AccessRuleTests(DelegationTestCase):

    def test_a_superuser_predating_this_feature_is_treated_as_full_access(self):
        """Otherwise shipping this locks the owner out of their own admin."""
        legacy = User.objects.create_superuser(
            email="legacy@example.com", password="pw", display_name="Legacy")
        self.assertIsNone(access.access_for(legacy))
        self.assertTrue(access.is_full_access(legacy))
        self.assertTrue(access.can(legacy, "people.delete"))

    def test_full_access_holds_capabilities_it_was_never_granted(self):
        """Full access is a flag, not every box ticked — so it gains new powers
        the day they ship rather than the day somebody re-ticks them."""
        self.assertFalse(self.boss.admin_access.grants.exists())
        self.assertTrue(access.can(self.boss, "people.delete"))

    def test_a_restricted_admin_holds_only_what_was_ticked(self):
        writer = make_admin("w@example.com", grants=["news.write"])
        self.assertTrue(access.can(writer, "news.write"))
        self.assertFalse(access.can(writer, "news.delete"))
        self.assertFalse(access.can(writer, "people.delete"))

    def test_full_access_never_needs_approval(self):
        """A queue only they can clear is a queue that never clears."""
        self.assertFalse(access.needs_approval(self.boss, "news.write"))

    def test_a_suspended_admin_can_do_nothing(self):
        writer = make_admin("w@example.com", grants=["news.write"])
        writer.admin_access.is_active = False
        writer.admin_access.save()
        self.assertFalse(access.can(writer, "news.write"))
        self.assertFalse(access.is_admin(writer))

    def test_every_full_access_admin_can_review_not_just_the_creator(self):
        """The client runs GoodTip with their partner; a review that only one
        named person can do waits for them to come back from holiday."""
        partner = make_admin("partner@example.com", full=True, by=self.boss)
        make_admin("w@example.com", grants=["news.write"], by=self.boss)
        reviewers = set(access.full_access_admins().values_list("email", flat=True))
        self.assertEqual(reviewers, {"boss@example.com", "partner@example.com"})


class CreatingAnAdminTests(DelegationTestCase):

    def test_a_new_admin_cannot_sign_in_until_they_accept(self):
        """The window between "you are an admin" and "you have proved you hold
        the address" is the one worth closing."""
        sign_in(self.client, self.boss)
        self.client.post(reverse("admin:hq_team_new"), {
            "display_name": "Writer", "email": "writer@example.com",
            "access_level": "limited", "capability": ["news.write"],
        })
        writer = User.objects.get(email="writer@example.com")
        self.assertFalse(writer.is_active)
        self.assertFalse(writer.has_usable_password())

    def test_the_invitation_carries_a_code_that_is_never_stored(self):
        sign_in(self.client, self.boss)
        self.client.post(reverse("admin:hq_team_new"), {
            "display_name": "Writer", "email": "writer@example.com",
            "access_level": "limited", "capability": ["news.write"],
        })
        invite = AdminInvite.objects.get()
        self.assertTrue(invite.code_hash)
        self.assertEqual(len(mail.outbox), 1)
        # The plaintext exists in the email and nowhere else.
        self.assertNotIn(invite.code_hash, mail.outbox[0].body)

    def test_the_invitation_says_what_they_are_being_trusted_with(self):
        sign_in(self.client, self.boss)
        self.client.post(reverse("admin:hq_team_new"), {
            "display_name": "Writer", "email": "writer@example.com",
            "access_level": "limited",
            "capability": ["news.write", "enquiries.reply"],
            "review": ["news.write"],
        })
        body = mail.outbox[0].body
        self.assertIn("Reply to enquiries", body)
        self.assertIn("Write and edit stories", body)
        self.assertIn("After a review", body)

    def test_review_cannot_be_set_on_a_capability_they_do_not_hold(self):
        sign_in(self.client, self.boss)
        self.client.post(reverse("admin:hq_team_new"), {
            "display_name": "Writer", "email": "writer@example.com",
            "access_level": "limited",
            "capability": ["news.write"],
            "review": ["news.write", "people.delete"],
        })
        row = User.objects.get(email="writer@example.com").admin_access
        self.assertEqual([g.capability for g in row.grants.all()], ["news.write"])

    def test_an_address_that_already_has_an_account_is_refused(self):
        sign_in(self.client, self.boss)
        res = self.client.post(reverse("admin:hq_team_new"), {
            "display_name": "Boss again", "email": "boss@example.com",
            "access_level": "limited", "capability": ["news.write"],
        })
        self.assertContains(res, "already has a GoodTip account")
        self.assertEqual(AdminAccess.objects.count(), 1)

    def test_a_restricted_admin_cannot_create_administrators(self):
        """The power that would make every other restriction decorative."""
        writer = make_admin("w@example.com", grants=["news.write"])
        sign_in(self.client, writer)
        self.assertEqual(self.client.get(reverse("admin:hq_team")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:hq_team_new")).status_code, 403)
        self.client.post(reverse("admin:hq_team_new"), {
            "display_name": "Mate", "email": "mate@example.com",
            "access_level": "full",
        })
        self.assertFalse(User.objects.filter(email="mate@example.com").exists())

    def test_you_cannot_change_your_own_access(self):
        partner = make_admin("partner@example.com", full=True, by=self.boss)
        sign_in(self.client, partner)
        self.client.post(
            reverse("admin:hq_team_edit", args=[partner.admin_access.pk]),
            {"access_level": "limited", "capability": ["news.write"]},
        )
        partner.admin_access.refresh_from_db()
        self.assertTrue(partner.admin_access.is_full_access)

    def test_creating_an_admin_is_written_into_the_record(self):
        sign_in(self.client, self.boss)
        self.client.post(reverse("admin:hq_team_new"), {
            "display_name": "Writer", "email": "writer@example.com",
            "access_level": "limited",
            "capability": ["news.write"], "review": ["news.write"],
        })
        event = AdminAuditEvent.objects.get(action=AdminAuditEvent.ADMIN_CREATED)
        self.assertEqual(event.actor, self.boss)
        self.assertEqual(event.detail["granted"], ["news.write"])
        self.assertEqual(event.detail["reviewed"], ["news.write"])


class AcceptingAnInvitationTests(DelegationTestCase):

    def setUp(self):
        super().setUp()
        self.writer = make_admin("w@example.com", grants=["news.write"], by=self.boss)
        self.writer.is_active = False
        self.writer.set_unusable_password()
        self.writer.save()
        self.invite, self.code = AdminInvite.issue(self.writer.admin_access, by_user=self.boss)
        self.url = reverse("admin_invite_accept", args=[self.invite.token])

    def test_the_wrong_code_does_not_let_them_past(self):
        res = self.client.post(self.url, {"step": "code", "code": "WRONGWRO"})
        self.assertNotContains(res, "Choose a password")

    def test_a_password_cannot_be_set_without_the_code(self):
        self.client.post(self.url, {
            "step": "password", "password1": "Kestrel-Vault-8812",
            "password2": "Kestrel-Vault-8812",
        })
        self.writer.refresh_from_db()
        self.assertFalse(self.writer.is_active)
        self.assertFalse(self.writer.has_usable_password())

    def test_a_weak_password_is_refused(self):
        self.client.post(self.url, {"step": "code", "code": self.code})
        res = self.client.post(self.url, {
            "step": "password", "password1": "password", "password2": "password",
        })
        self.writer.refresh_from_db()
        self.assertFalse(self.writer.has_usable_password())
        self.assertContains(res, "Choose a password")

    def test_mismatched_passwords_are_refused(self):
        self.client.post(self.url, {"step": "code", "code": self.code})
        res = self.client.post(self.url, {
            "step": "password", "password1": "Kestrel-Vault-8812",
            "password2": "Kestrel-Vault-8813",
        })
        self.assertContains(res, "don&#x27;t match")

    def test_the_right_code_and_a_strong_password_switches_the_account_on(self):
        self.client.post(self.url, {"step": "code", "code": self.code})
        res = self.client.post(self.url, {
            "step": "password", "password1": "Kestrel-Vault-8812",
            "password2": "Kestrel-Vault-8812",
        }, follow=True)
        self.writer.refresh_from_db()
        self.assertTrue(self.writer.is_active)
        self.assertTrue(self.writer.check_password("Kestrel-Vault-8812"))
        self.assertEqual(res.redirect_chain[-1][0], reverse("admin:hq_my_work"))

    def test_an_invitation_only_works_once(self):
        self.client.post(self.url, {"step": "code", "code": self.code})
        self.client.post(self.url, {
            "step": "password", "password1": "Kestrel-Vault-8812",
            "password2": "Kestrel-Vault-8812",
        })
        self.assertEqual(self.client.get(self.url).status_code, 410)

    def test_too_many_wrong_codes_burns_the_invitation(self):
        for _ in range(AdminInvite.MAX_ATTEMPTS):
            self.client.post(self.url, {"step": "code", "code": "NOPENOPE"})
        self.assertEqual(self.client.get(self.url).status_code, 410)

    def test_a_made_up_token_is_a_404(self):
        self.assertEqual(self.client.get(
            reverse("admin_invite_accept", args=["nonsense"])).status_code, 404)


class ReviewedWorkTests(DelegationTestCase):
    """The approval loop: held, looked at, and either carried out or not."""

    def setUp(self):
        super().setUp()
        self.writer = make_admin(
            "w@example.com", grants=["news.write", "news.publish"],
            reviewed=["news.write"], by=self.boss,
        )
        self.wc = self.client_class()
        sign_in(self.wc, self.writer)
        self.bc = self.client_class()
        sign_in(self.bc, self.boss)

    def _submit(self, **over):
        data = {
            "title_html": "Finals race tightens",
            "excerpt_html": "Two games separate fourth from ninth.",
            "body": "<p>The top eight is pulling away.</p>",
            "tag": "AFL", "is_published": "on",
        }
        data.update(over)
        return self.wc.post(reverse("admin:hq_news_new"), data, follow=True)

    def test_a_reviewed_submission_changes_nothing_yet(self):
        self._submit()
        self.assertEqual(NewsPost.objects.count(), 0)
        cr = ChangeRequest.objects.get()
        self.assertEqual(cr.status, ChangeRequest.PENDING)
        self.assertEqual(cr.summary, "New story: Finals race tightens")

    def test_the_author_is_told_it_is_waiting_rather_than_done(self):
        res = self._submit()
        self.assertContains(res, "Sent for approval")

    def test_every_reviewer_is_emailed(self):
        partner = make_admin("partner@example.com", full=True, by=self.boss)
        mail.outbox.clear()
        self._submit()
        recipients = {addr for m in mail.outbox for addr in m.to}
        self.assertEqual(recipients, {"boss@example.com", "partner@example.com"})

    def test_an_unreviewed_capability_still_happens_immediately(self):
        """news.publish was granted without review, so it must not be held."""
        post = NewsPost.objects.create(title="Existing", is_published=False)
        self.wc.post(reverse("admin:hq_news_toggle", args=[post.pk]))
        post.refresh_from_db()
        self.assertTrue(post.is_published)
        self.assertFalse(ChangeRequest.objects.exists())

    def test_approving_carries_the_submission_out_for_real(self):
        self._submit()
        cr = ChangeRequest.objects.get()
        self.bc.post(reverse("admin:hq_review_detail", args=[cr.pk]),
                     {"decision": "approve"}, follow=True)
        cr.refresh_from_db()
        self.assertEqual(cr.status, ChangeRequest.APPROVED)
        self.assertIsNotNone(cr.applied_at)
        self.assertEqual(cr.apply_error, "")
        self.assertEqual(NewsPost.objects.count(), 1)
        self.assertEqual(NewsPost.objects.get().title, "Finals race tightens")

    def test_a_reviewer_can_edit_before_approving_and_the_edit_is_what_ships(self):
        """The common answer to a piece of writing is neither yes nor no."""
        self._submit()
        cr = ChangeRequest.objects.get()
        self.bc.post(reverse("admin:hq_review_detail", args=[cr.pk]), {
            "decision": "amend",
            "field_title_html": "Finals race tightens as the eight pulls away",
            "field_excerpt_html": "Two games separate fourth from ninth.",
            "field_body": "<p>The top eight is pulling away.</p>",
            "field_tag": "AFL", "field_is_published": "on",
            "feedback": "Sharpened the headline.",
        }, follow=True)
        cr.refresh_from_db()
        self.assertEqual(cr.status, ChangeRequest.AMENDED)
        self.assertEqual(NewsPost.objects.get().title,
                         "Finals race tightens as the eight pulls away")
        # And the author can see exactly what was altered.
        self.assertEqual([f[0] for f in cr.changed_fields()], ["title_html"])

    def test_the_original_submission_survives_being_amended(self):
        self._submit()
        cr = ChangeRequest.objects.get()
        self.bc.post(reverse("admin:hq_review_detail", args=[cr.pk]), {
            "decision": "amend", "field_title_html": "Something else",
        }, follow=True)
        cr.refresh_from_db()
        self.assertEqual(cr.post_data["title_html"], "Finals race tightens")

    def test_declining_changes_nothing_and_says_why(self):
        self._submit()
        cr = ChangeRequest.objects.get()
        mail.outbox.clear()
        self.bc.post(reverse("admin:hq_review_detail", args=[cr.pk]), {
            "decision": "decline", "feedback": "Check the date in paragraph two.",
        }, follow=True)
        cr.refresh_from_db()
        self.assertEqual(cr.status, ChangeRequest.DECLINED)
        self.assertEqual(NewsPost.objects.count(), 0)
        self.assertIn("Check the date", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, ["w@example.com"])

    def test_declining_without_a_reason_is_refused(self):
        """The note is the only thing the author has to work from."""
        self._submit()
        cr = ChangeRequest.objects.get()
        self.bc.post(reverse("admin:hq_review_detail", args=[cr.pk]),
                     {"decision": "decline", "feedback": "  "}, follow=True)
        cr.refresh_from_db()
        self.assertEqual(cr.status, ChangeRequest.PENDING)

    def test_the_author_is_always_told_the_outcome(self):
        for decision, expect in (("approve", "Approved"), ("decline", "Not published")):
            NewsPost.objects.all().delete()
            ChangeRequest.objects.all().delete()
            self._submit()
            cr = ChangeRequest.objects.get()
            mail.outbox.clear()
            self.bc.post(reverse("admin:hq_review_detail", args=[cr.pk]),
                         {"decision": decision, "feedback": "A note."}, follow=True)
            self.assertTrue(
                any(expect in m.subject for m in mail.outbox),
                f"{decision}: {[m.subject for m in mail.outbox]}",
            )

    def test_nobody_reviews_their_own_work(self):
        """A full-access admin submitting is never held, so this can only
        happen if somebody is promoted while their work is in the queue."""
        self._submit()
        cr = ChangeRequest.objects.get()
        self.writer.admin_access.is_full_access = True
        self.writer.admin_access.save()
        self.wc.post(reverse("admin:hq_review_detail", args=[cr.pk]),
                     {"decision": "approve"}, follow=True)
        cr.refresh_from_db()
        self.assertEqual(cr.status, ChangeRequest.PENDING)

    def test_a_restricted_admin_cannot_open_the_review_queue(self):
        self.assertEqual(self.wc.get(reverse("admin:hq_reviews")).status_code, 403)

    def test_a_decision_cannot_be_taken_twice(self):
        self._submit()
        cr = ChangeRequest.objects.get()
        self.bc.post(reverse("admin:hq_review_detail", args=[cr.pk]),
                     {"decision": "approve"}, follow=True)
        self.bc.post(reverse("admin:hq_review_detail", args=[cr.pk]),
                     {"decision": "approve"}, follow=True)
        self.assertEqual(NewsPost.objects.count(), 1)

    def test_a_failed_replay_is_recorded_rather_than_called_done(self):
        self._submit()
        cr = ChangeRequest.objects.get()
        cr.path = "/admin/news/999999/edit/"   # a post that is not there
        cr.save(update_fields=["path"])
        self.bc.post(reverse("admin:hq_review_detail", args=[cr.pk]),
                     {"decision": "approve"}, follow=True)
        cr.refresh_from_db()
        self.assertTrue(cr.apply_error)
        self.assertIsNone(cr.applied_at)
        self.assertEqual(NewsPost.objects.count(), 0)

    def test_a_credential_field_is_never_stored_in_the_queue(self):
        self.wc.post(reverse("admin:hq_news_new"), {
            "title_html": "Hi", "body": "<p>x</p>", "tag": "AFL",
            "password": "hunter2", "api_token": "secret",
        }, follow=True)
        cr = ChangeRequest.objects.get()
        self.assertNotIn("password", cr.post_data)
        self.assertNotIn("api_token", cr.post_data)

    def test_a_reviewed_page_edit_is_held_too(self):
        """The page editor posts JSON rather than a form, which request.POST
        cannot see — the raw body has to be kept instead."""
        editor = make_admin("e@example.com", grants=["pages.edit"],
                            reviewed=["pages.edit"], by=self.boss)
        ec = self.client_class()
        sign_in(ec, editor)
        ec.post(
            reverse("admin:hq_page_save"),
            data={"page": "how_it_works", "blocks": [
                {"key": "p-abc123", "html": "New words", "original": "Old words"}]},
            content_type="application/json",
        )
        cr = ChangeRequest.objects.get()
        self.assertTrue(cr.is_json)
        self.assertEqual(cr.post_data["page"], "how_it_works")
        self.assertTrue(cr.raw_body)


class TasksAndRecordTests(DelegationTestCase):

    def test_a_task_reaches_the_person_and_their_inbox(self):
        writer = make_admin("w@example.com", grants=["news.write"], by=self.boss)
        sign_in(self.client, self.boss)
        mail.outbox.clear()
        self.client.post(reverse("admin:hq_task_new"), {
            "assigned_to": writer.pk,
            "title": "Write something about the AFLW finals",
            "detail": "Two or three paragraphs.",
        })
        task = AdminTask.objects.get()
        self.assertEqual(task.assigned_to, writer)
        self.assertEqual(task.assigned_by, self.boss)
        self.assertEqual(mail.outbox[0].to, ["w@example.com"])

    def test_a_restricted_admin_cannot_hand_out_work(self):
        writer = make_admin("w@example.com", grants=["news.write"], by=self.boss)
        sign_in(self.client, writer)
        self.client.post(reverse("admin:hq_task_new"), {
            "assigned_to": self.boss.pk, "title": "Do my job",
        })
        self.assertFalse(AdminTask.objects.exists())

    def test_somebody_else_cannot_tick_off_your_task(self):
        writer = make_admin("w@example.com", grants=["news.write"], by=self.boss)
        other = make_admin("o@example.com", grants=["news.write"], by=self.boss)
        task = AdminTask.objects.create(
            assigned_to=writer, assigned_by=self.boss, title="Yours")
        sign_in(self.client, other)
        self.assertEqual(
            self.client.post(reverse("admin:hq_task_done", args=[task.pk])).status_code,
            403,
        )

    def test_the_record_survives_the_grant_being_taken_away(self):
        """The point of an audit log is that it outlives what it describes."""
        sign_in(self.client, self.boss)
        self.client.post(reverse("admin:hq_team_new"), {
            "display_name": "Writer", "email": "writer@example.com",
            "access_level": "limited", "capability": ["news.write"],
        })
        row = User.objects.get(email="writer@example.com").admin_access
        row.grants.all().delete()
        event = AdminAuditEvent.objects.get(action=AdminAuditEvent.ADMIN_CREATED)
        self.assertEqual(event.detail["granted"], ["news.write"])
        self.assertEqual(event.subject_name, "Writer")

    def test_the_record_names_who_approved_and_why_it_was_turned_down(self):
        writer = make_admin("w@example.com", grants=["news.write"],
                            reviewed=["news.write"], by=self.boss)
        wc = self.client_class()
        sign_in(wc, writer)
        wc.post(reverse("admin:hq_news_new"),
                {"title_html": "Hi", "body": "<p>x</p>", "tag": "AFL"}, follow=True)
        cr = ChangeRequest.objects.get()
        sign_in(self.client, self.boss)
        self.client.post(reverse("admin:hq_review_detail", args=[cr.pk]),
                         {"decision": "decline", "feedback": "Not this week."}, follow=True)
        event = AdminAuditEvent.objects.get(action=AdminAuditEvent.CHANGE_DECLINED)
        self.assertEqual(event.actor, self.boss)
        self.assertEqual(event.subject, writer)
        self.assertEqual(event.detail["feedback"], "Not this week.")

    def test_only_full_access_reads_the_record(self):
        writer = make_admin("w@example.com", grants=["news.write"], by=self.boss)
        sign_in(self.client, writer)
        self.assertEqual(self.client.get(reverse("admin:hq_activity")).status_code, 403)


class WhatTheMenuShowsTests(DelegationTestCase):
    """A link somebody cannot open teaches them the product is broken."""

    def test_a_writer_sees_news_and_not_the_rest(self):
        writer = make_admin("w@example.com", grants=["news.write"], by=self.boss)
        sign_in(self.client, writer)
        html = self.client.get(reverse("admin:hq_my_work")).content.decode()
        self.assertIn(reverse("admin:hq_news"), html)
        self.assertNotIn(reverse("admin:hq_sync"), html)
        self.assertNotIn(reverse("admin:hq_team"), html)

    def test_full_access_sees_the_team_screens(self):
        sign_in(self.client, self.boss)
        html = self.client.get(reverse("admin:hq_my_work")).content.decode()
        for name in ("hq_team", "hq_reviews", "hq_activity"):
            self.assertIn(reverse(f"admin:{name}"), html)

    def test_your_own_page_says_what_you_may_do_and_what_waits(self):
        writer = make_admin(
            "w@example.com", grants=["news.write", "enquiries.reply"],
            reviewed=["news.write"], by=self.boss)
        sign_in(self.client, writer)
        res = self.client.get(reverse("admin:hq_my_work"))
        self.assertContains(res, "Write and edit stories")
        self.assertContains(res, "Reply to enquiries")
        self.assertContains(res, "Goes to a full-access administrator")

    def test_a_refused_screen_explains_itself(self):
        writer = make_admin("w@example.com", grants=["news.write"], by=self.boss)
        sign_in(self.client, writer)
        res = self.client.get(reverse("admin:hq_pages"))
        self.assertEqual(res.status_code, 403)
        self.assertContains(res, "ask a full-access administrator", status_code=403)


class ReadAndWriteOnOneScreenTests(DelegationTestCase):
    """The enquiry page shows the message and carries the reply box.

    Gating the whole screen on the write capability would mean somebody given
    "read enquiries" could not open one, which is not what was ticked.
    """

    def setUp(self):
        super().setUp()
        self.enquiry = Enquiry.objects.create(
            name="Sam", email="sam@example.com", message="Can we run a season?",
        )

    def test_a_reader_can_open_an_enquiry(self):
        reader = make_admin("r@example.com", grants=["enquiries.read"], by=self.boss)
        sign_in(self.client, reader)
        res = self.client.get(
            reverse("admin:hq_enquiry_detail", args=[self.enquiry.pk]))
        self.assertEqual(res.status_code, 200)

    def test_a_reader_cannot_send_a_reply(self):
        reader = make_admin("r@example.com", grants=["enquiries.read"], by=self.boss)
        sign_in(self.client, reader)
        res = self.client.post(
            reverse("admin:hq_enquiry_detail", args=[self.enquiry.pk]),
            {"reply_body": "Yes, of course."},
        )
        self.assertEqual(res.status_code, 403)
        self.enquiry.refresh_from_db()
        self.assertEqual(self.enquiry.status, Enquiry.STATUS_NEW)

    def test_a_reviewed_replier_has_the_reply_held(self):
        replier = make_admin(
            "p@example.com", grants=["enquiries.read", "enquiries.reply"],
            reviewed=["enquiries.reply"], by=self.boss)
        sign_in(self.client, replier)
        self.client.post(
            reverse("admin:hq_enquiry_detail", args=[self.enquiry.pk]),
            {"reply_body": "Yes, of course."}, follow=True)
        self.enquiry.refresh_from_db()
        self.assertEqual(self.enquiry.status, Enquiry.STATUS_NEW)
        self.assertEqual(ChangeRequest.objects.count(), 1)
