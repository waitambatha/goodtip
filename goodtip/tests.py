from django.test import TestCase, override_settings

GATE_ON = dict(
    STAGING_GATE=True,
    STAGING_GATE_USERS="team:Team-Pass-1234,client:Client-Pass-5678",
)


@override_settings(STAGING_GATE=False)
class StagingGateOffTests(TestCase):
    def test_site_open_when_gate_disabled(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_gate_page_redirects_home_when_disabled(self):
        resp = self.client.get("/gate/")
        self.assertRedirects(resp, "/")


@override_settings(**GATE_ON)
class StagingGateOnTests(TestCase):
    def unlock(self, username="team", password="Team-Pass-1234", next_url="/"):
        return self.client.post("/gate/", {
            "username": username, "password": password, "next": next_url,
        })

    def test_locked_site_redirects_to_gate(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/gate/?next=/")

    def test_gate_page_renders(self):
        resp = self.client.get("/gate/?next=/pricing/")
        self.assertEqual(resp.status_code, 401)
        self.assertContains(resp, "Private preview", status_code=401)

    def test_wrong_credentials_rejected(self):
        resp = self.unlock(password="wrong")
        self.assertEqual(resp.status_code, 401)
        self.assertContains(resp, "didn't match", status_code=401)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_team_credentials_unlock_site(self):
        resp = self.unlock()
        self.assertRedirects(resp, "/")
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_client_credentials_unlock_site(self):
        resp = self.unlock(username="client", password="Client-Pass-5678")
        self.assertRedirects(resp, "/")
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_external_next_url_ignored(self):
        resp = self.unlock(next_url="https://evil.example.com/")
        self.assertRedirects(resp, "/")

    def test_stripe_webhook_exempt(self):
        # Stripe's servers can't pass the gate; the view must stay reachable.
        # (400 = signature check failed, which means the view itself ran.)
        resp = self.client.post("/stripe/webhook/", data="{}", content_type="application/json")
        self.assertNotEqual(resp.status_code, 302)

    def test_signup_reachable_after_unlock(self):
        self.unlock()
        resp = self.client.get("/signup/")
        self.assertEqual(resp.status_code, 200)
