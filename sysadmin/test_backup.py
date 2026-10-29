"""The two databases (client, 25 Oct 2026): every table backed up from live
into staging and labelled Backup; staging's own rows labelled Testing; and
real data deleted only by Ian, or with his approval.

"Live" here is the test-only `backup_source` database (goodtip/settings.py),
standing in for live: rows are made there, then copied by
sysadmin.backup.copy_everything into the default database, as the backup
job does on staging.
"""
from datetime import timedelta
from decimal import Decimal  # noqa: F401 — kept for readers extending these

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from catalog.models import Season
from goodtip.origin import ORIGIN_LIVE
from orgs.models import Organisation, WallPost
from sysadmin import backup
from sysadmin.models import AdminAccess, BackupRecord, DeletionRequest
from sysadmin.test_delegation import make_admin, sign_in

User = get_user_model()
SRC = "backup_source"


class _TwoDatabases(TestCase):
    databases = {"default", SRC}

    def setUp(self):
        # Reference data exists on both sides (loaded from the feeds there).
        for db in ("default", SRC):
            Season.objects.using(db).get_or_create(year=2099, defaults={"label": "Backup Season"})
        src_season = Season.objects.using(SRC).get(year=2099)
        self.live_user = User.objects.db_manager(SRC).create_user(email="real@live.test", password="x")
        self.live_org = Organisation.objects.using(SRC).create(name="Real Org", season=src_season)
        self.long_ago = timezone.now() - timedelta(days=40)
        self.live_post = WallPost.objects.using(SRC).create(org=self.live_org, author=self.live_user, body="Real post")
        WallPost.objects.using(SRC).filter(pk=self.live_post.pk).update(created_at=self.long_ago)
        # The six origin tables are copied by sync_live_backup's own steps;
        # stand in for that here so the generic copy can map onto them.
        season = Season.objects.get(year=2099)
        self.user = User.objects.create_user(email="real@live.test", password="x")
        User.objects.filter(pk=self.user.pk).update(origin=ORIGIN_LIVE, live_id=self.live_user.pk)
        self.org = Organisation.objects.create(name="Real Org", season=season)
        Organisation.objects.filter(pk=self.org.pk).update(origin=ORIGIN_LIVE, live_id=self.live_org.pk)


class CopyEverythingTests(_TwoDatabases):
    def test_a_live_wall_post_arrives_on_staging_as_backup_with_its_own_date(self):
        backup.copy_everything(SRC)
        post = WallPost.objects.get(body="Real post")
        self.assertEqual(post.org_id, self.org.pk)
        self.assertEqual(post.author_id, self.user.pk)
        self.assertEqual(backup.label_for(WallPost, post), backup.BACKUP)
        self.assertLess(abs((post.created_at - self.long_ago).total_seconds()), 1)

    def test_running_again_updates_rather_than_duplicates(self):
        backup.copy_everything(SRC)
        WallPost.objects.using(SRC).filter(pk=self.live_post.pk).update(body="Edited on live")
        backup.copy_everything(SRC)
        self.assertEqual(WallPost.objects.count(), 1)
        self.assertEqual(WallPost.objects.get().body, "Edited on live")
        self.assertEqual(BackupRecord.objects.filter(model_label="orgs.wallpost").count(), 1)

    def test_copying_notifies_nobody(self):
        from orgs.models import Notification

        before = Notification.objects.count()
        backup.copy_everything(SRC)
        self.assertEqual(Notification.objects.count(), before)

    def test_a_post_made_on_staging_is_testing(self):
        backup.copy_everything(SRC)
        mine = WallPost.objects.create(org=self.org, author=self.user, body="Trying things")
        self.assertEqual(backup.label_for(WallPost, mine), backup.TESTING)
        c = backup.counts(WallPost)
        self.assertEqual((c["backup"], c["testing"]), (1, 1))

    def test_deleting_on_live_keeps_the_backup(self):
        backup.copy_everything(SRC)
        WallPost.objects.using(SRC).all().delete()
        backup.copy_everything(SRC)
        self.assertTrue(WallPost.objects.filter(body="Real post").exists())

    def test_secrets_and_admin_internals_are_never_copied(self):
        for label in ("accounts.logincode", "sysadmin.adminaccess", "sysadmin.backuprecord"):
            from django.apps import apps
            self.assertEqual(backup.kind_of(apps.get_model(label)), "skip")


@override_settings(STAGING_GATE=False)
class DeleteRuleTests(_TwoDatabases):
    def setUp(self):
        super().setUp()
        backup.copy_everything(SRC)
        self.real_post = WallPost.objects.get(body="Real post")
        self.test_post = WallPost.objects.create(org=self.org, author=self.user, body="Testing post")
        self.boss = User.objects.create_superuser(email="boss-db@example.com", password="pw", display_name="Boss")
        AdminAccess.objects.create(user=self.boss, is_full_access=True)
        self.ian = make_admin("ian-db@example.com", full=True, by=self.boss)
        AdminAccess.objects.filter(user=self.ian).update(can_delete_real=True)
        self.ian = User.objects.get(pk=self.ian.pk)        # admin_access is cached on the instance

    def _table(self, db="staging"):
        return reverse("admin:hq_database_table", args=[db, "orgs.wallpost"])

    def _delete(self, post, db="staging", **data):
        return self.client.post(reverse("admin:hq_database_delete", args=[db, "orgs.wallpost", post.pk]), data)

    def test_the_rule(self):
        self.assertEqual(backup.delete_rule(self.boss, "staging", backup.TESTING), "delete")
        self.assertEqual(backup.delete_rule(self.boss, "staging", backup.BACKUP), "ask")
        self.assertEqual(backup.delete_rule(self.boss, "live", backup.REAL), "ask")
        self.assertEqual(backup.delete_rule(self.ian, "staging", backup.BACKUP), "delete")
        self.assertEqual(backup.delete_rule(self.ian, "live", backup.REAL), "delete")

    def test_any_super_admin_deletes_testing(self):
        sign_in(self.client, self.boss)
        self._delete(self.test_post)
        self.assertFalse(WallPost.objects.filter(pk=self.test_post.pk).exists())

    def test_a_backup_row_needs_ian(self):
        sign_in(self.client, self.boss)
        self._delete(self.real_post, reason="Spam")
        self.assertTrue(WallPost.objects.filter(pk=self.real_post.pk).exists())
        req = DeletionRequest.objects.get()
        self.assertEqual((req.database, req.status, req.reason), ("staging", "pending", "Spam"))

        sign_in(self.client, self.ian)
        self.client.post(reverse("admin:hq_deletion_decide", args=[req.pk]), {"action": "approve"})
        self.assertFalse(WallPost.objects.filter(pk=self.real_post.pk).exists())
        self.assertFalse(BackupRecord.objects.filter(model_label="orgs.wallpost").exists())
        req.refresh_from_db()
        self.assertEqual(req.status, "done")

    def test_only_ian_decides(self):
        req = DeletionRequest.objects.create(database="staging", model_label="orgs.wallpost",
                                             object_pk=str(self.real_post.pk), object_repr="x", requested_by=self.boss)
        sign_in(self.client, self.boss)
        resp = self.client.post(reverse("admin:hq_deletion_decide", args=[req.pk]), {"action": "approve"})
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(WallPost.objects.filter(pk=self.real_post.pk).exists())

    def test_a_live_deletion_is_queued_and_carried_out_by_the_job(self):
        sign_in(self.client, self.ian)
        self._delete(self.real_post, db="live")
        req = DeletionRequest.objects.get()
        self.assertEqual((req.database, req.status, req.object_pk), ("live", "approved", str(self.live_post.pk)))
        self.assertTrue(WallPost.objects.using(SRC).filter(pk=self.live_post.pk).exists())   # nothing yet

        backup.run_live_deletions(SRC)
        req.refresh_from_db()
        self.assertEqual(req.status, "done")
        self.assertFalse(WallPost.objects.using(SRC).filter(pk=self.live_post.pk).exists())
        self.assertTrue(WallPost.objects.filter(pk=self.real_post.pk).exists())             # the backup stays

    def test_without_a_write_connection_live_deletions_wait(self):
        DeletionRequest.objects.create(database="live", model_label="orgs.wallpost", object_pk=str(self.live_post.pk),
                                       object_repr="x", status="approved", requested_by=self.ian)
        backup.run_live_deletions(None)
        self.assertEqual(DeletionRequest.objects.get().status, "approved")

    def test_the_django_admin_refuses_a_backup_row_to_anyone_but_ian(self):
        from django.contrib import admin
        from django.test import RequestFactory

        ma = admin.site._registry[WallPost]
        request = RequestFactory().get("/")
        request.user = self.boss
        self.assertFalse(ma.has_delete_permission(request, self.real_post))
        self.assertTrue(ma.has_delete_permission(request, self.test_post))
        request.user = self.ian
        self.assertTrue(ma.has_delete_permission(request, self.real_post))

    def test_the_screens_open_and_say_which_is_which(self):
        sign_in(self.client, self.boss)
        hub = self.client.get(reverse("admin:hq_tables"))
        self.assertContains(hub, "Two databases")
        for db in ("live", "staging"):
            self.assertEqual(self.client.get(reverse("admin:hq_database", args=[db])).status_code, 200)
        staging = self.client.get(self._table())
        self.assertContains(staging, "Backup")
        self.assertContains(staging, "Testing")
        self.assertContains(staging, "Ask Ian")
        live = self.client.get(self._table("live"))
        self.assertEqual([r["pk"] for r in live.context["rows"]], [self.real_post.pk])
        self.assertEqual({r["label"] for r in live.context["rows"]}, {"real"})
        self.assertEqual(self.client.get(reverse("admin:hq_deletions")).status_code, 200)


@override_settings(STAGING_GATE=False)
class RecordAndHistoryTests(_TwoDatabases):
    """One record's own page, editing it, and the backup's history
    (client, 26 Oct 2026)."""

    def setUp(self):
        super().setUp()
        backup.copy_everything(SRC)
        self.real_post = WallPost.objects.get(body="Real post")
        self.boss = User.objects.create_superuser(email="boss-rec@example.com", password="pw", display_name="Boss")
        AdminAccess.objects.create(user=self.boss, is_full_access=True)
        sign_in(self.client, self.boss)

    def _url(self, db, label, pk):
        return reverse("admin:hq_database_record", args=[db, label, pk])

    def test_a_record_opens_on_its_own_page_with_what_points_at_it(self):
        resp = self.client.get(self._url("staging", "orgs.organisation", self.org.pk))
        self.assertEqual(resp.status_code, 200)
        names = [c["label"] for c in resp.context["connected"]]
        self.assertIn("orgs.wallpost", names)
        self.assertContains(resp, "Backup — a copy of real data")

    def test_the_table_links_rows_to_their_record_page(self):
        resp = self.client.get(reverse("admin:hq_database_table", args=["staging", "orgs.wallpost"]))
        self.assertContains(resp, self._url("staging", "orgs.wallpost", self.real_post.pk))

    def test_where_filters_a_table_to_one_parent(self):
        other = Organisation.objects.create(name="Other Org", season=self.org.season)
        WallPost.objects.create(org=other, author=self.user, body="Elsewhere")
        resp = self.client.get(reverse("admin:hq_database_table", args=["staging", "orgs.wallpost"])
                               + f"?where=org:{self.org.pk}")
        self.assertEqual([r["pk"] for r in resp.context["rows"]], [self.real_post.pk])

    def test_editing_saves_through_the_new_form(self):
        resp = self.client.get(self._url("staging", "orgs.wallpost", self.real_post.pk))
        self.assertIsNotNone(resp.context["form"])
        data = {k: v for k, v in resp.context["form"].initial.items() if v is not None}
        data["body"] = "Corrected"
        for name, field in resp.context["form"].fields.items():
            if name not in data and field.required:
                data[name] = resp.context["form"][name].value()
        data = {k: (v.pk if hasattr(v, "pk") else v) for k, v in data.items()}
        self.client.post(self._url("staging", "orgs.wallpost", self.real_post.pk), data)
        self.real_post.refresh_from_db()
        self.assertEqual(self.real_post.body, "Corrected")

    def test_a_testing_row_is_not_on_the_live_database(self):
        mine = WallPost.objects.create(org=self.org, author=self.user, body="Mine")
        self.assertEqual(self.client.get(self._url("live", "orgs.wallpost", mine.pk)).status_code, 404)
        self.assertEqual(self.client.get(self._url("live", "orgs.wallpost", self.real_post.pk)).status_code, 200)

    def test_the_history_says_what_each_run_brought(self):
        from sysadmin.models import BackupRun

        BackupRun.objects.create(finished_at=timezone.now(), ok=True,
                                 counts={"orgs.wallpost": {"new": 3, "updated": 0}})
        BackupRun.objects.create(finished_at=timezone.now(), ok=True,
                                 counts={"accounts.user": {"new": 0, "updated": 5}})
        resp = self.client.get(reverse("admin:hq_backup_history"))
        self.assertContains(resp, "+3")
        self.assertContains(resp, "Nothing new to back up.")
        hub = self.client.get(reverse("admin:hq_tables"))
        self.assertContains(hub, reverse("admin:hq_backup_history"))
        self.assertContains(hub, "data-gx-countdown")

    def test_copy_everything_reports_new_and_updated(self):
        WallPost.objects.using(SRC).create(org=self.live_org, author=self.live_user, body="Another")
        result = backup.copy_everything(SRC)
        self.assertEqual(result["orgs.wallpost"], {"new": 1, "updated": 0, "live": 2})


class WatchingTests(TestCase):
    """The backup watches live (client, 26 Oct 2026): quiet checks fold into
    one line, and every blog post is real."""

    def test_quiet_checks_fold_into_one_stretch(self):
        from sysadmin.models import BackupRun

        for _ in range(3):
            run = BackupRun.objects.create(ok=True, finished_at=timezone.now(),
                                           counts={"orgs.wallpost": {"new": 0, "updated": 0}})
            backup.fold_quiet_run(run)
        self.assertEqual(BackupRun.objects.count(), 1)
        counts = BackupRun.objects.get().counts
        self.assertEqual((counts["_quiet"], counts["_checks"]), (True, 3))

    def test_a_run_that_brings_something_stands_on_its_own(self):
        from sysadmin.models import BackupRun

        quiet = BackupRun.objects.create(ok=True, finished_at=timezone.now(), counts={})
        backup.fold_quiet_run(quiet)
        busy = BackupRun.objects.create(ok=True, finished_at=timezone.now(),
                                        counts={"orgs.wallpost": {"new": 2, "updated": 0}})
        backup.fold_quiet_run(busy)
        self.assertEqual(BackupRun.objects.count(), 2)

    def test_every_blog_post_is_backup_on_staging(self):
        from admin_panel.models import NewsPost

        post = NewsPost.objects.create(title="A story", slug="a-story-watch")
        self.assertEqual(backup.label_for(NewsPost, post), backup.BACKUP)
        self.assertEqual(backup.counts(NewsPost)["testing"], 0)


class SportsScreenTests(TestCase):
    """Sports & competitions (client, 27 Oct 2026) and the Competition column."""

    def setUp(self):
        from accounts.models import User
        from sysadmin.test_control_plane import sign_in_to_admin

        self.boss = User.objects.create_superuser(email="sp@example.com", password="pw", display_name="Sp")
        sign_in_to_admin(self.client, self.boss)

    def test_the_screen_lists_every_sport_and_switches_one(self):
        from catalog.models import Sport
        from django.urls import reverse

        r = self.client.get(reverse("admin:hq_sports"))
        self.assertEqual(r.status_code, 200)
        for name in ("Football", "Golf", "Basketball", "Australian Rules"):
            self.assertContains(r, name)
        golf = Sport.objects.get(slug="golf")
        self.assertFalse(golf.is_active)
        self.client.post(reverse("admin:hq_sports_toggle"), {"kind": "sport", "pk": golf.pk})
        golf.refresh_from_db()
        self.assertTrue(golf.is_active)

    def test_tables_that_belong_to_a_competition_say_which(self):
        from django.urls import reverse

        r = self.client.get(reverse("admin:hq_database_table", args=["staging", "tipping.round"]))
        self.assertTrue(r.context["has_comp"])
        r = self.client.get(reverse("admin:hq_database_table", args=["staging", "tipping.tip"]))
        self.assertTrue(r.context["has_comp"])
        r = self.client.get(reverse("admin:hq_database_table", args=["staging", "accounts.user"]))
        self.assertFalse(r.context["has_comp"])
