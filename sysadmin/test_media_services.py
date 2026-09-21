"""The two screens added 20 Sep 2026: the media library and Services."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from goodtip.testing import temp_media


class _AdminBase(TestCase):
    def setUp(self):
        from sysadmin.test_control_plane import sign_in_to_admin

        self.boss = get_user_model().objects.create_superuser(
            email="root@goodtip.test", password="x", display_name="Root",
        )
        # /admin/ is behind an emailed one-time code, so force_login alone
        # lands on the verify screen rather than the page under test.
        sign_in_to_admin(self.client, self.boss)


class MediaLibraryTests(_AdminBase):
    """Client, 20 Sep 2026: "all the images and videos, when clicked, it should
    [show] where and where it is being displayed, and have the option of
    changing it." """

    def test_the_gallery_lists_the_pictures_that_ship_with_the_site(self):
        r = self.client.get(reverse("admin:hq_media"))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("stadium-panorama.jpg", body)

    def test_it_can_be_narrowed_to_one_source_and_one_kind(self):
        r = self.client.get(reverse("admin:hq_media"), {"source": "upload"})
        self.assertEqual(r.status_code, 200)
        # Nothing shipped with the product survives an "uploaded only" filter.
        self.assertNotIn("stadium-panorama.jpg", r.content.decode())

    def test_searching_finds_a_picture_by_name(self):
        r = self.client.get(reverse("admin:hq_media"), {"q": "mcg"})
        self.assertIn("mcg-stadium.jpg", r.content.decode())

    def test_opening_one_says_where_it_is_shown(self):
        r = self.client.get(
            reverse("admin:hq_media_detail"),
            {"source": "site", "key": "img/scenes/mcg-stadium.jpg"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("Where it appears", body)
        # It is used by the chat wallpaper list, which is a reference NOBODY
        # would find from the template side — the exact case this screen is for.
        self.assertIn("orgs/services.py", body)

    def test_a_path_that_climbs_out_of_static_is_refused(self):
        """The key comes from a URL. "../../.env" is an ordinary thing for a
        scanner to try, and a viewer that reads any file on disk is a viewer
        that reads the environment file."""
        r = self.client.get(
            reverse("admin:hq_media_detail"),
            {"source": "site", "key": "../../.env"},
        )
        self.assertEqual(r.status_code, 404)

    def test_an_unknown_picture_is_a_404_and_not_a_crash(self):
        r = self.client.get(
            reverse("admin:hq_media_detail"),
            {"source": "site", "key": "img/not-a-real-file.jpg"},
        )
        self.assertEqual(r.status_code, 404)

    def test_a_member_cannot_open_it(self):
        self.client.logout()
        member = get_user_model().objects.create_user(
            email="member@goodtip.test", password="x", display_name="M",
        )
        self.client.force_login(member)
        r = self.client.get(reverse("admin:hq_media"))
        self.assertNotEqual(r.status_code, 200)


# MEDIA_ROOT is redirected although nothing here writes to it: the uploads go
# into a throwaway file under static/, not into media/. The project's guard
# (goodtip.tests.NoTestMayDeleteRealUploadsTests) cannot tell the two apart
# from the outside, and satisfying it costs nothing — whereas exempting this
# class would put a hole in a rule whose whole value is having none.
@override_settings(MEDIA_ROOT=temp_media())
class MediaReplaceTests(_AdminBase):
    """Client, 20 Sep 2026: "yes, add the button with the caveat."

    Every test here works on a throwaway picture written into the static tree
    in setUp and removed afterwards. Nothing touches a file the product
    actually ships — a test that overwrote one would leave the repository
    dirty in exactly the way the caveat warns about.
    """

    def setUp(self):
        super().setUp()
        from sysadmin import media_library as lib

        self.root = lib._static_root()
        self.key = "img/__test_replaceable.png"
        self.path = self.root / self.key
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"original-bytes")
        self.addCleanup(lambda: self.path.exists() and self.path.unlink())
        self.url = reverse("admin:hq_media_replace")
        self.detail = reverse("admin:hq_media_detail")

    def _upload(self, name="new.png", data=b"replacement-bytes"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile(name, data, content_type="image/png")

    def test_the_detail_screen_offers_the_button_and_states_the_caveat(self):
        body = self.client.get(
            self.detail, {"source": "site", "key": self.key},
        ).content.decode()
        self.assertIn("Replace this picture", body)
        # The specific consequence, not a softened version of it.
        self.assertIn("the next deploy will stop on it", body)

    def test_replacing_it_writes_the_file(self):
        r = self.client.post(self.url, {
            "key": self.key, "understood": "1", "replacement": self._upload(),
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.path.read_bytes(), b"replacement-bytes")

    def test_it_will_not_go_through_without_the_tick(self):
        self.client.post(self.url, {"key": self.key, "replacement": self._upload()})
        self.assertEqual(self.path.read_bytes(), b"original-bytes")

    def test_the_file_type_cannot_change(self):
        """Everything that shows this picture names it with its extension, so
        a .jpg over a .png would replace something nothing is looking at."""
        self.client.post(self.url, {
            "key": self.key, "understood": "1",
            "replacement": self._upload(name="new.jpg"),
        })
        self.assertEqual(self.path.read_bytes(), b"original-bytes")

    def test_an_oversized_file_is_refused(self):
        from sysadmin.media_views import MAX_UPLOAD

        self.client.post(self.url, {
            "key": self.key, "understood": "1",
            "replacement": self._upload(data=b"x" * (MAX_UPLOAD + 1)),
        })
        self.assertEqual(self.path.read_bytes(), b"original-bytes")

    def test_a_path_that_climbs_out_of_static_is_refused(self):
        r = self.client.post(self.url, {
            "key": "../../.env", "understood": "1", "replacement": self._upload(),
        })
        self.assertEqual(r.status_code, 404)

    def test_it_is_post_only(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_the_replacement_is_recorded_in_the_admin_log(self):
        from django.contrib.admin.models import LogEntry

        before = LogEntry.objects.count()
        self.client.post(self.url, {
            "key": self.key, "understood": "1", "replacement": self._upload(),
        })
        self.assertEqual(LogEntry.objects.count(), before + 1)
        self.assertIn("Replaced the shipped picture", LogEntry.objects.latest("id").change_message)

    def test_a_member_cannot_replace_anything(self):
        self.client.logout()
        member = get_user_model().objects.create_user(
            email="m3@goodtip.test", password="x", display_name="M",
        )
        self.client.force_login(member)
        self.client.post(self.url, {
            "key": self.key, "understood": "1", "replacement": self._upload(),
        })
        self.assertEqual(self.path.read_bytes(), b"original-bytes")


class ServicesScreenTests(_AdminBase):
    """Client, 20 Sep 2026: "we also need to add Services ... monitor the email
    services and such ... and the AI, like the Prefect; monitor things like the
    MatchReader, Group Recap, see how they perform." """

    def test_every_service_is_on_the_screen(self):
        r = self.client.get(reverse("admin:hq_services"))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        for label in ("Email", "Fixture and results sync", "Prefect",
                      "MatchReader", "Group recaps", "Traffic"):
            self.assertIn(label, body, label)

    def test_the_window_is_shared_by_all_of_them(self):
        """Six panels each with their own period is six numbers that cannot be
        put beside one another."""
        r = self.client.get(reverse("admin:hq_services"), {"window": "24h"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("last 24 hours", r.content.decode().lower())

    def test_it_says_plainly_that_page_views_are_not_measured(self):
        """A monitoring screen earns its figures by being honest about the
        ones it has not got."""
        body = self.client.get(reverse("admin:hq_services")).content.decode()
        self.assertIn("NO PAGE-VIEW TRACKING", body)

    def test_a_sent_email_shows_up_in_the_email_panel(self):
        from sysadmin.models import MailLog

        MailLog.record(
            backend="postmark", subject="Your sign-in code",
            recipients=3, ok=True,
        )
        MailLog.record(
            backend="postmark", subject="Round 9 is graded",
            recipients=40, ok=False, detail="Postmark said no",
        )
        body = self.client.get(reverse("admin:hq_services")).content.decode()
        self.assertIn("43 messages", body)
        self.assertIn("Postmark said no", body)

    def test_a_member_cannot_open_it(self):
        self.client.logout()
        member = get_user_model().objects.create_user(
            email="m2@goodtip.test", password="x", display_name="M",
        )
        self.client.force_login(member)
        self.assertNotEqual(
            self.client.get(reverse("admin:hq_services")).status_code, 200,
        )


class MailLogTests(TestCase):
    def test_recording_never_raises(self):
        """It is called from inside the email backend — that is to say, from
        inside whatever was trying to email somebody."""
        from sysadmin.models import MailLog

        self.assertIsNone(MailLog.record(
            backend="postmark", subject="x", recipients="not a number", ok=True,
        ))

    def test_a_long_provider_error_cannot_fill_the_table(self):
        from sysadmin.models import MailLog

        row = MailLog.record(
            backend="postmark", subject="s", recipients=1, ok=False,
            detail="x" * 5000,
        )
        self.assertEqual(len(row.detail), 300)
