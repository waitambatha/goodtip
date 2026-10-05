"""Season Champion Certificates (spec addendum, Ian Hopkinson, 14 Aug 2026)."""
from datetime import timedelta
from io import BytesIO

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from goodtip.testing import drop_temp_media, temp_media

from .models import Match, Round, Team, Tip

User = get_user_model()


class CertFixture(TestCase):
    """A club with an NRL season that can be finished on demand."""

    def setUp(self):
        from catalog.models import Season, Series
        from orgs.models import OrgMember, Organisation

        self.nrl = Series.objects.get(name="NRL")
        self.season = Season.objects.create(year=2098, label="2098")
        self.org = Organisation.objects.create(name="Cert Club", season=self.season)
        self.ann = User.objects.create_user(email="ann@x.com", password="x", display_name="Ann Lee")
        self.bob = User.objects.create_user(email="bob@x.com", password="x", display_name="Bob Ray")
        self.boss = User.objects.create_user(email="boss@x.com", password="x", display_name="Boss")
        self.cap = User.objects.create_user(email="cap@x.com", password="x", display_name="Cap")
        OrgMember.objects.create(user=self.ann, org=self.org)
        OrgMember.objects.create(user=self.bob, org=self.org)
        OrgMember.objects.create(user=self.boss, org=self.org, role=OrgMember.ROLE_MANAGER)
        OrgMember.objects.create(user=self.cap, org=self.org, role=OrgMember.ROLE_CAPTAIN)
        self.h = Team.objects.create(name="Cert Home", slug="cert-h", series=self.nrl)
        self.a = Team.objects.create(name="Cert Away", slug="cert-a", series=self.nrl)
        self.past = timezone.now() - timedelta(days=10)

    def _match(self, number, stage=Round.STAGE_REGULAR, when=None, result="home"):
        when = when or self.past
        rnd, _ = Round.objects.get_or_create(
            org=self.org, round_number=number, series=self.nrl,
            defaults={"lockout_at": when, "stage": stage},
        )
        return Match.objects.create(round=rnd, home_team=self.h, away_team=self.a,
                                    kickoff_at=when, result=result)

    def _tip(self, user, match, points):
        Tip.objects.create(user=user, match=match, org=self.org, selection="home",
                           is_correct=points > 0, points_awarded=points)

    def _finished_season(self, ann=3, bob=1):
        m1 = self._match(1)
        m2 = self._match(27, Round.STAGE_FINALS, when=self.past + timedelta(days=2))
        self._tip(self.ann, m1, 1); self._tip(self.bob, m1, 1 if bob else 0)
        self._tip(self.ann, m2, ann - 1); self._tip(self.bob, m2, max(bob - 1, 0))


class CertificateTests(CertFixture):

    # ---- §4 the trigger -------------------------------------------------

    def test_no_certificate_before_the_finals(self):
        from .certificates import champions, season_state

        self._tip(self.ann, self._match(1), 1)
        self.assertFalse(season_state(self.org, self.nrl).final)
        self.assertEqual(champions(self.org, self.nrl), [])

    def test_no_certificate_while_a_match_is_ungraded(self):
        from .certificates import season_state

        self._finished_season()
        self._match(28, Round.STAGE_FINALS, result=None)
        state = season_state(self.org, self.nrl)
        self.assertFalse(state.final)
        self.assertIn("1 match still", state.reason)

    def test_not_final_after_an_earlier_finals_week(self):
        """Finals arrive a week at a time: a graded two-match week with nothing
        after it yet is mid-finals, not the end of the season."""
        from .certificates import season_state

        self._tip(self.ann, self._match(1), 1)
        self._match(26, Round.STAGE_FINALS)
        Match.objects.create(round=Round.objects.get(org=self.org, round_number=26, series=self.nrl),
                             home_team=self.a, away_team=self.h, kickoff_at=self.past, result="away")
        state = season_state(self.org, self.nrl)
        self.assertFalse(state.final)
        self.assertIn("grand final", state.reason)

    def test_final_once_the_finals_are_graded(self):
        from .certificates import season_state

        self._finished_season()
        self.assertTrue(season_state(self.org, self.nrl).final)

    # ---- §2 / §3 who and what --------------------------------------------

    def test_the_season_champion_gets_one(self):
        from .certificates import champions

        self._finished_season(ann=3, bob=1)
        certs = champions(self.org, self.nrl)
        self.assertEqual([c.winner for c in certs], ["Ann Lee"])
        self.assertEqual(certs[0].headline, "2098 NRL Season Champion")
        self.assertEqual(certs[0].org_name, "Cert Club")

    def test_co_champions_each_get_their_own_identical_certificate(self):
        from .certificates import champions

        self._finished_season(ann=2, bob=2)
        certs = champions(self.org, self.nrl)
        self.assertEqual(sorted(c.winner for c in certs), ["Ann Lee", "Bob Ray"])
        self.assertEqual({c.headline for c in certs}, {"2098 NRL Season Champion"})

    # ---- §5 exports and who gets them --------------------------------------

    def test_a_participant_cannot_open_the_page(self):
        self.client.force_login(self.ann)
        self.assertEqual(self.client.get(reverse("tipping:certificates", args=[self.org.id])).status_code, 403)

    def test_the_manager_and_the_captain_can(self):
        self._finished_season()
        for u in (self.boss, self.cap):
            self.client.force_login(u)
            resp = self.client.get(reverse("tipping:certificates", args=[self.org.id]))
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, "Ann Lee")

    def test_the_print_pdf_and_the_social_pngs(self):
        from PIL import Image

        self._finished_season()
        self.client.force_login(self.boss)
        base = [self.org.id, "NRL", self.ann.id]
        pdf = self.client.get(reverse("tipping:certificate_file", args=base + ["pdf"]))
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertIn("attachment", pdf["Content-Disposition"])
        sq = self.client.get(reverse("tipping:certificate_file", args=base + ["square"]))
        self.assertEqual(Image.open(BytesIO(sq.content)).size, (1080, 1080))
        story = self.client.get(reverse("tipping:certificate_file", args=base + ["story"]))
        self.assertEqual(Image.open(BytesIO(story.content)).size, (1080, 1920))

    def test_nobody_else_can_be_certified(self):
        self._finished_season(ann=3, bob=1)
        self.client.force_login(self.boss)
        url = reverse("tipping:certificate_file", args=[self.org.id, "NRL", self.bob.id, "pdf"])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_mid_season_there_is_no_file(self):
        self._tip(self.ann, self._match(1), 1)
        self.client.force_login(self.boss)
        url = reverse("tipping:certificate_file", args=[self.org.id, "NRL", self.ann.id, "square"])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_leaderboard_points_the_captain_at_them(self):
        self._finished_season()
        self.client.force_login(self.cap)
        resp = self.client.get(reverse("tipping:leaderboard", args=[self.org.id]))
        self.assertContains(resp, "Season champion certificates are ready")


@override_settings(MEDIA_ROOT=temp_media())
class ChampionPhotoTests(CertFixture):
    """The photo beside a champion's certificate (client mock-up, 3 Oct 2026)."""

    def _png(self, size=(1200, 700)):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image

        buf = BytesIO()
        Image.new("RGB", size, "#336699").save(buf, "PNG")
        return SimpleUploadedFile("me.png", buf.getvalue(), content_type="image/png")

    def setUp(self):
        super().setUp()
        self._finished_season()
        self.client.force_login(self.boss)
        self.url = reverse("tipping:certificate_photo", args=[self.org.id, "NRL", self.ann.id])

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        drop_temp_media()

    def test_without_a_photo_the_circle_shows_initials(self):
        resp = self.client.get(reverse("tipping:certificates", args=[self.org.id]))
        self.assertContains(resp, ">AL<")
        self.assertContains(resp, "Add a photo")

    def test_an_upload_is_squared_and_shown(self):
        from PIL import Image

        from .models import ChampionPhoto

        self.client.post(self.url, {"photo": self._png()})
        photo = ChampionPhoto.objects.get(org=self.org, user=self.ann)
        self.assertEqual(Image.open(photo.image.path).size, (800, 800))
        resp = self.client.get(reverse("tipping:certificates", args=[self.org.id]))
        self.assertContains(resp, photo.image.url)
        self.assertContains(resp, "Use profile photo")

    def test_reset_goes_back_to_the_profile_photo(self):
        from .models import ChampionPhoto

        self.client.post(self.url, {"photo": self._png()})
        self.client.post(self.url, {"reset": "1"})
        self.assertFalse(ChampionPhoto.objects.filter(org=self.org, user=self.ann).exists())

    def test_a_non_image_is_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from .models import ChampionPhoto

        self.client.post(self.url, {"photo": SimpleUploadedFile("x.png", b"not an image", content_type="image/png")})
        self.assertFalse(ChampionPhoto.objects.exists())

    def test_only_a_champion_can_be_given_one(self):
        url = reverse("tipping:certificate_photo", args=[self.org.id, "NRL", self.bob.id])
        self.assertEqual(self.client.post(url, {"photo": self._png()}).status_code, 404)
