from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

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


class StripeWebhookTests(TestCase):
    """checkout.session.completed, signed exactly as Stripe signs it.

    FOUND 28 SEP 2026, LIVE: stripe==15.2.0's StripeObject stopped being a
    dict subclass — event["data"]["object"] still supports [] (__getitem__),
    but not .get(), which is what stripe_webhook and _record_donation_from_
    session are written in. Every real webhook delivery 500'd, silently,
    because Stripe keys had never been configured in any environment before
    that day, so nothing had ever actually called this view with a real,
    Stripe-shaped (not hand-built dict) event. Posting a real signed payload,
    the way `stripe listen`/Stripe's own servers do, is the only way this
    reproduces — a hand-built dict payload passed straight to the view
    function would not catch it, since a plain dict's .get() works fine."""

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test Season"})
        self.charity, _ = Charity.objects.get_or_create(
            slug="webhook-charity", defaults={"name": "Webhook Charity", "is_approved": True}
        )
        self.org = Organisation.objects.create(name="Webhook Org", season=self.season, charity=self.charity)
        self.sub = PlanSubscription.objects.create(
            org=self.org, season=self.season, tier=STARTER,
            price_aud=tier_config(STARTER)["price"], seat_limit=tier_config(STARTER)["seat_limit"],
            status=PlanSubscription.STATUS_PENDING,
        )

    def _post_signed(self, session_extra, event_type="checkout.session.completed", obj="checkout.session"):
        import json
        import time

        import stripe
        from django.test import override_settings

        secret = "whsec_test_only_not_a_real_secret"
        payload = json.dumps({
            "id": "evt_test",
            "object": "event",
            "type": event_type,
            "data": {"object": {"object": obj, **session_extra}},
        }).encode()
        timestamp = int(time.time())
        sig = stripe.WebhookSignature._compute_signature(f"{timestamp}.{payload.decode()}", secret)
        with override_settings(STRIPE_WEBHOOK_SECRET=secret):
            return self.client.post(
                reverse("stripe_webhook"), data=payload, content_type="application/json",
                HTTP_STRIPE_SIGNATURE=f"t={timestamp},v1={sig}",
            )

    def test_real_signed_event_activates_the_subscription(self):
        resp = self._post_signed({
            "id": "cs_test", "payment_intent": "pi_test",
            "metadata": {"subscription_id": str(self.sub.id), "org_id": str(self.org.id)},
        })
        self.assertEqual(resp.status_code, 200)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, PlanSubscription.STATUS_ACTIVE)
        self.assertEqual(self.sub.stripe_payment_intent_id, "pi_test")

    # ---- the yearly subscription (3 Oct 2026) ----

    def test_subscription_checkout_subscribes_but_does_not_mark_paid(self):
        resp = self._post_signed({
            "id": "cs_sub", "mode": "subscription", "subscription": "sub_test", "customer": "cus_test",
            "metadata": {"subscription_id": str(self.sub.id), "org_id": str(self.org.id)},
        })
        self.assertEqual(resp.status_code, 200)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, PlanSubscription.STATUS_SCHEDULED)
        self.assertEqual(self.sub.stripe_subscription_id, "sub_test")
        self.assertIsNone(self.sub.paid_at)

    def test_first_paid_invoice_activates_it(self):
        self.sub.status = PlanSubscription.STATUS_SCHEDULED
        self.sub.stripe_subscription_id = "sub_test"
        self.sub.save()
        resp = self._post_signed({
            "id": "in_test", "amount_paid": 9900, "status": "paid",
            "hosted_invoice_url": "https://invoice.stripe.com/i/test",
            # newer API versions only carry it here
            "parent": {"subscription_details": {"subscription": "sub_test"}},
        }, event_type="invoice.paid", obj="invoice")
        self.assertEqual(resp.status_code, 200)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, PlanSubscription.STATUS_ACTIVE)
        self.assertEqual(self.sub.stripe_invoice_url, "https://invoice.stripe.com/i/test")

    def test_a_zero_invoice_does_not_count_as_paid(self):
        self.sub.status = PlanSubscription.STATUS_SCHEDULED
        self.sub.stripe_subscription_id = "sub_test"
        self.sub.save()
        self._post_signed({"id": "in_zero", "amount_paid": 0, "status": "paid", "subscription": "sub_test"},
                          event_type="invoice.paid", obj="invoice")
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, PlanSubscription.STATUS_SCHEDULED)

    def test_a_scheduled_subscription_already_sets_the_plan(self):
        from .entitlements import current_tier

        self.sub.status = PlanSubscription.STATUS_SCHEDULED
        self.sub.save()
        self.assertEqual(current_tier(self.org), (STARTER, True))

    def test_bad_signature_is_rejected_and_changes_nothing(self):
        import json

        from django.test import override_settings

        with override_settings(STRIPE_WEBHOOK_SECRET="whsec_test_only_not_a_real_secret"):
            resp = self.client.post(
                reverse("stripe_webhook"),
                data=json.dumps({"id": "evt_x", "type": "checkout.session.completed"}),
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="t=1,v1=not-a-real-signature",
            )
        self.assertEqual(resp.status_code, 400)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, PlanSubscription.STATUS_PENDING)


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
        self.assertIn(b'<tr class="is-popular has-groups" data-plan="workplace"', body)
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


class SponsorshipAjaxTests(TestCase):
    """The sponsorship form on the shared in-place submit path (22 Sep 2026).

    The other two forms are covered in accounts.tests; this one has its own
    view and its own set of required fields, and its JSON branch sits after a
    block that can set `sent` several ways — which is exactly the shape where
    a refactor silently starts answering the page instead of the script.
    """

    AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}

    def _payload(self, **over):
        data = {
            "contact_name": "Dana Okafor",
            "email": "dana@example.com",
            "organisation_name": "Lae Warehouse Crew",
            "reason": "Forty of us and no budget for it.",
        }
        data.update(over)
        return data

    def test_a_good_application_answers_json(self):
        resp = self.client.post(reverse("sponsorship"), self._payload(), **self.AJAX)
        self.assertEqual(resp["Content-Type"], "application/json")
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["title"], "Application received.")
        # It promises what the page promises: nothing is published.
        self.assertIn("nothing you wrote is published", data["message"])

    def test_a_missing_field_is_json_and_not_an_http_error(self):
        resp = self.client.post(reverse("sponsorship"),
                                self._payload(reason=""), **self.AJAX)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertTrue(data["error"])

    def test_without_the_header_the_page_still_renders(self):
        resp = self.client.post(reverse("sponsorship"), self._payload())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Thanks for asking")

    def test_a_get_is_not_treated_as_a_submission(self):
        """The JSON branch is guarded on method as well as on the header — a
        prefetcher sending XHR headers on a GET must still get the page."""
        resp = self.client.get(reverse("sponsorship"), **self.AJAX)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("application/json", resp["Content-Type"])


class YearlySubscriptionCheckoutTests(TestCase):
    """The plan is a yearly Stripe subscription billed from FIRST_BILLING_DATE,
    paid on GoodTip's own page (client, 9 Oct 2026: "whatever happens in
    GoodTip stays in GoodTip") with GoodTip's own loader, decline animation
    and success animation around Stripe's card fields (client, 10 Oct 2026).

    The card is SAVED through a SetupIntent — confirmed client-side with
    stripe.confirmSetup(), which Django never sees — and the real Stripe
    Subscription is only created once that succeeds
    (billing.services.finalize_subscription). See also the live-Stripe-API
    probes run by hand while building this (success path, decline-then-retry
    on the same intent, idempotent finalize) — not repeatable here without
    real network access, so this file checks the parameters GoodTip computes
    and sends, against a mocked Stripe client.
    """

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Test Season"})
        self.org = Organisation.objects.create(name="Yearly Org", season=self.season)

    def _owner(self):
        owner = get_user_model().objects.create_user(email="payer@x.com", password="x")
        OrgMember.objects.create(user=owner, org=self.org, is_league_owner=True)
        self.client.force_login(owner)
        return owner

    def _mock_client(self):
        from unittest.mock import MagicMock

        client = MagicMock()
        client.Customer.create.return_value = MagicMock(id="cus_x")
        client.test_helpers.TestClock.create.return_value = MagicMock(id="clock_x")
        client.SetupIntent.create.return_value = MagicMock(id="seti_x", client_secret="seti_x_secret")
        return client

    def _setup_intent_kwargs(self, **kw):
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = services.create_subscription(self.org, STARTER)
        client = self._mock_client()
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            services.create_setup_intent(sub, email="o@example.com", **kw)
        sub.refresh_from_db()
        return client, sub

    def _finalize(self, sub, *, price_amount=9900, interval="year", **mock_kw):
        """finalize_subscription, with Stripe's SetupIntent and Subscription
        responses mocked. ``mock_kw`` lets a test override the retrieved
        intent (customer id, status) to exercise the guard clauses."""
        from unittest.mock import MagicMock, patch

        from django.test import override_settings

        from . import services

        client = self._mock_client()
        intent = MagicMock(
            id=sub.stripe_setup_intent_id, status="succeeded",
            customer=sub.stripe_customer_id, payment_method="pm_x",
        )
        for k, v in mock_kw.items():
            setattr(intent, k, v)
        client.SetupIntent.retrieve.return_value = intent
        client.Price.create.return_value = MagicMock(id="price_x")
        client.Subscription.create.return_value = MagicMock(id="sub_x")
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            result = services.finalize_subscription(sub, sub.stripe_setup_intent_id)
        return client, result

    # ---- create_setup_intent: what GoodTip asks Stripe to save -------------

    def test_a_setup_intent_saves_the_card_without_charging_anything(self):
        from . import services

        client, sub = self._setup_intent_kwargs()
        kw = client.SetupIntent.create.call_args.kwargs
        self.assertEqual(kw["usage"], "off_session")
        self.assertEqual(kw["payment_method_types"], ["card"])
        self.assertEqual(kw["customer"], "cus_x")
        self.assertEqual(sub.stripe_setup_intent_id, "seti_x")
        self.assertEqual(sub.stripe_customer_id, "cus_x")
        self.assertFalse(sub.is_demo)
        self.assertFalse(sub.is_test_daily)
        del services

    def test_the_customer_is_reused_across_attempts_not_recreated(self):
        """A declined card and its retry are the same customer: creating a
        fresh customer (and so a fresh SetupIntent) on every POST would turn
        a retry into an unrelated attempt Stripe has never seen before."""
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = services.create_subscription(self.org, STARTER)
        client = self._mock_client()
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            services.create_setup_intent(sub)
            services.create_setup_intent(sub)
        self.assertEqual(client.Customer.create.call_count, 1)
        self.assertEqual(client.SetupIntent.create.call_count, 2)

    def test_a_customer_stripe_no_longer_has_is_replaced_not_fatal(self):
        """Found 11 Oct 2026: "Couldn't start checkout" on a retry, traced to
        a saved customer id Stripe had deleted (by hand, clearing other test
        data) — the exact kind of staleness this guards against, whatever
        caused it. Verified against the real Stripe API while fixing this."""
        import stripe
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = services.create_subscription(self.org, STARTER)
        sub.stripe_customer_id = "cus_gone"
        sub.save(update_fields=["stripe_customer_id"])
        client = self._mock_client()
        client.Customer.retrieve.side_effect = stripe.InvalidRequestError(
            "No such customer", "customer", code="resource_missing")
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            services.create_setup_intent(sub)
        client.Customer.create.assert_called_once()
        self.assertEqual(sub.stripe_customer_id, "cus_x")  # the mock's fresh id

    def test_a_customer_error_unrelated_to_staleness_still_raises(self):
        import stripe
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = services.create_subscription(self.org, STARTER)
        sub.stripe_customer_id = "cus_whatever"
        sub.save(update_fields=["stripe_customer_id"])
        client = self._mock_client()
        client.Customer.retrieve.side_effect = stripe.InvalidRequestError(
            "Rate limited", "customer", code="rate_limited")
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            with self.assertRaises(stripe.InvalidRequestError):
                services.create_setup_intent(sub)
        client.Customer.create.assert_not_called()

    def test_the_demo_creates_a_test_clock_customer(self):
        client, sub = self._setup_intent_kwargs(demo=True)
        self.assertEqual(client.Customer.create.call_args.kwargs["test_clock"], "clock_x")
        self.assertEqual(sub.stripe_test_clock_id, "clock_x")
        self.assertTrue(sub.is_demo)

    def test_test_daily_is_recorded_but_does_not_need_a_test_clock(self):
        client, sub = self._setup_intent_kwargs(test_daily=True)
        self.assertTrue(sub.is_test_daily)
        self.assertNotIn("test_clock", client.Customer.create.call_args.kwargs)

    def test_test_daily_refuses_live_keys(self):
        from unittest.mock import MagicMock, patch

        from django.test import override_settings

        from . import services

        sub = services.create_subscription(self.org, STARTER)
        with override_settings(STRIPE_SECRET_KEY="sk_live_x"), patch.object(services, "_client", return_value=MagicMock()):
            with self.assertRaises(services.BillingNotConfigured):
                services.create_setup_intent(sub, test_daily=True)

    def test_the_demo_refuses_live_keys(self):
        from unittest.mock import MagicMock, patch

        from django.test import override_settings

        from . import services

        sub = services.create_subscription(self.org, STARTER)
        with override_settings(STRIPE_SECRET_KEY="sk_live_x"), patch.object(services, "_client", return_value=MagicMock()):
            with self.assertRaises(services.BillingNotConfigured):
                services.create_setup_intent(sub, demo=True)

    # ---- finalize_subscription: what turns a saved card into the real sub --

    def test_finalizing_creates_a_yearly_subscription_from_the_start_date(self):
        from datetime import datetime
        from unittest.mock import patch
        from zoneinfo import ZoneInfo

        from django.utils import timezone

        from . import services

        with patch("django.utils.timezone.now", return_value=timezone.make_aware(datetime(2026, 10, 3))):
            _c, sub = self._setup_intent_kwargs()
            client, sub = self._finalize(sub)
        price_kw = client.Price.create.call_args.kwargs
        self.assertEqual(price_kw["recurring"], {"interval": "year"})
        self.assertEqual(price_kw["unit_amount"], int(tier_config(STARTER)["price"] * 100))
        sub_kw = client.Subscription.create.call_args.kwargs
        self.assertEqual(sub_kw["items"], [{"price": "price_x", "quantity": 1}])
        self.assertEqual(sub_kw["default_payment_method"], "pm_x")
        self.assertEqual(sub_kw["proration_behavior"], "none")
        # midday 31 January in Melbourne, so the date reads the 31st everywhere west of it too
        anchor = sub_kw["billing_cycle_anchor"]
        self.assertEqual(datetime.fromtimestamp(anchor, ZoneInfo("Australia/Melbourne")).isoformat(), "2027-01-31T12:00:00+11:00")
        self.assertEqual(sub.stripe_subscription_id, "sub_x")
        self.assertEqual(sub.status, PlanSubscription.STATUS_SCHEDULED)
        self.assertEqual(sub.starts_at.astimezone(ZoneInfo("Australia/Melbourne")).isoformat(), "2027-01-31T12:00:00+11:00")

    def test_once_the_date_has_passed_it_bills_straight_away(self):
        from datetime import datetime
        from unittest.mock import patch

        from django.utils import timezone

        with patch("django.utils.timezone.now", return_value=timezone.make_aware(datetime(2027, 3, 1))):
            _c, sub = self._setup_intent_kwargs()
            client, sub = self._finalize(sub)
        self.assertNotIn("billing_cycle_anchor", client.Subscription.create.call_args.kwargs)
        self.assertIsNone(sub.starts_at)

    def test_test_daily_bills_about_a_day_from_now_and_daily(self):
        from datetime import datetime, timedelta
        from unittest.mock import patch

        from django.utils import timezone

        now = timezone.make_aware(datetime(2026, 10, 6, 9, 0))
        with patch("django.utils.timezone.now", return_value=now):
            _c, sub = self._setup_intent_kwargs(test_daily=True)
            client, sub = self._finalize(sub)
        self.assertEqual(client.Price.create.call_args.kwargs["recurring"], {"interval": "day"})
        anchor = client.Subscription.create.call_args.kwargs["billing_cycle_anchor"]
        self.assertEqual(anchor, int((now + timedelta(days=1) - timedelta(minutes=5)).timestamp()))
        self.assertIn("TEST MODE", client.Subscription.create.call_args.kwargs["description"])

    def test_finalizing_twice_does_not_create_a_second_subscription(self):
        """A double-click, or the success animation's own fetch racing a
        slow network, must not charge the organisation for two subscriptions."""
        _c, sub = self._setup_intent_kwargs()
        client, sub = self._finalize(sub)
        self.assertEqual(client.Subscription.create.call_count, 1)
        # called again, on the now-finalised sub: must be a no-op
        client2, sub2 = self._finalize(sub)
        self.assertEqual(client2.Subscription.create.call_count, 0)
        self.assertEqual(sub2.stripe_subscription_id, sub.stripe_subscription_id)

    def test_an_intent_still_requiring_payment_cannot_be_finalized(self):
        from . import services

        _c, sub = self._setup_intent_kwargs()
        with self.assertRaises(services.BillingNotConfigured):
            self._finalize(sub, status="requires_payment_method")

    def test_an_intent_for_a_different_customer_is_refused(self):
        """Defence against a forged or stale setup_intent_id in the confirm
        POST: the intent Stripe hands back must belong to this subscription's
        own customer, not just exist."""
        from . import services

        _c, sub = self._setup_intent_kwargs()
        with self.assertRaises(services.BillingNotConfigured):
            self._finalize(sub, customer="cus_someone_else")

    # ---- the pay page and its confirm endpoint ------------------------------

    def test_the_pay_page_serves_the_form_from_our_own_url(self):
        from unittest.mock import MagicMock, patch

        from django.test import override_settings

        from . import services

        self._owner()
        sub = services.create_subscription(self.org, STARTER)
        sub.stripe_setup_intent_id = "seti_x"
        sub.save(update_fields=["stripe_setup_intent_id"])
        intent = MagicMock(client_secret="seti_x_secret", status="requires_payment_method")
        with override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_PUBLISHABLE_KEY="pk_test_x"), \
                patch.object(services, "open_setup_intent", return_value=intent):
            page = self.client.get(reverse("billing:pay", args=[self.org.id]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "seti_x_secret")
        self.assertContains(page, "pk_test_x")
        self.assertContains(page, 'id="payment-element"')
        self.assertContains(page, "gt-pay.js")
        self.assertNotContains(page, "checkout.stripe.com")

    def test_the_pay_page_sends_you_back_when_there_is_nothing_to_pay(self):
        self._owner()
        resp = self.client.get(reverse("billing:pay", args=[self.org.id]))
        self.assertRedirects(resp, reverse("billing:plans", args=[self.org.id]))

    def test_only_the_owner_can_open_the_pay_page(self):
        other = get_user_model().objects.create_user(email="not-owner@x.com", password="x")
        OrgMember.objects.create(user=other, org=self.org)
        self.client.force_login(other)
        self.assertEqual(
            self.client.get(reverse("billing:pay", args=[self.org.id])).status_code, 403)

    def test_pay_confirm_finalizes_and_reports_where_to_go(self):
        from unittest.mock import patch

        from . import services

        self._owner()
        _c, sub = self._setup_intent_kwargs()
        with patch.object(services, "finalize_subscription") as mocked:
            mocked.return_value = sub
            resp = self.client.post(
                reverse("billing:pay_confirm", args=[self.org.id]),
                {"setup_intent_id": sub.stripe_setup_intent_id},
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["redirect"], reverse("billing:success", args=[self.org.id]))
        mocked.assert_called_once_with(sub, sub.stripe_setup_intent_id)

    def test_pay_confirm_reports_a_decline_without_a_500(self):
        from unittest.mock import patch

        from . import services

        self._owner()
        _c, sub = self._setup_intent_kwargs()
        with patch.object(services, "finalize_subscription", side_effect=services.BillingNotConfigured("nope")):
            resp = self.client.post(
                reverse("billing:pay_confirm", args=[self.org.id]),
                {"setup_intent_id": sub.stripe_setup_intent_id},
            )
        self.assertEqual(resp.status_code, 409)
        self.assertFalse(resp.json()["ok"])

    def test_pay_confirm_needs_a_setup_intent_id(self):
        self._owner()
        resp = self.client.post(reverse("billing:pay_confirm", args=[self.org.id]), {})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])

    def test_only_the_owner_can_confirm_a_payment(self):
        other = get_user_model().objects.create_user(email="not-owner2@x.com", password="x")
        OrgMember.objects.create(user=other, org=self.org)
        self.client.force_login(other)
        resp = self.client.post(
            reverse("billing:pay_confirm", args=[self.org.id]), {"setup_intent_id": "seti_x"})
        self.assertEqual(resp.status_code, 403)

    def test_the_plans_page_hides_the_test_mode_switch_even_on_test_keys(self):
        """Client, 19 Oct 2026: "no demo testing, so just real... I want to
        see how the design will look just real life" — the switch is now
        commented out in templates/billing/plans.html regardless of which
        kind of Stripe key is configured. The mechanism underneath (daily
        test billing) is untouched for whoever flips that template block
        back for their own end-to-end testing; only its visible control is
        gone from a page a client or prospect might actually see."""
        from django.test import override_settings

        owner = get_user_model().objects.create_user(email="own@x.com", password="x")
        OrgMember.objects.create(user=owner, org=self.org, is_league_owner=True)
        self.client.force_login(owner)
        url = reverse("billing:plans", args=[self.org.id])
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"):
            page = self.client.get(url)
        self.assertNotContains(page, "class=\"pl-test-switch\"")
        self.assertNotContains(page, "Run the demo")
        with override_settings(STRIPE_SECRET_KEY="sk_live_x"):
            self.assertNotContains(self.client.get(url), "class=\"pl-test-switch\"")


class SubscriptionManagementTests(TestCase):
    """Upgrade, downgrade, opt out, and the "your subscription" panel that
    makes them visible on the Plans page (client, 10 Oct 2026). The card on
    file is reused for all of this — none of it touches the pay page."""

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2098, defaults={"label": "Mgmt Season"})
        self.org = Organisation.objects.create(name="Managed Org", season=self.season)
        self.owner = get_user_model().objects.create_user(email="mgmt@x.com", password="x")
        OrgMember.objects.create(user=self.owner, org=self.org, is_league_owner=True)
        self.client.force_login(self.owner)

    def _subscribed(self, tier=STARTER, *, active=False):
        """A finalised subscription, mocked Stripe throughout, optionally
        ACTIVE (paid_at set) rather than SCHEDULED."""
        from . import services

        sub = services.create_subscription(self.org, tier)
        sub.stripe_customer_id = "cus_x"
        sub.stripe_subscription_id = "sub_x"
        sub.status = PlanSubscription.STATUS_ACTIVE if active else PlanSubscription.STATUS_SCHEDULED
        if active:
            sub.paid_at = timezone.now()
        sub.save()
        return sub

    def _mock_client(self, **kw):
        from unittest.mock import MagicMock

        client = MagicMock()
        client.Subscription.retrieve.return_value = {"items": {"data": [{"id": "si_x"}]}}
        client.Price.create.return_value = MagicMock(id="price_y")
        for k, v in kw.items():
            setattr(client, k, v)
        return client

    # ---- services.switch_tier --------------------------------------------

    def test_switching_updates_the_stripe_item_and_the_local_row(self):
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = self._subscribed(STARTER)
        client = self._mock_client()
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            updated = services.switch_tier(sub, PRO)
        modify_kw = client.Subscription.modify.call_args.kwargs
        self.assertEqual(modify_kw["items"], [{"id": "si_x", "price": "price_y"}])
        self.assertEqual(updated.tier, PRO)
        self.assertEqual(updated.price_aud, tier_config(PRO)["price"])
        self.assertEqual(updated.seat_limit, tier_config(PRO)["seat_limit"])

    def test_switching_before_the_first_charge_has_no_proration(self):
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = self._subscribed(STARTER, active=False)
        client = self._mock_client()
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            services.switch_tier(sub, PRO)
        self.assertEqual(client.Subscription.modify.call_args.kwargs["proration_behavior"], "none")

    def test_switching_after_the_first_charge_prorates(self):
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = self._subscribed(STARTER, active=True)
        client = self._mock_client()
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            services.switch_tier(sub, PRO)
        self.assertEqual(client.Subscription.modify.call_args.kwargs["proration_behavior"], "create_prorations")

    def test_switching_to_the_same_tier_is_a_no_op(self):
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = self._subscribed(STARTER)
        client = self._mock_client()
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            services.switch_tier(sub, STARTER)
        client.Subscription.modify.assert_not_called()

    def test_switching_an_unpaid_subscription_is_refused(self):
        from . import services

        sub = services.create_subscription(self.org, STARTER)  # no stripe_subscription_id yet
        with self.assertRaises(services.BillingNotConfigured):
            services.switch_tier(sub, PRO)

    def test_switching_to_a_fake_tier_is_refused(self):
        from . import services

        sub = self._subscribed(STARTER)
        with self.assertRaises(services.BillingNotConfigured):
            services.switch_tier(sub, "not-a-real-tier")

    # ---- services.cancel_subscription / resume_subscription --------------

    def test_cancelling_a_scheduled_subscription_ends_it_outright(self):
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = self._subscribed(STARTER, active=False)
        client = self._mock_client()
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            updated = services.cancel_subscription(sub)
        client.Subscription.cancel.assert_called_once_with("sub_x")
        self.assertEqual(updated.status, PlanSubscription.STATUS_EXPIRED)
        self.assertFalse(updated.cancel_at_period_end)
        self.assertIsNotNone(updated.canceled_at)

    def test_cancelling_an_active_subscription_keeps_it_through_the_period(self):
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = self._subscribed(STARTER, active=True)
        client = self._mock_client()
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            updated = services.cancel_subscription(sub)
        client.Subscription.modify.assert_called_once_with("sub_x", cancel_at_period_end=True)
        client.Subscription.cancel.assert_not_called()
        self.assertTrue(updated.is_active)
        self.assertTrue(updated.cancel_at_period_end)

    def test_resuming_undoes_a_not_yet_effective_cancellation(self):
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = self._subscribed(STARTER, active=True)
        sub.cancel_at_period_end = True
        sub.canceled_at = timezone.now()
        sub.save()
        client = self._mock_client()
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            updated = services.resume_subscription(sub)
        client.Subscription.modify.assert_called_once_with("sub_x", cancel_at_period_end=False)
        self.assertFalse(updated.cancel_at_period_end)
        self.assertIsNone(updated.canceled_at)

    def test_resuming_something_not_cancelled_is_refused(self):
        from . import services

        sub = self._subscribed(STARTER, active=True)
        with self.assertRaises(services.BillingNotConfigured):
            services.resume_subscription(sub)

    # ---- the views: ownership, wiring, messaging --------------------------

    def test_switch_tier_view_calls_through_and_redirects(self):
        from unittest.mock import patch

        from . import services

        sub = self._subscribed(STARTER)
        with patch.object(services, "switch_tier") as mocked, \
                patch.object(services, "sync_from_stripe", return_value={}):
            mocked.return_value = sub
            resp = self.client.post(reverse("billing:switch_tier", args=[self.org.id]), {"tier": PRO})
            mocked.assert_called_once_with(sub, PRO)
            self.assertRedirects(resp, reverse("billing:plans", args=[self.org.id]))

    def test_switch_tier_view_rejects_a_fake_tier_without_calling_stripe(self):
        from unittest.mock import patch

        from . import services

        self._subscribed(STARTER)
        with patch.object(services, "switch_tier") as mocked:
            self.client.post(reverse("billing:switch_tier", args=[self.org.id]), {"tier": "nonsense"})
        mocked.assert_not_called()

    def test_switch_tier_view_needs_an_existing_subscription(self):
        from . import services

        resp = self.client.post(reverse("billing:switch_tier", args=[self.org.id]), {"tier": PRO}, follow=True)
        self.assertContains(resp, "no subscription here to change")

    def test_only_the_owner_can_switch_tier(self):
        other = get_user_model().objects.create_user(email="not-owner3@x.com", password="x")
        OrgMember.objects.create(user=other, org=self.org)
        self.client.force_login(other)
        resp = self.client.post(reverse("billing:switch_tier", args=[self.org.id]), {"tier": PRO})
        self.assertEqual(resp.status_code, 403)

    def test_cancel_view_calls_through_and_redirects(self):
        from unittest.mock import patch

        from . import services

        sub = self._subscribed(STARTER, active=True)
        with patch.object(services, "cancel_subscription") as mocked, \
                patch.object(services, "sync_from_stripe", return_value={}):
            mocked.return_value = sub
            resp = self.client.post(reverse("billing:cancel_subscription", args=[self.org.id]))
            mocked.assert_called_once_with(sub)
            self.assertRedirects(resp, reverse("billing:plans", args=[self.org.id]))

    def test_only_the_owner_can_cancel(self):
        other = get_user_model().objects.create_user(email="not-owner4@x.com", password="x")
        OrgMember.objects.create(user=other, org=self.org)
        self.client.force_login(other)
        resp = self.client.post(reverse("billing:cancel_subscription", args=[self.org.id]))
        self.assertEqual(resp.status_code, 403)

    def test_resume_view_calls_through_and_redirects(self):
        from unittest.mock import patch

        from . import services

        sub = self._subscribed(STARTER, active=True)
        with patch.object(services, "resume_subscription") as mocked, \
                patch.object(services, "sync_from_stripe", return_value={}):
            mocked.return_value = sub
            resp = self.client.post(reverse("billing:resume_subscription", args=[self.org.id]))
            mocked.assert_called_once_with(sub)
            self.assertRedirects(resp, reverse("billing:plans", args=[self.org.id]))

    # ---- the Plans page's "your subscription" panel -----------------------

    def test_an_unsubscribed_org_sees_the_ordinary_picker_only(self):
        resp = self.client.get(reverse("billing:plans", args=[self.org.id]))
        self.assertNotContains(resp, "bl-card-eyebrow")
        self.assertContains(resp, "Pay for Starter")
        self.assertNotContains(resp, "switch-tier")

    def test_a_subscribed_org_sees_the_summary_card_and_switch_buttons(self):
        from unittest.mock import patch

        from . import services

        self._subscribed(PRO, active=True)
        # plans_view calls sync_from_stripe whenever a subscription exists —
        # stubbed here so the test is not a real network call to Stripe for
        # a subscription id ("sub_x") that was never really created there.
        with patch.object(services, "sync_from_stripe", return_value={}):
            resp = self.client.get(reverse("billing:plans", args=[self.org.id]))
        # The full card moved to its own page (client, 21 Oct 2026) — the
        # Plans page keeps only the small notice pointing at it.
        self.assertContains(resp, "plk-pane is-sub")
        self.assertContains(resp, "You're on Workplace")
        self.assertContains(resp, "Upgrade to Organisation")
        self.assertContains(resp, "Downgrade to Starter")
        self.assertContains(resp, "switch-tier/")
        with patch.object(services, "sync_from_stripe", return_value={}):
            sub_resp = self.client.get(reverse("billing:subscription", args=[self.org.id]))
        self.assertContains(sub_resp, "sx-hero")

    def test_a_cancelled_but_still_active_sub_shows_resume_not_cancel(self):
        from unittest.mock import patch

        from . import services

        sub = self._subscribed(PRO, active=True)
        sub.cancel_at_period_end = True
        sub.save()
        with patch.object(services, "sync_from_stripe", return_value={}):
            resp = self.client.get(reverse("billing:subscription", args=[self.org.id]))
        self.assertContains(resp, "Keep subscription")
        self.assertNotContains(resp, "bl-card-cancel\">Cancel subscription")

    # ---- _period_progress --------------------------------------------------

    def test_period_progress_is_none_without_real_dates(self):
        from .views import _period_progress

        self.assertIsNone(_period_progress({}))
        self.assertIsNone(_period_progress({"current_period_start": "", "current_period_end": ""}))

    def test_period_progress_computes_a_sane_percentage(self):
        from datetime import timedelta

        from .views import _period_progress

        now = timezone.now()
        start = (now - timedelta(days=100)).isoformat()
        end = (now + timedelta(days=265)).isoformat()  # ~365-day span, ~27% elapsed
        p = _period_progress({"current_period_start": start, "current_period_end": end})
        self.assertIsNotNone(p)
        self.assertTrue(25 <= p["pct"] <= 29)
        self.assertEqual(p["days_left"], 265)

    # ---- sync_from_stripe extensions ---------------------------------------

    def test_sync_reads_period_dates_from_the_subscription_item(self):
        """Stripe moved current_period_start/end onto the item on this
        account's API version — verified against the real API while building
        this. A regression here is exactly the kind of thing that silently
        blanks the ring."""
        from unittest.mock import MagicMock, patch

        from django.test import override_settings

        from . import services

        sub = self._subscribed(STARTER, active=True)
        now_ts = int(timezone.now().timestamp())
        client = MagicMock()
        client.Subscription.retrieve.return_value = MagicMock(to_dict=lambda: {
            "status": "active", "cancel_at_period_end": False,
            "items": {"data": [{"current_period_start": now_ts, "current_period_end": now_ts + 86400}]},
        })
        client.Invoice.list.return_value = MagicMock(to_dict=lambda: {"data": []})
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            info = services.sync_from_stripe(sub)
        self.assertTrue(info["current_period_start"])
        self.assertTrue(info["current_period_end"])

    def test_sync_self_heals_the_local_cancel_flag_from_stripe(self):
        """A cancel made directly in the Stripe dashboard, not through
        cancel_subscription, must still show up here — Stripe is the source
        of truth, the same way .status already self-heals above."""
        from unittest.mock import MagicMock, patch

        from django.test import override_settings

        from . import services

        sub = self._subscribed(STARTER, active=True)
        self.assertFalse(sub.cancel_at_period_end)
        client = MagicMock()
        client.Subscription.retrieve.return_value = MagicMock(to_dict=lambda: {
            "status": "active", "cancel_at_period_end": True, "items": {"data": []},
        })
        client.Invoice.list.return_value = MagicMock(to_dict=lambda: {"data": []})
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            info = services.sync_from_stripe(sub)
        sub.refresh_from_db()
        self.assertTrue(info["cancel_at_period_end"])
        self.assertTrue(sub.cancel_at_period_end)


class CustomPricingTests(TestCase):
    """A price outside the five published plans (client, 11 Oct 2026):
    requesting one, Ian approving or declining it, and the organisation
    paying for it once approved — the same card-on-GoodTip's-own-page flow
    as the five published plans."""

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Custom Season"})
        self.org = Organisation.objects.create(name="Bespoke Org", season=self.season)
        self.owner = User.objects.create_user(email="bespoke@x.com", password="x")
        OrgMember.objects.create(user=self.owner, org=self.org, is_league_owner=True)
        self.client.force_login(self.owner)

    def _approved_offer(self, *, price=Decimal("1500"), seats=300, label="Bespoke Org custom plan"):
        from .models import CustomPriceRequest

        return CustomPriceRequest.objects.create(
            org=self.org, requested_by=self.owner, status=CustomPriceRequest.STATUS_APPROVED,
            price_aud=price, seat_limit=seats, label=label, decided_at=timezone.now(),
        )

    # ---- services.create_custom_subscription / current_custom_offer -------

    def test_create_custom_subscription_uses_the_offers_own_numbers(self):
        from . import services

        offer = self._approved_offer()
        sub = services.create_custom_subscription(self.org, offer)
        self.assertEqual(sub.tier, "custom")
        self.assertEqual(sub.price_aud, Decimal("1500"))
        self.assertEqual(sub.seat_limit, 300)
        self.assertEqual(sub.custom_label, "Bespoke Org custom plan")
        self.assertFalse(sub.is_founding_rate)

    def test_create_custom_subscription_reuses_the_pending_row(self):
        from . import services

        offer = self._approved_offer()
        first = services.create_custom_subscription(self.org, offer)
        second = services.create_custom_subscription(self.org, offer)
        self.assertEqual(first.pk, second.pk)

    def test_current_custom_offer_is_the_latest_approved_request(self):
        from . import services
        from .models import CustomPriceRequest

        CustomPriceRequest.objects.create(
            org=self.org, requested_by=self.owner, status=CustomPriceRequest.STATUS_DECLINED,
        )
        approved = self._approved_offer()
        self.assertEqual(services.current_custom_offer(self.org), approved)

    def test_current_custom_offer_is_none_without_an_approval(self):
        from . import services
        from .models import CustomPriceRequest

        CustomPriceRequest.objects.create(org=self.org, requested_by=self.owner)
        self.assertIsNone(services.current_custom_offer(self.org))

    # ---- PlanSubscription.get_tier_display ---------------------------------

    def test_get_tier_display_shows_the_custom_label(self):
        from . import services

        offer = self._approved_offer(label="AquaFlow partner plan")
        sub = services.create_custom_subscription(self.org, offer)
        self.assertEqual(sub.get_tier_display(), "AquaFlow partner plan")

    def test_get_tier_display_falls_back_to_custom_without_a_label(self):
        from . import services

        offer = self._approved_offer(label="")
        sub = services.create_custom_subscription(self.org, offer)
        self.assertEqual(sub.get_tier_display(), "Custom")

    def test_published_tiers_are_unaffected_by_the_override(self):
        from . import services

        sub = services.create_subscription(self.org, STARTER)
        self.assertEqual(sub.get_tier_display(), "Starter")

    # ---- request_custom_price_view -----------------------------------------

    def test_requesting_a_custom_price_saves_it_and_emails_staff(self):
        from unittest.mock import patch

        from .models import CustomPriceRequest

        admin = User.objects.create_user(email="ian@x.com", password="x", is_staff=True, is_superuser=True)
        with patch("goodtip.mail.send_template") as mocked:
            resp = self.client.post(
                reverse("billing:request_custom_price", args=[self.org.id]),
                {"message": "We need 800 seats and a dedicated ladder."},
            )
        self.assertRedirects(resp, reverse("billing:packages", args=[self.org.id]) + "#track", fetch_redirect_response=False)
        req = CustomPriceRequest.objects.get(org=self.org)
        self.assertEqual(req.status, CustomPriceRequest.STATUS_PENDING)
        self.assertEqual(req.message, "We need 800 seats and a dedicated ladder.")
        mocked.assert_called_once()
        self.assertEqual(mocked.call_args.kwargs["to"], [admin.email])

    def test_a_second_request_while_one_is_pending_is_refused(self):
        from .models import CustomPriceRequest

        CustomPriceRequest.objects.create(org=self.org, requested_by=self.owner)
        resp = self.client.post(
            reverse("billing:request_custom_price", args=[self.org.id]), {"message": "again"},
        )
        self.assertRedirects(resp, reverse("billing:packages", args=[self.org.id]) + "#track", fetch_redirect_response=False)
        self.assertEqual(CustomPriceRequest.objects.filter(org=self.org).count(), 1)

    def test_only_the_owner_can_request_a_custom_price(self):
        outsider = User.objects.create_user(email="outside@x.com", password="x")
        self.client.force_login(outsider)
        resp = self.client.post(
            reverse("billing:request_custom_price", args=[self.org.id]), {"message": "hi"},
        )
        self.assertEqual(resp.status_code, 403)

    # ---- custom_checkout_view ----------------------------------------------

    def test_custom_checkout_without_an_offer_redirects_to_plans(self):
        resp = self.client.post(reverse("billing:custom_checkout", args=[self.org.id]))
        self.assertRedirects(resp, reverse("billing:plans", args=[self.org.id]))

    def test_custom_checkout_with_an_approved_offer_starts_the_setup_intent(self):
        from unittest.mock import MagicMock, patch

        from django.test import override_settings

        from . import services

        self._approved_offer()
        client = MagicMock()
        client.Customer.create.return_value = MagicMock(id="cus_x")
        client.SetupIntent.create.return_value = MagicMock(id="seti_x", client_secret="secret_x")
        client.SetupIntent.retrieve.return_value = MagicMock(status="requires_payment_method")
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            resp = self.client.post(reverse("billing:custom_checkout", args=[self.org.id]))
            self.assertRedirects(resp, reverse("billing:pay", args=[self.org.id]))
        sub = PlanSubscription.objects.get(org=self.org)
        self.assertEqual(sub.tier, "custom")
        self.assertEqual(sub.stripe_setup_intent_id, "seti_x")

    # ---- plans_view context -------------------------------------------------

    def test_plans_page_offers_an_approved_custom_price(self):
        offer = self._approved_offer(price=Decimal("2200"), seats=400, label="Bespoke deal")
        resp = self.client.get(reverse("billing:plans", args=[self.org.id]))
        self.assertContains(resp, "Bespoke deal")
        self.assertContains(resp, ">Subscribe <")
        self.assertContains(resp, "2200")

    def test_plans_page_shows_a_pending_request_as_waiting(self):
        from .models import CustomPriceRequest

        CustomPriceRequest.objects.create(org=self.org, requested_by=self.owner)
        resp = self.client.get(reverse("billing:plans", args=[self.org.id]))
        self.assertContains(resp, "With the team now")

    def test_plans_page_shows_a_decline_note_and_still_offers_to_ask_again(self):
        from .models import CustomPriceRequest

        CustomPriceRequest.objects.create(
            org=self.org, requested_by=self.owner, status=CustomPriceRequest.STATUS_DECLINED,
            decision_note="Start with Workplace and revisit at renewal.",
            decided_at=timezone.now(),
        )
        resp = self.client.get(reverse("billing:plans", args=[self.org.id]))
        self.assertContains(resp, "Start with Workplace and revisit at renewal.")
        self.assertContains(resp, "Ask again")

    def test_plans_page_with_no_request_at_all_offers_to_ask(self):
        resp = self.client.get(reverse("billing:plans", args=[self.org.id]))
        self.assertContains(resp, "Request a custom price")
        self.assertNotContains(resp, "is with the team")

    def test_an_org_already_on_the_custom_plan_is_not_offered_it_again(self):
        from unittest.mock import patch

        from . import services

        offer = self._approved_offer()
        sub = services.create_custom_subscription(self.org, offer)
        sub.stripe_customer_id = "cus_x"
        sub.stripe_subscription_id = "sub_x"
        sub.status = PlanSubscription.STATUS_ACTIVE
        sub.save()
        with patch.object(services, "sync_from_stripe", return_value={}):
            resp = self.client.get(reverse("billing:plans", args=[self.org.id]))
        self.assertNotContains(resp, "plk-custom-row")


class CustomPackageTests(TestCase):
    """Standing custom packages (client, 19 Oct 2026): pre-made offers an
    organisation can take without asking first, each with a simulated
    review wait before it reads as approved. Sits above the negotiated
    CustomPricingTests flow, which stays the fallback for "none of these
    fit"."""

    def setUp(self):
        from .models import CustomPackage

        self.season, _ = Season.objects.get_or_create(year=2098, defaults={"label": "Package Season"})
        self.org = Organisation.objects.create(name="Schooltown PS", season=self.season)
        self.owner = User.objects.create_user(email="schooltown@x.com", password="x")
        OrgMember.objects.create(user=self.owner, org=self.org, is_league_owner=True)
        self.client.force_login(self.owner)
        self.package = CustomPackage.objects.create(
            name="Schools Plan", description="For schools.",
            price_aud=Decimal("499"), seat_limit=300, child_org_limit=5,
        )

    def test_the_plans_page_links_to_the_packages_page(self):
        """Client, 21 Oct 2026: packages open as a page of their own, not a
        grid that unfolds on the Plans page and pushes it down."""
        resp = self.client.get(reverse("billing:plans", args=[self.org.id]))
        self.assertContains(resp, "See custom packages")
        self.assertContains(resp, reverse("billing:packages", args=[self.org.id]))
        self.assertNotContains(resp, "Schools Plan")

    def test_the_packages_page_lists_active_packages(self):
        resp = self.client.get(reverse("billing:packages", args=[self.org.id]))
        self.assertContains(resp, "Schools Plan")
        self.assertContains(resp, "Take this package")
        self.assertContains(resp, "Ask for your own price")

    def test_an_inactive_package_is_not_offered(self):
        self.package.is_active = False
        self.package.save(update_fields=["is_active"])
        resp = self.client.get(reverse("billing:packages", args=[self.org.id]))
        self.assertNotContains(resp, "Schools Plan")
        plans = self.client.get(reverse("billing:plans", args=[self.org.id]))
        self.assertNotContains(plans, "See custom packages")

    def test_only_the_owner_can_see_the_packages_page(self):
        other = User.objects.create_user(email="not-owner-pk@x.com", password="x")
        OrgMember.objects.create(user=other, org=self.org)
        self.client.force_login(other)
        resp = self.client.get(reverse("billing:packages", args=[self.org.id]))
        self.assertEqual(resp.status_code, 403)

    def test_taking_a_package_creates_a_claim(self):
        from .models import CustomPackageClaim

        resp = self.client.post(reverse("billing:take_custom_package", args=[self.org.id, self.package.id]))
        self.assertRedirects(resp, reverse("billing:plans", args=[self.org.id]))
        claim = CustomPackageClaim.objects.get(org=self.org)
        self.assertEqual(claim.package, self.package)
        self.assertEqual(claim.requested_by, self.owner)
        self.assertFalse(claim.is_ready)

    def test_only_the_owner_can_take_a_package(self):
        other = User.objects.create_user(email="not-owner@x.com", password="x")
        OrgMember.objects.create(user=other, org=self.org)
        self.client.force_login(other)
        resp = self.client.post(reverse("billing:take_custom_package", args=[self.org.id, self.package.id]))
        self.assertEqual(resp.status_code, 403)

    def test_an_inactive_package_cannot_be_taken_directly(self):
        self.package.is_active = False
        self.package.save(update_fields=["is_active"])
        resp = self.client.post(reverse("billing:take_custom_package", args=[self.org.id, self.package.id]))
        self.assertEqual(resp.status_code, 404)

    def test_the_plans_page_shows_a_claim_as_reviewing_while_not_ready(self):
        from .models import CustomPackageClaim

        CustomPackageClaim.objects.create(package=self.package, org=self.org, requested_by=self.owner)
        resp = self.client.get(reverse("billing:plans", args=[self.org.id]))
        self.assertContains(resp, "Reviewing")
        self.assertContains(resp, "Schools Plan")
        self.assertNotContains(resp, "See custom packages")
        pk = self.client.get(reverse("billing:packages", args=[self.org.id]))
        self.assertContains(pk, "Reviewing Schools Plan")
        self.assertContains(pk, "Being reviewed")

    def test_the_plans_page_shows_a_ready_claim_as_approved(self):
        from .models import CustomPackageClaim

        claim = CustomPackageClaim.objects.create(package=self.package, org=self.org, requested_by=self.owner)
        claim.requested_at = timezone.now() - CustomPackageClaim.REVIEW_WAIT
        claim.save(update_fields=["requested_at"])
        resp = self.client.get(reverse("billing:plans", args=[self.org.id]))
        self.assertContains(resp, "Schools Plan")
        self.assertContains(resp, "Approved")
        self.assertContains(resp, "We'll be in touch")

    def test_an_org_already_on_the_custom_plan_is_not_offered_packages(self):
        from unittest.mock import patch

        from . import services
        from .models import CustomPriceRequest

        request = CustomPriceRequest.objects.create(
            org=self.org, requested_by=self.owner, status=CustomPriceRequest.STATUS_APPROVED,
            price_aud=Decimal("1000"), seat_limit=100, decided_at=timezone.now(),
        )
        sub = services.create_custom_subscription(self.org, request)
        sub.stripe_customer_id = "cus_x"
        sub.stripe_subscription_id = "sub_x"
        sub.status = PlanSubscription.STATUS_ACTIVE
        sub.save()
        with patch.object(services, "sync_from_stripe", return_value={}):
            resp = self.client.get(reverse("billing:plans", args=[self.org.id]))
            pk = self.client.get(reverse("billing:packages", args=[self.org.id]))
        self.assertNotContains(resp, "See custom packages")
        self.assertRedirects(pk, reverse("billing:plans", args=[self.org.id]), fetch_redirect_response=False)


class PastSubscriptionTests(TestCase):
    """Once there is no current subscription, the Plans page still says
    something true about the one that was here (client, 12 Oct 2026: "have
    that place where it tells me... you have not auto-subscribed to any
    subscription... when this ends")."""

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2097, defaults={"label": "Past Season"})
        self.org = Organisation.objects.create(name="Lapsed Org", season=self.season)
        self.owner = User.objects.create_user(email="lapsed@x.com", password="x")
        OrgMember.objects.create(user=self.owner, org=self.org, is_league_owner=True)
        self.client.force_login(self.owner)

    def test_cancelled_before_the_first_charge_says_nothing_was_charged(self):
        from . import services

        sub = services.create_subscription(self.org, STARTER)
        sub.status = PlanSubscription.STATUS_EXPIRED
        sub.canceled_at = timezone.now()
        sub.save()
        resp = self.client.get(reverse("billing:plans", args=[self.org.id]))
        self.assertContains(resp, "Not subscribed")
        # The full sentence moved to its own page (client, 21 Oct 2026).
        sub_resp = self.client.get(reverse("billing:subscription", args=[self.org.id]))
        self.assertContains(sub_resp, "before the first payment")
        self.assertContains(sub_resp, "nothing was charged")

    def test_cancelled_after_paying_says_what_it_covered(self):
        from . import services

        sub = services.create_subscription(self.org, PRO)
        sub.status = PlanSubscription.STATUS_EXPIRED
        sub.paid_at = timezone.now()
        sub.canceled_at = timezone.now()
        sub.stripe_subscription_id = ""  # no Stripe sync attempted without one
        sub.save()
        resp = self.client.get(reverse("billing:plans", args=[self.org.id]))
        self.assertContains(resp, "Not subscribed")
        sub_resp = self.client.get(reverse("billing:subscription", args=[self.org.id]))
        self.assertContains(sub_resp, "Last payment $799")
        self.assertNotContains(sub_resp, "before the first payment")

    def test_a_current_subscription_hides_the_past_one(self):
        from . import services

        old = services.create_subscription(self.org, STARTER)
        old.status = PlanSubscription.STATUS_EXPIRED
        old.canceled_at = timezone.now()
        old.save()
        new = services.create_subscription(self.org, PRO)
        new.status = PlanSubscription.STATUS_SCHEDULED
        new.save()
        resp = self.client.get(reverse("billing:plans", args=[self.org.id]))
        self.assertNotContains(resp, "Not subscribed")

    def test_with_no_subscription_ever_nothing_past_shows(self):
        resp = self.client.get(reverse("billing:plans", args=[self.org.id]))
        # The Subscription tile is always there now (client, 21 Oct 2026),
        # but for an organisation that never had one it says so neutrally —
        # no "ended" state and no link to the details of a plan that wasn't.
        self.assertContains(resp, "Not subscribed yet")
        self.assertNotContains(resp, "is-ended")
        self.assertNotContains(resp, "View details")


class PayNowTests(TestCase):
    """Charging a subscription's full price ahead of its normal date
    (client, 12 Oct 2026: "the capacity of paying the next subscription
    even before the time that it's due to be auto-deducted")."""

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2096, defaults={"label": "Pay Now Season"})
        self.org = Organisation.objects.create(name="Early Payer Org", season=self.season)
        self.owner = User.objects.create_user(email="earlypay@x.com", password="x")
        OrgMember.objects.create(user=self.owner, org=self.org, is_league_owner=True)
        self.client.force_login(self.owner)

    def _subscribed(self, *, active=False):
        from . import services

        sub = services.create_subscription(self.org, PRO)
        sub.stripe_customer_id = "cus_x"
        sub.stripe_subscription_id = "sub_x"
        sub.status = PlanSubscription.STATUS_ACTIVE if active else PlanSubscription.STATUS_SCHEDULED
        if active:
            sub.paid_at = timezone.now()
        sub.save()
        return sub

    def _mock_client(self, *, already_paid=True):
        from unittest.mock import MagicMock

        client = MagicMock()
        invoice = MagicMock(id="in_x", status="paid" if already_paid else "open",
                            hosted_invoice_url="https://pay.stripe.com/invoice/in_x")
        client.Invoice.create.return_value = invoice
        client.Invoice.finalize_invoice.return_value = invoice
        client.Invoice.pay.return_value = invoice
        return client, invoice

    def test_pay_now_charges_the_full_price_not_a_proration(self):
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = self._subscribed(active=False)
        client, invoice = self._mock_client()
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            services.pay_now(sub)
        item_kw = client.InvoiceItem.create.call_args.kwargs
        self.assertEqual(item_kw["amount"], int(sub.price_aud * 100))
        self.assertEqual(item_kw["customer"], "cus_x")

    def test_pay_now_links_the_invoice_to_the_subscription(self):
        """So an early renewal shows up in the organisation's own payment
        history (sync_from_stripe reads invoices by subscription id)
        instead of existing only as an orphaned customer-level charge —
        found live, 12 Oct 2026: the first working version charged the
        right amount but left the invoice unlinked, invisible in the
        subscription's own invoice list."""
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = self._subscribed(active=False)
        client, invoice = self._mock_client()
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            services.pay_now(sub)
        item_kw = client.InvoiceItem.create.call_args.kwargs
        create_kw = client.Invoice.create.call_args.kwargs
        self.assertEqual(item_kw["subscription"], "sub_x")
        self.assertEqual(create_kw["subscription"], "sub_x")
        # Mutually exclusive with `subscription` on this API version — found
        # live the same day, trying to keep both.
        self.assertNotIn("pending_invoice_items_behavior", create_kw)

    def test_pay_now_does_not_call_pay_when_finalize_already_paid_it(self):
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = self._subscribed(active=False)
        client, invoice = self._mock_client(already_paid=True)
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            services.pay_now(sub)
        client.Invoice.pay.assert_not_called()

    def test_pay_now_calls_pay_when_finalize_left_it_open(self):
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = self._subscribed(active=False)
        client, invoice = self._mock_client(already_paid=False)
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            services.pay_now(sub)
        client.Invoice.pay.assert_called_once_with("in_x")

    def test_pay_now_resets_the_anchor_without_a_second_charge(self):
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = self._subscribed(active=False)
        client, invoice = self._mock_client()
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            services.pay_now(sub)
        modify_kw = client.Subscription.modify.call_args.kwargs
        self.assertEqual(modify_kw["billing_cycle_anchor"], "now")
        self.assertEqual(modify_kw["proration_behavior"], "none")

    def test_pay_now_marks_the_subscription_active_and_paid(self):
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = self._subscribed(active=False)
        client, invoice = self._mock_client()
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            updated = services.pay_now(sub)
        self.assertEqual(updated.status, PlanSubscription.STATUS_ACTIVE)
        self.assertIsNotNone(updated.paid_at)
        self.assertEqual(updated.stripe_invoice_url, "https://pay.stripe.com/invoice/in_x")

    def test_pay_now_without_a_stripe_subscription_is_refused(self):
        from . import services

        sub = services.create_subscription(self.org, PRO)  # no stripe_subscription_id yet
        with self.assertRaises(services.BillingNotConfigured):
            services.pay_now(sub)

    def test_pay_now_view_calls_through_and_redirects(self):
        from unittest.mock import patch

        from django.test import override_settings

        from . import services

        sub = self._subscribed(active=False)
        client, invoice = self._mock_client()
        with override_settings(STRIPE_SECRET_KEY="sk_test_x"), patch.object(services, "_client", return_value=client):
            resp = self.client.post(reverse("billing:pay_now", args=[self.org.id]))
        # Not assertRedirects: that follows the redirect, and the plans page
        # it lands on calls sync_from_stripe with a real (unmocked) Stripe
        # client once this `with` block has exited — same trap documented
        # elsewhere in this file for switch/cancel/resume's own view tests.
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("billing:plans", args=[self.org.id]))
        client.Subscription.modify.assert_called_once()

    def test_pay_now_view_requires_ownership(self):
        outsider = User.objects.create_user(email="outsider2@x.com", password="x")
        self.client.force_login(outsider)
        self._subscribed(active=False)
        resp = self.client.post(reverse("billing:pay_now", args=[self.org.id]))
        self.assertEqual(resp.status_code, 403)

    def test_subscription_page_offers_pay_now_for_a_scheduled_subscription(self):
        self._subscribed(active=False)
        resp = self.client.get(reverse("billing:subscription", args=[self.org.id]))
        self.assertContains(resp, "Pay now")

    def test_subscription_page_hides_pay_now_once_cancelling(self):
        sub = self._subscribed(active=True)
        sub.cancel_at_period_end = True
        sub.save()
        from unittest.mock import patch

        from . import services

        with patch.object(services, "sync_from_stripe", return_value={}):
            resp = self.client.get(reverse("billing:subscription", args=[self.org.id]))
        self.assertNotContains(resp, "Pay now")


class DeclineCustomOfferTests(TestCase):
    """Accepting an approved custom price is the existing Subscribe button;
    this is the other half — saying no, with a reason (client, 12 Oct 2026:
    "you can either accept it or reject it... give a reason why")."""

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2095, defaults={"label": "Decline Season"})
        self.org = Organisation.objects.create(name="Picky Org", season=self.season)
        self.owner = User.objects.create_user(email="picky@x.com", password="x")
        OrgMember.objects.create(user=self.owner, org=self.org, is_league_owner=True)
        self.client.force_login(self.owner)

    def _approved_offer(self):
        from .models import CustomPriceRequest

        return CustomPriceRequest.objects.create(
            org=self.org, requested_by=self.owner, status=CustomPriceRequest.STATUS_APPROVED,
            price_aud=Decimal("1500"), seat_limit=300, label="Picky Org custom plan",
            decided_at=timezone.now(),
        )

    def test_declining_records_the_reason_and_stops_offering_it(self):
        from unittest.mock import patch

        from . import services
        from .models import CustomPriceRequest

        offer = self._approved_offer()
        with patch("goodtip.mail.send_template") as mocked:
            resp = self.client.post(
                reverse("billing:decline_custom_offer", args=[self.org.id]),
                {"reason": "We need 500 seats, not 300."},
            )
        self.assertRedirects(resp, reverse("billing:packages", args=[self.org.id]) + "#track", fetch_redirect_response=False)
        offer.refresh_from_db()
        self.assertTrue(offer.is_declined_by_org)
        self.assertEqual(offer.org_decline_reason, "We need 500 seats, not 300.")
        # The row is still APPROVED — the org said no to the price, which is
        # a different fact from Ian never having approved it at all.
        self.assertEqual(offer.status, CustomPriceRequest.STATUS_APPROVED)
        self.assertIsNone(services.current_custom_offer(self.org))
        # No staff/admin account exists in this test's setUp, so there is
        # nobody to notify — test_declining_notifies_staff_with_the_reason
        # covers the case where there is.
        mocked.assert_not_called()

    def test_declining_notifies_staff_with_the_reason(self):
        from unittest.mock import patch

        admin = User.objects.create_user(email="ian2@x.com", password="x", is_staff=True, is_superuser=True)
        offer = self._approved_offer()
        with patch("goodtip.mail.send_template") as mocked:
            self.client.post(
                reverse("billing:decline_custom_offer", args=[self.org.id]),
                {"reason": "Too expensive for us."},
            )
        self.assertEqual(mocked.call_args.kwargs["to"], [admin.email])
        self.assertIn("declined", mocked.call_args.kwargs["subject"].lower())

    def test_declining_without_an_offer_is_a_no_op(self):
        resp = self.client.post(
            reverse("billing:decline_custom_offer", args=[self.org.id]), {"reason": "x"},
        )
        self.assertRedirects(resp, reverse("billing:plans", args=[self.org.id]))

    def test_only_the_owner_can_decline(self):
        outsider = User.objects.create_user(email="outsider3@x.com", password="x")
        self.client.force_login(outsider)
        self._approved_offer()
        resp = self.client.post(
            reverse("billing:decline_custom_offer", args=[self.org.id]), {"reason": "x"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_plans_page_shows_the_decline_and_offers_to_ask_again(self):
        from . import services

        offer = self._approved_offer()
        services.decline_custom_offer(offer, reason="Need more seats than that.")
        resp = self.client.get(reverse("billing:plans", args=[self.org.id]))
        self.assertContains(resp, "Need more seats than that.")
        self.assertContains(resp, "Ask again")
        self.assertNotContains(resp, "Subscribe <")

    def test_declining_then_asking_again_is_allowed(self):
        from . import services

        offer = self._approved_offer()
        services.decline_custom_offer(offer, reason="x")
        resp = self.client.post(
            reverse("billing:request_custom_price", args=[self.org.id]), {"message": "Let's try again."},
        )
        self.assertRedirects(resp, reverse("billing:packages", args=[self.org.id]) + "#track", fetch_redirect_response=False)


class CustomPriceConversationTests(TestCase):
    """The custom-price conversation, from the organisation's side (client,
    24 Oct 2026: "being able to custom request a price... see some
    notifications... follow-up... me being able to reach back")."""

    def setUp(self):
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "Convo Season"})
        self.org = Organisation.objects.create(name="Talkative Org", season=self.season)
        self.owner = User.objects.create_user(email="talk@x.com", password="x")
        OrgMember.objects.create(user=self.owner, org=self.org, is_league_owner=True)
        self.client.force_login(self.owner)

    def test_the_request_form_keeps_the_numbers_and_writes_them_into_the_message(self):
        from unittest.mock import patch

        from .models import CustomPriceRequest

        with patch("goodtip.mail.send_template"):
            self.client.post(reverse("billing:request_custom_price", args=[self.org.id]), {
                "org_kind": "school", "people_count": "320", "sites_count": "3",
                "budget_aud": "900", "message": "Starting term two.",
            })
        req = CustomPriceRequest.objects.get(org=self.org)
        self.assertEqual((req.people_count, req.sites_count, req.budget_aud, req.org_kind), (320, 3, 900, "school"))
        self.assertIn("320 people", req.message)
        self.assertIn("3 sites", req.message)
        self.assertIn("Starting term two.", req.message)

    def test_a_request_with_neither_a_size_nor_a_note_is_refused(self):
        from .models import CustomPriceRequest

        self.client.post(reverse("billing:request_custom_price", args=[self.org.id]), {"message": ""})
        self.assertFalse(CustomPriceRequest.objects.filter(org=self.org).exists())

    def test_writing_back_adds_to_the_thread_and_tells_the_team(self):
        from unittest.mock import patch

        from .models import CustomPriceRequest

        User.objects.create_user(email="ian-convo@x.com", password="x", is_staff=True, is_superuser=True)
        req = CustomPriceRequest.objects.create(org=self.org, requested_by=self.owner, message="Hi")
        with patch("goodtip.mail.send_template") as mocked:
            resp = self.client.post(reverse("billing:custom_price_reply", args=[self.org.id, req.id]),
                                    {"body": "Can we start in March?"})
        self.assertRedirects(resp, reverse("billing:packages", args=[self.org.id]) + "#track",
                             fetch_redirect_response=False)
        msg = req.thread.get()
        self.assertFalse(msg.from_team)
        self.assertEqual(msg.body, "Can we start in March?")
        mocked.assert_called_once()
        self.assertIn("ian-convo@x.com", mocked.call_args.kwargs["to"])

    def test_someone_elses_request_cannot_be_written_to(self):
        from .models import CustomPriceRequest

        other_org = Organisation.objects.create(name="Other Org", season=self.season)
        req = CustomPriceRequest.objects.create(org=other_org, requested_by=self.owner, message="x")
        resp = self.client.post(reverse("billing:custom_price_reply", args=[self.org.id, req.id]), {"body": "hi"})
        self.assertEqual(resp.status_code, 404)

    def test_the_page_shows_the_offer_and_the_conversation(self):
        from .models import CustomPriceMessage, CustomPriceRequest

        req = CustomPriceRequest.objects.create(
            org=self.org, requested_by=self.owner, message="We need 300 seats",
            status=CustomPriceRequest.STATUS_APPROVED, price_aud=Decimal("1200"), seat_limit=300,
            label="Talkative deal", decided_at=timezone.now(),
        )
        CustomPriceMessage.objects.create(request=req, from_team=True, body="Happy to help.")
        resp = self.client.get(reverse("billing:packages", args=[self.org.id]))
        self.assertContains(resp, "Your offer is ready")
        self.assertContains(resp, "Accept &amp; subscribe")
        self.assertContains(resp, "We need 300 seats")
        self.assertContains(resp, "Happy to help.")

    def test_accepting_an_offer_stamps_it(self):
        from unittest.mock import patch

        from .models import CustomPriceRequest

        req = CustomPriceRequest.objects.create(
            org=self.org, requested_by=self.owner, status=CustomPriceRequest.STATUS_APPROVED,
            price_aud=Decimal("1200"), seat_limit=300, label="Deal", decided_at=timezone.now(),
        )
        with patch("goodtip.mail.send_template"):
            self.client.post(reverse("billing:custom_checkout", args=[self.org.id]))
        req.refresh_from_db()
        self.assertIsNotNone(req.accepted_at)
        self.assertEqual(req.stage, "accepted")

    def test_the_subscription_page_draws_four_charts_without_a_plan(self):
        resp = self.client.get(reverse("billing:subscription", args=[self.org.id]))
        self.assertContains(resp, 'id="c3d-data"')
        self.assertEqual(resp.content.decode().count('data-c3d="'), 4)
        self.assertContains(resp, "has no plan right now")
