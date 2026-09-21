from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from catalog.models import Charity, Season, Sport
from orgs.models import OrgMember, Organisation

from . import donations
from .models import CharityDisbursement, PlanSubscription
from .pricing import PRO, STARTER, tier_config

User = get_user_model()


class FamilyTotalsTests(TestCase):
    """Org-structure note §7: local and national totals, always separate,
    with child org totals rolling up into the parent automatically."""

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test Season"})
        self.lifeline, _ = Charity.objects.get_or_create(
            slug="lifeline", defaults={"name": "Lifeline", "is_approved": True}
        )
        self.beyondblue, _ = Charity.objects.get_or_create(
            slug="beyond-blue", defaults={"name": "Beyond Blue", "is_approved": True}
        )
        self.parent = Organisation.objects.create(
            name="National Tiles", season=self.season, charity=self.lifeline
        )
        self.mitcham = Organisation.objects.create(
            name="National Tiles Mitcham", season=self.season,
            charity=self.beyondblue, parent=self.parent,
        )
        self.preston = Organisation.objects.create(
            name="National Tiles Preston", season=self.season,
            charity=self.lifeline, parent=self.parent,
        )

    def test_standalone_org_has_no_national_figure(self):
        loner = Organisation.objects.create(
            name="Loner", season=self.season, charity=self.lifeline
        )
        donations.set_pledge(loner, pledged_amount=Decimal("100"))
        self.assertIsNone(donations.family_totals(loner))

    def test_rollup_spans_parent_and_all_children_across_charities(self):
        donations.set_pledge(self.parent, pledged_amount=Decimal("1000"))
        donations.set_pledge(self.mitcham, pledged_amount=Decimal("200"))
        donations.set_pledge(self.preston, pledged_amount=Decimal("300"))
        totals = donations.family_totals(self.mitcham)
        # Local stays the child's own figure; national is the whole family —
        # a dollar total regardless of each org's charity choice (§5).
        self.assertEqual(totals["local"], Decimal("200.00"))
        self.assertEqual(totals["national"], Decimal("1500.00"))
        self.assertEqual(totals["root"], self.parent)
        self.assertEqual(totals["org_count"], 3)

    def test_parent_sees_same_national_with_its_own_local(self):
        donations.set_pledge(self.parent, pledged_amount=Decimal("1000"))
        donations.set_pledge(self.mitcham, pledged_amount=Decimal("200"))
        totals = donations.family_totals(self.parent)
        self.assertEqual(totals["local"], Decimal("1000.00"))
        self.assertEqual(totals["national"], Decimal("1200.00"))

    def test_orgs_without_pledges_count_as_zero(self):
        donations.set_pledge(self.mitcham, pledged_amount=Decimal("200"))
        totals = donations.family_totals(self.preston)
        self.assertEqual(totals["local"], Decimal("0.00"))
        self.assertEqual(totals["national"], Decimal("200.00"))

    def test_a_child_member_gets_both_figures_kept_apart(self):
        """§7: local and national side by side, never combined.

        This used to assert the two figures on the dashboard. That markup was
        deliberately removed — both read $0 for every group until money
        actually moves, and a pair of empty dollar figures was taking the space
        the fixtures wanted (see the comment in templates/dashboard.html).

        The calculation was explicitly kept so the tiles can be re-added as
        markup rather than rebuilt, so that is what this now guards. Asserting
        the deleted markup made the test fail for the one reason that is not a
        bug: the feature working as intended.
        """
        donations.set_pledge(self.parent, pledged_amount=Decimal("1000"))
        donations.set_pledge(self.mitcham, pledged_amount=Decimal("200"))
        member = User.objects.create_user(
            email="m@example.com", password="x", display_name="M",
        )
        OrgMember.objects.create(user=member, org=self.mitcham)
        self.client.force_login(member)
        # The page a child member lands on still renders.
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

        totals = donations.family_totals(self.mitcham)
        self.assertIsNotNone(totals)
        # Parent + Mitcham + Preston, all three set up above.
        self.assertEqual(totals["org_count"], 3)
        # Kept apart: the child's own figure is not folded into the roll-up.
        self.assertEqual(totals["local"], Decimal("200.00"))
        self.assertEqual(totals["national"], Decimal("1200.00"))


def _activate_plan(org, tier):
    cfg = tier_config(tier)
    return PlanSubscription.objects.create(
        org=org, season=org.season, tier=tier,
        price_aud=cfg["price"], seat_limit=cfg["seat_limit"],
        status=PlanSubscription.STATUS_ACTIVE,
    )


class DonationEngineTests(TestCase):
    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test Season"})
        self.sport, _ = Sport.objects.get_or_create(name="AFL", defaults={"slug": "afl"})
        self.charity, _ = Charity.objects.get_or_create(
            slug="test-charity", defaults={"name": "Test Charity", "is_approved": True}
        )
        self.org = Organisation.objects.create(name="Acme", season=self.season, charity=self.charity)
        self.user = User.objects.create_user(email="p@example.com", password="x", display_name="P")

    def test_pledge_anchors_the_total(self):
        donations.set_pledge(self.org, pledged_amount=Decimal("500"))
        s = donations.donation_summary(self.org)
        self.assertEqual(s["base"], Decimal("500.00"))
        self.assertEqual(s["raised"], Decimal("500.00"))
        self.assertEqual(s["goal"], Decimal("500.00"))
        self.assertEqual(s["pct"], 100)

    def test_topup_without_matching(self):
        pledge = donations.set_pledge(self.org, pledged_amount=Decimal("200"))
        result = donations.record_topup(pledge, participant=self.user, amount=Decimal("20"))
        self.assertEqual(result["matched"], Decimal("0"))
        s = donations.donation_summary(self.org)
        self.assertEqual(s["topups"], Decimal("20.00"))
        self.assertEqual(s["matched"], Decimal("0.00"))
        self.assertEqual(s["raised"], Decimal("220.00"))

    def test_matching_doubles_topup_until_cap(self):
        pledge = donations.set_pledge(
            self.org, pledged_amount=Decimal("200"),
            matching_enabled=True, matching_cap=Decimal("50"),
        )
        # $20 top-up → $20 matched (cap not yet reached)
        r1 = donations.record_topup(pledge, participant=self.user, amount=Decimal("20"))
        self.assertEqual(r1["matched"], Decimal("20.00"))
        self.assertFalse(r1["cap_reached"])
        # $40 more → only $30 left under the $50 cap gets matched
        r2 = donations.record_topup(pledge, participant=self.user, amount=Decimal("40"))
        self.assertEqual(r2["matched"], Decimal("30.00"))
        self.assertTrue(r2["cap_reached"])
        # Further top-ups still go to charity but are no longer doubled
        r3 = donations.record_topup(pledge, participant=self.user, amount=Decimal("10"))
        self.assertEqual(r3["matched"], Decimal("0.00"))

        s = donations.donation_summary(self.org)
        self.assertEqual(s["topups"], Decimal("70.00"))   # 20 + 40 + 10
        self.assertEqual(s["matched"], Decimal("50.00"))  # capped
        self.assertEqual(s["matching_remaining"], Decimal("0"))
        self.assertEqual(s["raised"], Decimal("320.00"))  # 200 base + 70 + 50
        self.assertEqual(s["goal"], Decimal("250.00"))    # 200 base + 50 cap
        self.assertEqual(s["pct"], 100)

    def test_disbursement_aggregates_the_season(self):
        pledge = donations.set_pledge(
            self.org, pledged_amount=Decimal("200"),
            matching_enabled=True, matching_cap=Decimal("200"),
        )
        donations.record_topup(pledge, participant=self.user, amount=Decimal("15"))
        d = donations.create_disbursement(self.org)
        self.assertEqual(d.total_base_aud, Decimal("200.00"))
        self.assertEqual(d.total_topups_aud, Decimal("15.00"))
        self.assertEqual(d.total_matched_aud, Decimal("15.00"))
        self.assertEqual(d.total_disbursed_aud, Decimal("230.00"))
        self.assertEqual(d.charity, self.charity)

    def test_negative_topup_rejected(self):
        pledge = donations.set_pledge(self.org, pledged_amount=Decimal("100"))
        with self.assertRaises(ValueError):
            donations.record_topup(pledge, participant=self.user, amount=Decimal("0"))


class DonationViewTests(TestCase):
    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test Season"})
        self.sport, _ = Sport.objects.get_or_create(name="AFL", defaults={"slug": "afl"})
        self.charity, _ = Charity.objects.get_or_create(
            slug="test-charity", defaults={"name": "Test Charity", "is_approved": True}
        )
        self.org = Organisation.objects.create(name="Acme", season=self.season, charity=self.charity)
        self.owner = User.objects.create_user(email="o@example.com", password="x", display_name="Owner")
        OrgMember.objects.create(user=self.owner, org=self.org, role=OrgMember.ROLE_BOTH, is_league_owner=True)
        self.player = User.objects.create_user(email="p@example.com", password="x", display_name="Player")
        OrgMember.objects.create(user=self.player, org=self.org, role=OrgMember.ROLE_PARTICIPANT)

    # test_owner_sets_pledge_via_view, test_participant_cannot_set_pledge and
    # test_participant_topup_recorded_without_gateway were removed on
    # 18 Aug 2026 with the screens they exercised. GoodTip funds the donation
    # from its own revenue, so an organisation has no pledge to set and a
    # participant has nothing to top up — there is no view left to test.
    #
    # donations.set_pledge and the summary maths are NOT removed and stay
    # covered below: the dashboard and the ESG report still read them to report
    # what has been given, they are simply no longer written from a form.

    def test_manager_closes_season(self):
        donations.set_pledge(self.org, pledged_amount=Decimal("300"))
        self.client.force_login(self.owner)
        url = reverse("billing:season_summary", args=[self.org.id])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url)
        self.assertTrue(CharityDisbursement.objects.filter(org=self.org, season=self.season).exists())

    def test_esg_export_blocked_below_pro(self):
        _activate_plan(self.org, STARTER)
        donations.set_pledge(self.org, pledged_amount=Decimal("100"))
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("billing:esg_report", args=[self.org.id]))
        self.assertEqual(resp.status_code, 302)

    def test_esg_pdf_for_pro_owner(self):
        _activate_plan(self.org, PRO)
        donations.set_pledge(self.org, pledged_amount=Decimal("500"))
        donations.record_topup(donations.get_pledge(self.org), participant=self.player, amount=Decimal("20"))
        self.client.force_login(self.owner)
        resp = self.client.get(reverse("billing:esg_report", args=[self.org.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertTrue(resp.content.startswith(b"%PDF"))


class FoundingRateTests(TestCase):
    """The Founding Member price lock, which until Sep 2026 was only a sentence.

    Client task list: "Founding Member pricing (locked through end of 2027 for
    anyone who signs up before 31 Dec) is currently just a label on the site,
    no backend mechanism enforces it."

    There is one now. These are the rules that make it either a promise or a
    trap, depending on which way they are written.
    """

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(
            year=2099, defaults={"label": "Test Season"},
        )
        self.org = Organisation.objects.create(name="Early Co", season=self.season)

    def _grant(self, tier=STARTER):
        from . import services

        return services.grant_founding_rate(self.org, tier)

    def test_choosing_a_plan_in_the_window_locks_the_rate(self):
        from . import services
        from .pricing import founding_locked_until

        sub = services.create_subscription(self.org, STARTER)

        rate = services.founding_rate_for(self.org)
        self.assertIsNotNone(rate)
        self.assertEqual(rate.price_aud, Decimal(tier_config(STARTER)["price"]))
        self.assertEqual(rate.locked_until, founding_locked_until())
        self.assertTrue(sub.is_founding_rate is False or sub.price_aud == rate.price_aud)

    def test_the_lock_holds_the_old_price_when_the_list_price_rises(self):
        from unittest.mock import patch

        from . import services

        self._grant(STARTER)
        risen = dict(tier_config(STARTER), price=149)
        with patch.dict("billing.pricing.TIERS", {STARTER: risen}):
            price, founding, list_price = services.price_for(self.org, STARTER)

        self.assertEqual(price, Decimal("99"))
        self.assertTrue(founding)
        self.assertEqual(list_price, Decimal("149"))

    def test_the_lock_is_a_ceiling_and_never_a_floor(self):
        """A loyalty rate that costs more than walking in off the street isn't one."""
        from unittest.mock import patch

        from . import services

        self._grant(STARTER)
        cheaper = dict(tier_config(STARTER), price=49)
        with patch.dict("billing.pricing.TIERS", {STARTER: cheaper}):
            price, founding, _list = services.price_for(self.org, STARTER)

        self.assertEqual(price, Decimal("49"))
        self.assertFalse(founding)

    def test_moving_up_a_plan_pays_that_plan_s_price(self):
        """The promise was that your price would not rise under you.

        It was never that every larger plan would be sold at the small one's
        rate, and reading it that way would give a $99 founding member a $1,299
        product for $99.
        """
        from . import services

        self._grant(STARTER)
        price, founding, _list = services.price_for(self.org, PRO)

        self.assertEqual(price, Decimal(tier_config(PRO)["price"]))
        self.assertFalse(founding)

    def test_changing_plan_before_paying_does_not_move_the_lock(self):
        from . import services

        services.create_subscription(self.org, STARTER)
        services.create_subscription(self.org, PRO)

        rate = services.founding_rate_for(self.org)
        self.assertEqual(rate.price_aud, Decimal(tier_config(STARTER)["price"]))
        self.assertEqual(rate.tier, STARTER)

    def test_an_expired_lock_stops_applying(self):
        from datetime import date, timedelta

        from . import services

        rate = self._grant(STARTER)
        rate.locked_until = date.today() - timedelta(days=1)
        rate.save(update_fields=["locked_until"])

        self.assertIsNone(services.founding_rate_for(self.org))

    def test_nothing_is_granted_once_the_window_has_shut(self):
        from unittest.mock import patch

        from . import services

        with patch("billing.services.founding_window_open", return_value=False):
            self.assertIsNone(services.grant_founding_rate(self.org, STARTER))

    def test_the_subscription_records_the_list_price_it_beat(self):
        """So a receipt can still explain itself years later."""
        from unittest.mock import patch

        from . import services

        self._grant(STARTER)
        risen = dict(tier_config(STARTER), price=149)
        with patch.dict("billing.pricing.TIERS", {STARTER: risen}):
            sub = services.create_subscription(self.org, STARTER)

        self.assertTrue(sub.is_founding_rate)
        self.assertEqual(sub.price_aud, Decimal("99"))
        self.assertEqual(sub.list_price_aud, Decimal("149"))
        self.assertEqual(sub.founding_saving_aud, Decimal("50"))


class SponsorshipVisibilityTests(TestCase):
    """Client, 20 Sep 2026: "we do have the sponsorship page, so in the pricing
    I think we should have that so someone can see the sponsorship and all."

    It was a line of footnote under the price table, in the same grey as the
    founding-rate note beside it. A person who has just read a number and
    decided it is not for them is not reading footnotes.
    """

    def test_the_pricing_page_gives_it_a_band_of_its_own(self):
        body = self.client.get("/pricing/").content
        self.assertIn(b'id="sponsored"', body)
        self.assertIn(b"Apply for a sponsored place", body)
        # And the three objections it answers, on the page rather than one
        # click away — those are what decide whether somebody asks at all.
        self.assertIn(b"Nothing to prove", body)
        self.assertIn(b"No account needed", body)

    def test_the_footer_carries_it_too(self):
        """Somebody who read a price, closed the tab and came back a week later
        does not retrace their steps through the table to find it."""
        body = self.client.get("/pricing/").content
        self.assertIn(b"Sponsored places", body)

    def test_the_legal_links_are_named_in_full_and_are_controls(self):
        """Client, 20 Sep 2026: "in the footer where we have the terms, have it
        as Terms and Conditions and the Privacy — those two look small and does
        not clickable." """
        body = self.client.get("/pricing/").content.decode()
        self.assertIn("Terms &amp; Conditions", body)
        self.assertIn("Privacy Policy", body)
        nav = body[body.index('class="footer-legal-nav"'):]
        nav = nav[:nav.index("</nav>")]
        self.assertIn('href="/terms/"', nav)
        self.assertIn('href="/privacy/"', nav)
        # An icon apiece: the thing that survives the row wrapping on a phone,
        # and the tell that these are buttons rather than a heading.
        self.assertEqual(nav.count("<svg"), 2)


class LegalPageCleanupTests(TestCase):
    """Client, 20 Sep 2026: "on the Terms and Conditions page ... remove that
    translucent feel, and let's have white and green." """

    def test_no_photograph_sits_behind_the_words(self):
        """The distinction the client drew across two messages, and it is a
        real one: a picture BEHIND text is what read as translucent, a picture
        BESIDE it is the hero split they then asked for.

        So this pins the two arrangements that were the problem — a shot under
        a veil with the heading on top of it, and four full-height rails down
        the margins of the document — without forbidding the photograph in the
        split's own panel.
        """
        for url in ("/terms/", "/privacy/"):
            body = self.client.get(url).content
            self.assertNotIn(b'class="hero-lean"', body, url)
            self.assertNotIn(b'class="hl-bg"', body, url)
            self.assertNotIn(b'class="pg-rails"', body, url)

    def test_the_hero_is_a_split_with_the_image_on_one_side(self):
        """Client, 20 Sep 2026: "on the Terms and Privacy we should have the
        hero split at the top, and the images on one side." """
        for url in ("/terms/", "/privacy/"):
            body = self.client.get(url).content.decode()
            self.assertIn('class="hero-split hero-legal"', body, url)
            hero = body[body.index('hero-split hero-legal'):]
            hero = hero[:hero.index("</section>")]
            # The words on the left, the photograph in its own panel on the
            # right — never one over the other.
            self.assertLess(hero.index('hs-left'), hero.index('hs-right'), url)
            self.assertIn("hero-bg", hero)

    def test_the_reading_band_script_is_loaded(self):
        """The colour change as you scroll — the clause you are reading takes
        the green and the contents rail lights to match."""
        for url in ("/terms/", "/privacy/"):
            self.assertIn(b"gt-legal.js", self.client.get(url).content, url)


class PricingHeroSponsorshipTests(TestCase):
    """Client, 20 Sep 2026: "make sure also the button to go to the sponsorship
    page is also there and visible, and maybe vibrates so it can draw
    attention." """

    def test_the_hero_carries_the_sponsorship_route(self):
        body = self.client.get("/pricing/").content.decode()
        hero = body[body.index('class="hero-split"'):]
        hero = hero[:hero.index("</section>")]
        self.assertIn("/sponsorship/", hero)
        self.assertIn("btn-nudge", hero)

    def test_it_is_the_ghost_button_not_the_primary_one(self):
        """"See the plans" is still what most people came for. A page that
        pushes the concession harder than the product reads as apologising for
        the price."""
        body = self.client.get("/pricing/").content.decode()
        hero = body[body.index('class="hero-split"'):]
        hero = hero[:hero.index("</section>")]
        plans = hero.index("See the plans")
        ask = hero.index("btn-nudge")
        self.assertLess(plans, ask)
        self.assertIn("btn-ghost btn-nudge", hero)


class PricingPageGroupsTests(TestCase):
    """Client, 19 Sep 2026: "The $799 package which now has groups should be
    marked 'most popular'. Also a little tag of 'groups' would be handy as
    people won't realise it's a big step up, this should also apply to the
    package above that. Unless there is another colour coding that can help
    distinguish it from the non group packs." """

    def test_the_799_plan_is_the_one_marked_most_popular(self):
        body = self.client.get("/pricing/").content
        self.assertIn(b'<tr class="is-popular has-groups">', body)
        # And the module the app bills from agrees with the page.
        from .pricing import PRO, TIERS

        self.assertTrue(TIERS[PRO]["popular"])
        self.assertFalse(any(
            cfg["popular"] for key, cfg in TIERS.items() if key != PRO
        ))

    def test_every_plan_with_groups_wears_the_groups_tag(self):
        from .pricing import TIERS

        body = self.client.get("/pricing/").content
        with_groups = sum(1 for cfg in TIERS.values() if cfg["groups"])
        self.assertEqual(body.count(b"tier-badge--groups"), with_groups)

    def test_the_group_plans_are_marked_as_a_block_not_just_tagged(self):
        """The colour coding the client asked for as the alternative: the rows
        that carry groups are marked as rows, so the table reads as two blocks
        before any of the words have been."""
        body = self.client.get("/pricing/").content
        from .pricing import TIERS

        with_groups = sum(1 for cfg in TIERS.values() if cfg["groups"])
        self.assertEqual(body.count(b"has-groups"), with_groups)


class PlanRecommendationTests(TestCase):
    """Client, 17 Sep 2026: "where in the process do they select their plan or
    get a recommended package — is it when they get asked the size of the
    group?" It is, and this is the sum that screen prints."""

    def test_the_size_picks_the_smallest_plan_that_fits(self):
        from .pricing import recommendation

        self.assertEqual(recommendation(12)["label"], "Starter")
        self.assertEqual(recommendation(20)["label"], "Starter")
        self.assertEqual(recommendation(21)["label"], "Team")
        self.assertEqual(recommendation(150)["label"], "Workplace")
        self.assertEqual(recommendation(151)["label"], "Organisation")

    def test_no_size_given_recommends_the_smallest(self):
        from .pricing import recommendation

        self.assertEqual(recommendation(None)["label"], "Starter")
        self.assertEqual(recommendation(0)["label"], "Starter")

    def test_asking_for_groups_lifts_a_small_org_to_the_plan_that_has_them(self):
        """The case the client raised: a forty-person workplace that wants
        groups is not on the forty-person plan."""
        from .pricing import recommendation

        pick = recommendation(40, wants_groups=True)
        self.assertEqual(pick["label"], "Workplace")
        self.assertTrue(pick["has_groups"])
        # And the screen is told WHICH of the two decided it, so it can say so.
        self.assertTrue(pick["raised_by_groups"])

    def test_groups_never_drag_a_large_org_downwards(self):
        from .pricing import recommendation

        pick = recommendation(400, wants_groups=True)
        self.assertEqual(pick["label"], "Organisation")
        self.assertFalse(pick["raised_by_groups"])

    def test_not_asking_for_groups_leaves_the_size_answer_alone(self):
        from .pricing import recommendation

        pick = recommendation(40, wants_groups=False)
        self.assertEqual(pick["label"], "Team")
        self.assertFalse(pick["has_groups"])
        self.assertFalse(pick["raised_by_groups"])

    def test_the_largest_plan_says_it_is_the_largest(self):
        from .pricing import recommendation

        self.assertTrue(recommendation(9000)["is_top"])
        self.assertFalse(recommendation(60)["is_top"])

    def test_the_quoted_price_is_the_published_one(self):
        from .pricing import TIERS, recommendation

        for size in (5, 30, 100, 300, 3000):
            pick = recommendation(size)
            self.assertEqual(pick["price"], TIERS[pick["tier"]]["price"])


class TierGroupsTests(TestCase):
    """Which plans carry groups, and what the pricing table says they cost.

    The published plans and this module had drifted — billing offered
    "Growth, $199" while the site sold "Team, $299" — so these pin the numbers
    a customer actually reads.
    """

    def test_the_labels_are_the_published_product_names(self):
        from .pricing import ENTERPRISE, GROWTH, PRO, tier_label

        self.assertEqual(tier_label(GROWTH), "Team")
        self.assertEqual(tier_label(PRO), "Workplace")
        self.assertEqual(tier_label(ENTERPRISE), "Organisation")

    def test_groups_start_at_workplace(self):
        from .pricing import ENTERPRISE, GROWTH, PRO, STARTER as S, tier_has_groups

        self.assertFalse(tier_has_groups(S))
        self.assertFalse(tier_has_groups(GROWTH))
        self.assertTrue(tier_has_groups(PRO))
        self.assertTrue(tier_has_groups(ENTERPRISE))

    def test_an_org_with_no_plan_at_all_has_no_groups(self):
        from .pricing import tier_has_groups

        self.assertFalse(tier_has_groups(None))

    def test_team_size_picks_the_smallest_plan_that_fits(self):
        from .pricing import ENTERPRISE, GROWTH, PRO, STARTER as S, tier_for_team_size

        self.assertEqual(tier_for_team_size(None), S)
        self.assertEqual(tier_for_team_size(20), S)
        self.assertEqual(tier_for_team_size(21), GROWTH)
        self.assertEqual(tier_for_team_size(50), GROWTH)
        self.assertEqual(tier_for_team_size(51), PRO)
        self.assertEqual(tier_for_team_size(150), PRO)
        self.assertEqual(tier_for_team_size(500), ENTERPRISE)

    def test_the_upgrade_to_sell_is_the_cheapest_one_with_groups(self):
        from .pricing import GROWTH, PRO, STARTER as S, next_tier_with_groups

        self.assertEqual(next_tier_with_groups(S), PRO)
        self.assertEqual(next_tier_with_groups(GROWTH), PRO)
