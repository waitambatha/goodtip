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
            self.assertContains(resp, "NRL")
            detail = self.client.get(reverse("tipping:certificate_competition", args=[self.org.id, "NRL"]))
            self.assertEqual(detail.status_code, 200)
            self.assertContains(detail, "Ann Lee")

    def test_the_print_pdf_and_the_png(self):
        import re

        from PIL import Image

        self._finished_season()
        self.client.force_login(self.boss)
        base = [self.org.id, "NRL", self.ann.id]
        pdf = self.client.get(reverse("tipping:certificate_file", args=base + ["pdf"]))
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertIn("attachment", pdf["Content-Disposition"])
        # A landscape page cut to the artwork's own shape — no portrait page,
        # no white border round it.
        box = [float(v) for v in re.search(rb"/MediaBox \[\s*([\d.\s]+)\]", pdf.content).group(1).split()]
        self.assertAlmostEqual((box[2] - box[0]) / (box[3] - box[1]), 1672 / 941, places=2)
        png = self.client.get(reverse("tipping:certificate_file", args=base + ["png"]))
        w, h = Image.open(BytesIO(png.content)).size
        # Landscape, the shape of the client's artwork (13 Oct 2026).
        self.assertEqual((w, h), (2508, 1411))
        self.assertEqual(
            self.client.get(reverse("tipping:certificate_file", args=base + ["square"])).status_code, 404,
        )

    def test_the_top_three_get_a_certificate_and_only_they_do(self):
        self._finished_season(ann=3, bob=1)
        self.client.force_login(self.boss)
        url = reverse("tipping:certificate_file", args=[self.org.id, "NRL", self.bob.id, "pdf"])
        self.assertEqual(self.client.get(url).status_code, 200)
        url = reverse("tipping:certificate_file", args=[self.org.id, "NRL", self.boss.id, "pdf"])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_a_placegetter_can_download_their_own(self):
        from .certificates import member_certificates

        self._finished_season(ann=3, bob=1)
        self.assertEqual(member_certificates(self.bob, self.org)[0]["cert"].place, 2)
        self.client.force_login(self.bob)
        url = reverse("tipping:certificate_file", args=[self.org.id, "NRL", self.bob.id, "png"])
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_a_small_group_awards_nothing(self):
        from orgs.models import Group, GroupMember

        from .certificates import member_certificates, placegetters

        self._finished_season(ann=3, bob=1)
        group = Group.objects.create(org=self.org, name="Tiny")
        GroupMember.objects.create(group=group, user=self.ann)
        self.assertEqual(placegetters(self.org, self.nrl, group=group), [])
        self.assertEqual({c["where"] for c in member_certificates(self.ann, self.org)}, {self.org.name})

    def test_finishing_outside_the_top_three_downloads_nothing(self):
        self._finished_season(ann=3, bob=1)
        self.client.force_login(self.boss)
        page = self.client.get(reverse("tipping:my_certificates", args=[self.org.id]))
        self.assertContains(page, "Certificates went to the top three")
        self.assertNotContains(page, "/pdf/")

    def test_mid_season_the_panel_says_what_it_would_take(self):
        from .certificates import certificate_progress

        self._tip(self.ann, self._match(1), 1)
        row = certificate_progress(self.ann, self.org)[0]
        self.assertFalse(row["final"])
        self.assertEqual((row["place"], row["certificates"]), (1, []))

    def test_mid_season_there_is_no_file(self):
        self._tip(self.ann, self._match(1), 1)
        self.client.force_login(self.boss)
        url = reverse("tipping:certificate_file", args=[self.org.id, "NRL", self.ann.id, "png"])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_the_leaderboard_points_the_captain_at_them(self):
        self._finished_season()
        self.client.force_login(self.cap)
        resp = self.client.get(reverse("tipping:my_certificates", args=[self.org.id]))
        self.assertContains(resp, reverse("tipping:certificates", args=[self.org.id]))


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

    def test_without_a_photo_the_page_offers_one(self):
        resp = self.client.get(reverse("tipping:certificate_competition", args=[self.org.id, "NRL"]))
        self.assertContains(resp, "Add a photo")
        # The preview IS the downloadable picture, not a separate drawing of it.
        self.assertContains(resp, reverse("tipping:certificate_file", args=[self.org.id, "NRL", self.ann.id, "png"]))

    def test_an_upload_is_squared_and_shown(self):
        from PIL import Image

        from .models import ChampionPhoto

        self.client.post(self.url, {"photo": self._png()})
        photo = ChampionPhoto.objects.get(org=self.org, user=self.ann)
        self.assertEqual(Image.open(photo.image.path).size, (800, 800))
        resp = self.client.get(reverse("tipping:certificate_competition", args=[self.org.id, "NRL"]))
        self.assertContains(resp, "a photo chosen for this certificate")
        self.assertContains(resp, "Use profile photo")

    def test_the_profile_photo_is_in_the_certificate_itself(self):
        from django.core.files.base import ContentFile
        from PIL import Image

        buf = BytesIO()
        Image.new("RGB", (600, 600), (220, 20, 20)).save(buf, "JPEG")
        self.ann.avatar.save("ann.jpg", ContentFile(buf.getvalue()), save=True)
        png = self.client.get(reverse("tipping:certificate_file", args=[self.org.id, "NRL", self.ann.id, "png"]))
        r, g, b = Image.open(BytesIO(png.content)).convert("RGB").getpixel((560, 660))
        self.assertGreater(r, 180)
        self.assertLess(g, 60)

    def test_padding_bars_are_trimmed_off_a_photo(self):
        from PIL import Image

        from .certificates import _trim_bars

        photo = Image.new("RGB", (400, 800), (0, 0, 0))
        photo.paste((200, 120, 60), (0, 300, 400, 800))
        self.assertEqual(_trim_bars(photo).size, (400, 500))

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


class SampleCertificateTests(CertFixture):
    """The showcase preview (client, 7 Oct 2026): a SAMPLE certificate while a
    season is still going, in the real design, with the real downloads."""

    def setUp(self):
        super().setUp()
        self._tip(self.ann, self._match(1), 3)          # mid-season: Ann leads
        self.client.force_login(self.boss)

    def test_the_preview_shows_a_sample_naming_the_viewer(self):
        from .certificates import sample_certificate

        page = self.client.get(
            reverse("tipping:certificate_competition", args=[self.org.id, "NRL"]), {"preview": "1"},
        )
        self.assertContains(page, "Hide the sample")
        self.assertContains(page, "with your name and profile photo")
        # Ann leads the board, but the preview is the viewer's own.
        self.assertEqual(sample_certificate(self.org, self.nrl, self.boss).winner, "Boss")
        self.assertContains(page, reverse("tipping:certificate_sample_file", args=[self.org.id, "NRL", "pdf"]))

    def test_without_the_switch_there_is_no_sample(self):
        page = self.client.get(reverse("tipping:certificate_competition", args=[self.org.id, "NRL"]))
        self.assertNotContains(page, "Hide the sample")
        self.assertContains(page, "Preview a sample certificate")

    def test_the_sample_downloads_and_says_sample(self):
        from PIL import Image

        from .certificates import sample_certificate

        pdf = self.client.get(reverse("tipping:certificate_sample_file", args=[self.org.id, "NRL", "pdf"]))
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertIn("-sample.pdf", pdf["Content-Disposition"])
        png = self.client.get(reverse("tipping:certificate_sample_file", args=[self.org.id, "NRL", "png"]))
        self.assertEqual(Image.open(BytesIO(png.content)).size, (2508, 1411))
        cert = sample_certificate(self.org, self.nrl, self.boss)
        self.assertTrue(cert.sample)
        self.assertEqual(cert.footer_line, "SAMPLE  ·  SEASON IN PROGRESS")

    def test_a_participant_cannot_get_one(self):
        self.client.force_login(self.ann)
        url = reverse("tipping:certificate_sample_file", args=[self.org.id, "NRL", "pdf"])
        self.assertEqual(self.client.get(url).status_code, 403)


class CrossOrgCertificateTests(TestCase):
    """Beyond one organisation (client, 12 Oct 2026): an organisation against
    its own type, against every organisation, and a person against every
    consenting organisation's tippers. Same consent gate as the Good List and
    the national/industry ladders — ``is_public_listed`` — and nothing is
    named until every consenting organisation that runs the competition has
    reached ITS OWN final, the same per-org trigger the single-org
    certificate already uses."""

    def setUp(self):
        from catalog.models import Competition, GoodListConfig, OrganisationType, Season, Series

        self.season = Season.objects.create(year=2097, label="2097")
        self.nrl = Series.objects.get(slug="nrl")
        self.comp = Competition.objects.create(
            sport=self.nrl.sport, season=self.season, name="Cross Comp", slug="cross-comp",
        )
        self.comp.series.add(self.nrl)
        self.business = OrganisationType.objects.get(slug="business")
        self.community = OrganisationType.objects.get(slug="community")
        cfg = GoodListConfig.get()
        cfg.credibility_min_groups = 2
        cfg.save()
        self.home = Team.objects.create(name="XH", slug="cross-h", series=self.nrl)
        self.away = Team.objects.create(name="XA", slug="cross-a", series=self.nrl)
        self.past = timezone.now() - timedelta(days=5)
        self._seq = 0

    def _user(self):
        self._seq += 1
        return User.objects.create_user(
            email=f"co{self._seq}@example.com", password="x", display_name=f"Co {self._seq}",
        )

    def make_org(self, name, *, public=True, organisation_type=None):
        from orgs.models import Organisation

        return Organisation.objects.create(
            name=name, season=self.season, is_public_listed=public,
            organisation_type=organisation_type or self.business,
        )

    def finish_season(self, org):
        """A graded regular round (where points are scored) and a one-match
        graded finals round — the same shape ``CertFixture`` uses — so
        ``season_state(org, nrl).final`` is True."""
        reg = Round.objects.create(
            org=org, round_number=1, series=self.nrl, competition=self.comp,
            lockout_at=self.past, stage=Round.STAGE_REGULAR,
        )
        match = Match.objects.create(
            round=reg, home_team=self.home, away_team=self.away,
            kickoff_at=self.past, status="complete", result="home",
        )
        finals = Round.objects.create(
            org=org, round_number=2, series=self.nrl, competition=self.comp,
            lockout_at=self.past + timedelta(days=1), stage=Round.STAGE_FINALS,
        )
        Match.objects.create(
            round=finals, home_team=self.home, away_team=self.away,
            kickoff_at=self.past + timedelta(days=1), status="complete", result="home",
        )
        return match

    def score(self, org, match, points):
        from orgs.models import OrgMember

        user = self._user()
        OrgMember.objects.create(user=user, org=org)
        Tip.objects.create(
            user=user, match=match, org=org, selection="home",
            is_correct=True, points_awarded=points,
        )
        return user

    def test_the_top_individual_is_attributed_to_their_own_organisation(self):
        from tipping.certificates import national_individual_champion

        a = self.make_org("A Co")
        b = self.make_org("B Co")
        ma, mb = self.finish_season(a), self.finish_season(b)
        star = self.score(a, ma, 50)
        self.score(b, mb, 1)
        cert_a = national_individual_champion(a, self.nrl)
        self.assertIsNotNone(cert_a)
        self.assertEqual(cert_a.winner, star.display_name)
        self.assertEqual(cert_a.scope, "individual_national")
        # B never tipped with this person — B does not get to show their cert.
        self.assertIsNone(national_individual_champion(b, self.nrl))
