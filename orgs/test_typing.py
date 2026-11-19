"""'Sam is typing…' (16 Nov 2026): a typist shows to the others in the same
conversation for a few seconds, never to themselves, and never to anyone who
cannot read that conversation."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.test import TestCase, override_settings
from django.urls import reverse

from catalog.models import Season
from orgs.models import OrgMember, Organisation
from orgs.services import org_room

User = get_user_model()

LOCMEM = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "typing": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "typing-tests"},
}


@override_settings(CACHES=LOCMEM)
class TypingTests(TestCase):
    def setUp(self):
        caches["typing"].clear()
        season = Season.objects.create(year=2093, label="2093")
        self.org = Organisation.objects.create(name="Typing Org", season=season)
        self.other_org = Organisation.objects.create(name="Elsewhere", season=season)
        self.sam = User.objects.create_user(email="sam@x.test", password="x", display_name="Sam Lee")
        self.pat = User.objects.create_user(email="pat@x.test", password="x", display_name="Pat")
        self.out = User.objects.create_user(email="out@x.test", password="x", display_name="Outsider")
        for u in (self.sam, self.pat):
            OrgMember.objects.create(user=u, org=self.org)
        OrgMember.objects.create(user=self.out, org=self.other_org)
        self.thread = org_room(self.org)
        self.url = reverse("orgs:typing", args=[self.org.id])
        self.ch = f"t{self.thread.id}"

    def type_as(self, user, ch, state="1"):
        self.client.force_login(user)
        return self.client.post(self.url, {"ch": ch, "state": state})

    def who(self, user, ch):
        self.client.force_login(user)
        return self.client.get(self.url, {"ch": ch})

    def test_a_typist_shows_to_others_by_first_name(self):
        self.assertEqual(self.type_as(self.sam, self.ch).status_code, 200)
        self.assertEqual(self.who(self.pat, self.ch).json(), {"typing": ["Sam"]})

    def test_never_shown_to_themselves(self):
        self.type_as(self.sam, self.ch)
        self.assertEqual(self.who(self.sam, self.ch).json(), {"typing": []})

    def test_stopping_clears_at_once(self):
        self.type_as(self.sam, self.ch)
        self.type_as(self.sam, self.ch, state="0")
        self.assertEqual(self.who(self.pat, self.ch).json(), {"typing": []})

    def test_expires_on_its_own(self):
        with mock.patch("time.time", return_value=1_000_000.0):
            self.type_as(self.sam, self.ch)
        with mock.patch("time.time", return_value=1_000_007.0):
            self.assertEqual(self.who(self.pat, self.ch).json(), {"typing": []})

    def test_wall_rooms_are_separate(self):
        self.type_as(self.sam, "w0:afl")
        self.assertEqual(self.who(self.pat, "w0:afl").json(), {"typing": ["Sam"]})
        self.assertEqual(self.who(self.pat, "w0:nrl").json(), {"typing": []})

    def test_outsiders_and_bad_channels_are_refused(self):
        self.assertEqual(self.who(self.out, self.ch).status_code, 403)
        self.assertEqual(self.who(self.out, "w0:afl").status_code, 403)
        self.assertEqual(self.who(self.pat, "../etc").status_code, 403)
