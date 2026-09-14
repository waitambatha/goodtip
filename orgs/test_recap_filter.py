"""The Wall as rooms by competition (client, Sep 2026).

First: "a creative way of knowing and distinguishing this is for NRL, this is
for NRLW ... let them have separate colours and tags." Then: "have it like the
WhatsApp and chat thing, so the menu of it should be the competitions — I click
NRL, then I find NRL where I can chat and see the group recap ... we will not
have that top centred menu."
"""
from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import User

from .models import OrgMember, Organisation, RoundRecap, WallPost


class WallRoomTests(TestCase):
    def setUp(self):
        from catalog.models import Season, Series, Sport
        from tipping.models import Round

        sport = Sport.objects.create(name="Filter League", slug="filter-league")
        self.men = Series.objects.create(sport=sport, name="Men's Comp", slug="fl-men")
        self.women = Series.objects.create(sport=sport, name="Women's Comp", slug="fl-women")
        season = Season.objects.create(year=2097, label="2097")
        self.org = Organisation.objects.create(name="Filter Club", season=season)
        self.user = User.objects.create_user(email="f@b.com", password="x", display_name="Fi")
        OrgMember.objects.create(user=self.user, org=self.org)

        def recap(series, body):
            rnd = Round.objects.create(
                org=self.org, round_number=5, series=series,
                lockout_at=timezone.now() - timedelta(days=2),
            )
            post = WallPost.objects.create(org=self.org, kind=WallPost.KIND_RECAP, body=body)
            RoundRecap.objects.create(org=self.org, round=rnd, post=post)

        recap(self.men, "PINE-RECAP-BODY")
        recap(self.women, "OAK-RECAP-BODY")
        WallPost.objects.create(org=self.org, author=self.user, body="MEMBER-POST-BODY")
        self.client.force_login(self.user)
        self.url = reverse("orgs:wall", args=[self.org.id])

    def get(self, query=""):
        return self.client.get(self.url + query).content.decode()

    def stream(self, query=""):
        """Only the open room's own cards. The rooms list beside it shows every
        room's newest line, as Messages does, and a line in that preview is not
        the same thing as being in this room."""
        html = self.get(query)
        start = html.index("data-gwr-stream")
        return html[start:html.index("gwr-composer", start)]

    def test_the_rooms_are_the_competitions_in_their_colours(self):
        html = self.get()
        self.assertIn("All competitions", html)
        self.assertIn('href="?room=fl-men"', html)
        self.assertIn('href="?room=fl-women"', html)
        self.assertIn('data-code="fl-women"', html)
        # The top centred filter bar is gone.
        self.assertNotIn("gwall-recaps", html)

    def test_no_page_loader_between_rooms(self):
        """Client: "when we click it ... remove that loader". Changing room is
        changing conversation, not opening a new screen."""
        html = self.get("?room=fl-women")
        self.assertIn("no-page-loader", html)
        # "It flickers when I press": a room is swapped in by htmx, only the
        # Wall's own frame, with the URL still changing so Back works.
        self.assertIn('hx-get="?room=fl-men" hx-target=".gwr" hx-select=".gwr"', html)
        self.assertIn('hx-push-url="true"', html)
        self.assertIn('data-veil-for=".gwr-stage"', html)
        # The same page answers an htmx request, so hx-select finds the frame.
        r = self.client.get(self.url + "?room=fl-men", HTTP_HX_REQUEST="true")
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="gtm gwr"', r.content.decode())

    def test_a_room_shows_its_own_recaps_only(self):
        html = self.stream("?room=fl-women")
        self.assertIn("OAK-RECAP-BODY", html)
        self.assertNotIn("PINE-RECAP-BODY", html)
        # A post written outside any competition lives in the all room.
        self.assertNotIn("MEMBER-POST-BODY", html)

    def test_the_all_room_shows_everything(self):
        html = self.stream("?room=all")
        for text in ("PINE-RECAP-BODY", "OAK-RECAP-BODY", "MEMBER-POST-BODY"):
            self.assertIn(text, html)

    def test_posting_in_a_room_files_the_post_there(self):
        r = self.client.post(reverse("orgs:wall_post", args=[self.org.id]),
                             {"body": "ROOM-TALK", "room": "fl-men"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].endswith("?room=fl-men"))
        post = WallPost.objects.get(body="ROOM-TALK")
        self.assertEqual(post.series, self.men)
        self.assertIn("ROOM-TALK", self.stream("?room=fl-men"))
        self.assertNotIn("ROOM-TALK", self.stream("?room=fl-women"))
        # ...and the all room still carries it, tagged with its competition.
        self.assertIn("ROOM-TALK", self.stream("?room=all"))

    def test_an_unknown_room_files_the_post_in_the_all_room(self):
        self.client.post(reverse("orgs:wall_post", args=[self.org.id]),
                         {"body": "STRAY-TALK", "room": "not-a-room"})
        self.assertIsNone(WallPost.objects.get(body="STRAY-TALK").series)

    def test_the_saved_room_opens_first(self):
        self.user.recap_codes = ["fl-men"]
        self.user.save(update_fields=["recap_codes"])
        html = self.stream()
        self.assertIn("PINE-RECAP-BODY", html)
        self.assertNotIn("OAK-RECAP-BODY", html)
        self.assertIn("Opens first", self.get())

    def test_saving_a_room_to_open_first(self):
        r = self.client.post(
            reverse("orgs:wall_recap_pref", args=[self.org.id]),
            {"codes": ["fl-men", "not-a-series"]},
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].endswith("?room=fl-men"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.recap_codes, ["fl-men"])

    def test_clearing_it_opens_all_competitions_first(self):
        self.user.recap_codes = ["fl-men"]
        self.user.save(update_fields=["recap_codes"])
        r = self.client.post(reverse("orgs:wall_recap_pref", args=[self.org.id]), {})
        self.assertTrue(r["Location"].endswith("?room=all"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.recap_codes, [])

    def test_only_members_can_save_one_here(self):
        outsider = User.objects.create_user(email="o@b.com", password="x", display_name="Out")
        self.client.force_login(outsider)
        r = self.client.post(reverse("orgs:wall_recap_pref", args=[self.org.id]), {"codes": ["fl-men"]})
        self.assertEqual(r.status_code, 403)


class RecapLabelTests(SimpleTestCase):
    def test_the_sentence_names_the_competition(self):
        from .recaps import _round_label

        rnd = {"is_origin": False, "number": 5, "series": "NRLW", "label": "Round 5"}
        self.assertEqual(_round_label(rnd), "NRLW Round 5")
        self.assertEqual(_round_label(rnd, lower=True), "NRLW round 5")
        finals = {"is_origin": False, "number": 28, "series": "NRL", "label": "Finals Week 1"}
        self.assertEqual(_round_label(finals), "NRL Finals Week 1")

    def test_facts_built_before_this_change_still_read(self):
        from .recaps import _round_label

        self.assertEqual(_round_label({"is_origin": False, "number": 3}), "Round 3")
        self.assertEqual(_round_label({"is_origin": True, "number": 2}), "State of Origin 2")


class RoundLabelTests(TestCase):
    """A final reads as a final, not as "Round 28" (NRL 2026 finals)."""

    def test_labels(self):
        from catalog.models import Season, Series, Sport
        from tipping.models import Round

        sport = Sport.objects.create(name="Label League", slug="label-league")
        series = Series.objects.create(sport=sport, name="Label Comp", slug="label-comp")
        org = Organisation.objects.create(
            name="Label Club", season=Season.objects.create(year=2096, label="2096"),
        )
        when = timezone.now()

        def rnd(n, stage):
            return Round.objects.create(org=org, round_number=n, series=series, stage=stage, lockout_at=when)

        regular = rnd(27, Round.STAGE_REGULAR)
        week1 = rnd(28, Round.STAGE_FINALS)
        week2 = rnd(29, Round.STAGE_FINALS)
        origin = rnd(2, Round.STAGE_ORIGIN)
        self.assertEqual(regular.label, "Round 27")
        self.assertEqual(week1.label, "Finals Week 1")
        self.assertEqual(week2.label, "Finals Week 2")
        self.assertEqual(origin.label, "Origin Game 2")
