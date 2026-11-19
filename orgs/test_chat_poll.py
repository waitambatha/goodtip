"""The open conversation's twelve-second refresh (12 Nov 2026): when nothing
changed it answers 204 and the page is left alone — no redraw, no flicker —
and anything that does change what you see (a new message, a reaction, a read
receipt) makes it send the list again."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from catalog.models import Season
from orgs.models import Message, OrgMember, Organisation
from orgs.services import org_room
from orgs.views import _thread_version

User = get_user_model()


class ChatPollTests(TestCase):
    def setUp(self):
        season = Season.objects.create(year=2092, label="2092")
        self.org = Organisation.objects.create(name="Poll Org", season=season)
        self.me = User.objects.create_user(email="p@x.test", password="x", display_name="Pat")
        self.you = User.objects.create_user(email="q@x.test", password="x", display_name="Quin")
        for u in (self.me, self.you):
            OrgMember.objects.create(user=u, org=self.org)
        self.thread = org_room(self.org)
        Message.objects.create(thread=self.thread, author=self.you, body="Hello")
        self.client.force_login(self.me)
        self.url = reverse("orgs:member_message_thread", args=[self.org.id, self.thread.id])

    def poll(self, v):
        return self.client.get(self.url, {"pane": "chat", "v": v}, HTTP_HX_REQUEST="true")

    def test_nothing_new_is_204_and_draws_nothing(self):
        first = self.client.get(self.url, {"pane": "chat"}, HTTP_HX_REQUEST="true")
        v = first.content.decode().split('data-v="')[1].split('"')[0]
        r = self.poll(v)
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.content, b"")

    def test_a_new_message_sends_the_list_again(self):
        self.client.get(self.url, {"pane": "chat"}, HTTP_HX_REQUEST="true")
        v = _thread_version(self.thread)
        Message.objects.create(thread=self.thread, author=self.you, body="Are you there?")
        r = self.poll(v)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Are you there?", r.content.decode())

    def test_opening_the_panel_skips_the_sidebar(self):
        r = self.client.get(self.url, {"pane": "room"}, HTTP_HX_REQUEST="true")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["conversations"], [])
