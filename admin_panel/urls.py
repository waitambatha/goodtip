"""The ORGANISATION admin's URLs.

Everything system-level that used to live here — the sync panel, the public
site's enquiries, the news editor — moved to /admin/ when the two areas were
split. What is left is what the creator of an organisation needs to run the
organisation they created, and every view behind it is scoped by
admin_panel.perms.managed_orgs().
"""
from django.urls import path

from . import org_views, views


app_name = "manage"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("approvals/", views.approvals, name="approvals"),
    path("orgs/", views.orgs_list, name="orgs_list"),
    path("org/<int:org_id>/rounds/", views.org_rounds, name="org_rounds"),
    path("org/<int:org_id>/round/<int:round_id>/matches/", views.round_matches, name="round_matches"),
    path("org/<int:org_id>/members/", views.org_members, name="org_members"),
    # Member <-> admin conversation, new with the split: a member had nowhere
    # to raise anything with their own admin, because the public contact form
    # goes to GoodTip rather than to their organisation.
    path("messages/", org_views.message_list, name="messages"),
    path("messages/new/", org_views.message_new, name="message_new"),
    path("messages/<int:thread_id>/", org_views.message_thread, name="message_thread"),
    path("charity/", org_views.charity_redirect, name="charity"),
]
