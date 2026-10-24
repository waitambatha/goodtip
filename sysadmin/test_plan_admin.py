"""Ian's own edits to the five published plans, and the standing custom
packages (client, 19 Oct 2026). See sysadmin/plan_admin_views.py."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from billing.models import CustomPackage, PlanOverride
from billing.pricing import STARTER, effective_tier_config
from sysadmin.test_delegation import make_admin, sign_in

User = get_user_model()


@override_settings(STAGING_GATE=False)
class PlanOverrideTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser(email="boss@example.com", password="pw")

    def test_someone_without_the_capability_gets_a_404_not_a_403(self):
        plain = make_admin("plain@example.com", by=self.boss)
        sign_in(self.client, plain)
        self.assertEqual(self.client.get(reverse("admin:hq_plan_admin")).status_code, 404)

    def test_a_granted_administrator_can_open_the_hub(self):
        granted = make_admin("granted@example.com", grants=["billing.plan_admin"], by=self.boss)
        sign_in(self.client, granted)
        resp = self.client.get(reverse("admin:hq_plan_admin"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Starter")

    def test_saving_a_price_and_limits_creates_an_override(self):
        sign_in(self.client, self.boss)
        resp = self.client.post(
            reverse("admin:hq_plan_override_save", args=[STARTER]),
            {"price_aud": "89", "seat_limit": "25", "child_org_limit": "2"},
        )
        self.assertRedirects(resp, reverse("admin:hq_plan_detail", args=[STARTER]))
        override = PlanOverride.objects.get(tier=STARTER)
        self.assertEqual(override.price_aud, Decimal("89"))
        self.assertEqual(override.seat_limit, 25)
        self.assertEqual(override.child_org_limit, 2)
        cfg = effective_tier_config(STARTER)
        self.assertEqual(cfg["price"], Decimal("89"))
        self.assertEqual(cfg["seat_limit"], 25)
        self.assertEqual(cfg["child_org_limit"], 2)

    def test_a_percentage_discount_is_saved_as_an_amount_too(self):
        sign_in(self.client, self.boss)
        self.client.post(
            reverse("admin:hq_plan_override_save", args=[STARTER]),
            {"price_aud": "100", "discount_percent": "10", "discount_mode": "percent"},
        )
        override = PlanOverride.objects.get(tier=STARTER)
        self.assertEqual(override.discount_percent, Decimal("10.00"))
        self.assertEqual(override.discount_amount_aud, Decimal("10.00"))
        self.assertEqual(effective_tier_config(STARTER)["price"], Decimal("90.00"))

    def test_a_flat_discount_is_saved_as_a_percentage_too(self):
        sign_in(self.client, self.boss)
        self.client.post(
            reverse("admin:hq_plan_override_save", args=[STARTER]),
            {"price_aud": "200", "discount_amount_aud": "50", "discount_mode": "amount"},
        )
        override = PlanOverride.objects.get(tier=STARTER)
        self.assertEqual(override.discount_amount_aud, Decimal("50.00"))
        self.assertEqual(override.discount_percent, Decimal("25.00"))
        self.assertEqual(effective_tier_config(STARTER)["price"], Decimal("150.00"))

    def test_a_blank_field_clears_back_to_the_published_default(self):
        PlanOverride.objects.create(tier=STARTER, price_aud=Decimal("50"))
        sign_in(self.client, self.boss)
        self.client.post(reverse("admin:hq_plan_override_save", args=[STARTER]), {})
        override = PlanOverride.objects.get(tier=STARTER)
        self.assertIsNone(override.price_aud)
        self.assertIsNone(override.seat_limit)

    def test_saving_without_the_capability_is_a_404(self):
        plain = make_admin("plain2@example.com", by=self.boss)
        sign_in(self.client, plain)
        resp = self.client.post(
            reverse("admin:hq_plan_override_save", args=[STARTER]), {"price_aud": "1"},
        )
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(PlanOverride.objects.filter(tier=STARTER).exists())


@override_settings(STAGING_GATE=False)
class CustomPackageAdminTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser(email="boss2@example.com", password="pw")

    def test_creating_a_package(self):
        sign_in(self.client, self.boss)
        resp = self.client.post(reverse("admin:hq_custom_package_save"), {
            "name": "Schools Plan", "description": "For schools.",
            "price_aud": "499", "seat_limit": "100", "child_org_limit": "5",
            "sort_order": "0", "is_active": "on",
        })
        pkg = CustomPackage.objects.get(name="Schools Plan")
        self.assertRedirects(resp, reverse("admin:hq_custom_package_detail", args=[pkg.pk]))
        self.assertEqual(pkg.price_aud, Decimal("499"))
        self.assertEqual(pkg.seat_limit, 100)
        self.assertTrue(pkg.is_active)

    def test_editing_an_existing_package(self):
        pkg = CustomPackage.objects.create(name="Old", price_aud=Decimal("100"), seat_limit=10)
        sign_in(self.client, self.boss)
        self.client.post(reverse("admin:hq_custom_package_edit", args=[pkg.pk]), {
            "name": "New Name", "price_aud": "150", "seat_limit": "20",
            "child_org_limit": "1", "sort_order": "0",
        })
        pkg.refresh_from_db()
        self.assertEqual(pkg.name, "New Name")
        self.assertEqual(pkg.price_aud, Decimal("150"))
        self.assertFalse(pkg.is_active)  # checkbox omitted from this POST

    def test_creating_without_a_real_price_is_refused(self):
        sign_in(self.client, self.boss)
        self.client.post(reverse("admin:hq_custom_package_save"), {
            "name": "Bad", "price_aud": "0", "seat_limit": "10",
        })
        self.assertFalse(CustomPackage.objects.filter(name="Bad").exists())


@override_settings(STAGING_GATE=False)
class PlanPagesTests(TestCase):
    """Each plan and package has a page of its own (client, 24 Oct 2026)."""

    def setUp(self):
        self.boss = User.objects.create_superuser(email="boss-pages@example.com", password="pw")
        sign_in(self.client, self.boss)

    def test_every_plan_opens_on_its_own_page(self):
        from billing.pricing import TIER_ORDER, tier_label

        for tier in TIER_ORDER:
            with self.subTest(tier=tier):
                resp = self.client.get(reverse("admin:hq_plan_detail", args=[tier]))
                self.assertContains(resp, tier_label(tier))
                self.assertContains(resp, reverse("admin:hq_plan_override_save", args=[tier]))

    def test_an_unknown_plan_is_a_404(self):
        self.assertEqual(self.client.get(reverse("admin:hq_plan_detail", args=["nope"])).status_code, 404)

    def test_a_package_opens_on_its_own_page_and_a_new_one_can_be_started(self):
        pkg = CustomPackage.objects.create(name="Clubs Pack", price_aud=Decimal("300"), seat_limit=120)
        self.assertContains(self.client.get(reverse("admin:hq_custom_package_detail", args=[pkg.pk])), "Clubs Pack")
        self.assertContains(self.client.get(reverse("admin:hq_custom_package_new")), "Create package")
        self.assertContains(self.client.get(reverse("admin:hq_plan_admin")),
                            reverse("admin:hq_custom_package_detail", args=[pkg.pk]))
