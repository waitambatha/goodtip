"""The ORGANISATION admin's URLs.

Everything system-level that used to live here — the sync panel, the public
site's enquiries, the news editor — moved to /admin/ when the two areas were
split. What is left is what the creator of an organisation needs to run the
organisation they created, and every view behind it is scoped by
admin_panel.perms.managed_orgs().
"""
from django.urls import path

from . import org_views, prefect_views, views


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
    # PREFECT — the review desk. Everything the chat moderator and the members
    # raise arrives here, and every consequence in the feature is applied from
    # here by a person.
    path("prefect/", prefect_views.prefect_queue, name="prefect"),
    path("prefect/<int:flag_id>/", prefect_views.prefect_flag, name="prefect_flag"),
    path("prefect/<int:flag_id>/clear/", prefect_views.prefect_clear, name="prefect_clear"),
    path("prefect/<int:flag_id>/act/", prefect_views.prefect_act, name="prefect_act"),
    path("prefect/lift/<int:sanction_id>/", prefect_views.prefect_lift, name="prefect_lift"),
    path("prefect/allow/<int:allowance_id>/remove/", prefect_views.prefect_unlearn, name="prefect_unlearn"),
]
