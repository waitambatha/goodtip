"""Custom pricing requests, from the super admin's side: the queue, approving
one (which sets a real price the organisation can subscribe to), and
declining one (client, 11 Oct 2026)."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from billing.models import CustomPriceRequest
from catalog.models import Season
from orgs.models import Organisation
from sysadmin.models import AdminAccess
from sysadmin.test_delegation import make_admin, sign_in

User = get_user_model()


@override_settings(STAGING_GATE=False)
class CustomPricingQueueTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser(
            email="boss@example.com", password="pw", display_name="Boss")
        AdminAccess.objects.create(user=self.boss, is_full_access=True)
        self.season, _ = Season.objects.get_or_create(year=2099, defaults={"label": "CPR Season"})
        self.org = Organisation.objects.create(name="Bespoke Org", season=self.season)
        self.requester = User.objects.create_user(email="owner@bespoke.com", password="x")

    def _request(self, **over):
        fields = dict(org=self.org, requested_by=self.requester, message="Need 800 seats.")
        fields.update(over)
        return CustomPriceRequest.objects.create(**fields)

    def test_someone_without_the_capability_gets_a_404_not_a_403(self):
        """Matches waitlist_views.py's own guard: a restricted administrator
        sees the screen as absent, not as forbidden — the nav already hides
        the link, so a 403 here would only confirm the screen exists."""
        plain = make_admin("plain@example.com", by=self.boss)
        sign_in(self.client, plain)
        self.assertEqual(self.client.get(reverse("admin:hq_custom_pricing")).status_code, 404)

    def test_a_granted_administrator_can_open_the_queue(self):
        granted = make_admin("granted@example.com", grants=["billing.custom_pricing"], by=self.boss)
        sign_in(self.client, granted)
        req = self._request()
        resp = self.client.get(reverse("admin:hq_custom_pricing"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Bespoke Org")

    def test_approving_sets_the_price_and_offers_it_to_the_org(self):
        from billing import services

        sign_in(self.client, self.boss)
        req = self._request()
        resp = self.client.post(
            reverse("admin:hq_custom_pricing_decide", args=[req.id]),
            {"action": "approve", "price_aud": "1500", "seat_limit": "300", "label": "Bespoke deal"},
        )
        self.assertRedirects(resp, reverse("admin:hq_custom_pricing"))
        req.refresh_from_db()
        self.assertEqual(req.status, CustomPriceRequest.STATUS_APPROVED)
        self.assertEqual(req.price_aud, Decimal("1500"))
        self.assertEqual(req.seat_limit, 300)
        self.assertEqual(req.label, "Bespoke deal")
        self.assertEqual(req.decided_by, self.boss)
        self.assertIsNotNone(req.decided_at)
        self.assertEqual(services.current_custom_offer(self.org), req)

    def test_approving_without_a_real_price_is_refused(self):
        sign_in(self.client, self.boss)
        req = self._request()
        self.client.post(
            reverse("admin:hq_custom_pricing_decide", args=[req.id]),
            {"action": "approve", "price_aud": "0", "seat_limit": "300"},
        )
        req.refresh_from_db()
        self.assertEqual(req.status, CustomPriceRequest.STATUS_PENDING)

    def test_declining_records_the_note_and_does_not_set_a_price(self):
        sign_in(self.client, self.boss)
        req = self._request()
        resp = self.client.post(
            reverse("admin:hq_custom_pricing_decide", args=[req.id]),
            {"action": "decline", "decision_note": "Workplace already covers this."},
        )
        self.assertRedirects(resp, reverse("admin:hq_custom_pricing"))
        req.refresh_from_db()
        self.assertEqual(req.status, CustomPriceRequest.STATUS_DECLINED)
        self.assertEqual(req.decision_note, "Workplace already covers this.")
        self.assertIsNone(req.price_aud)

    def test_a_request_already_decided_cannot_be_decided_again(self):
        sign_in(self.client, self.boss)
        req = self._request(status=CustomPriceRequest.STATUS_APPROVED, price_aud=Decimal("500"),
                            seat_limit=50, label="Old deal", decided_by=self.boss)
        self.client.post(
            reverse("admin:hq_custom_pricing_decide", args=[req.id]),
            {"action": "decline", "decision_note": "too late"},
        )
        req.refresh_from_db()
        self.assertEqual(req.status, CustomPriceRequest.STATUS_APPROVED)
        self.assertEqual(req.price_aud, Decimal("500"))

    def test_deciding_without_the_capability_is_a_404(self):
        plain = make_admin("plain2@example.com", by=self.boss)
        sign_in(self.client, plain)
        req = self._request()
        resp = self.client.post(
            reverse("admin:hq_custom_pricing_decide", args=[req.id]),
            {"action": "decline"},
        )
        self.assertEqual(resp.status_code, 404)
        req.refresh_from_db()
        self.assertEqual(req.status, CustomPriceRequest.STATUS_PENDING)
