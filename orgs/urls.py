from django.urls import path

from . import views


app_name = "orgs"

urlpatterns = [
    path("new/", views.create_org_view, name="create"),
    path("search/", views.org_search_view, name="search"),
    path("search.json", views.org_search_json, name="search_json"),
    path("<int:org_id>/created/", views.org_created_view, name="created"),
    path("<int:org_id>/invite/", views.org_invite_view, name="invite"),
    path("<int:org_id>/members/", views.members_view, name="members"),
    path("<int:org_id>/settings/", views.org_settings_view, name="settings"),
    path("<int:org_id>/groups/", views.groups_view, name="groups"),
    path("<int:org_id>/groups/toggle/", views.groups_toggle, name="groups_toggle"),
    path("<int:org_id>/requests/<int:req_id>/", views.review_request_view, name="review_request"),
    path("<int:org_id>/request-join/", views.request_join_view, name="request_join"),
    path("<int:org_id>/charity-vote/", views.charity_vote_view, name="charity_vote"),
    path("<int:org_id>/charity-vote/cast/", views.cast_charity_vote, name="cast_charity_vote"),
    path("<int:org_id>/charity-vote/close/", views.close_charity_vote_view, name="close_charity_vote"),
    path("<int:org_id>/charity-vote/close-time/", views.election_close_time_view, name="election_close_time"),
    path("<int:org_id>/lock-fundraising/", views.lock_fundraising_view, name="lock_fundraising"),
    path("<int:org_id>/election/", views.election_setup_view, name="election_setup"),
    path("<int:org_id>/wall/", views.wall_view, name="wall"),
    path("<int:org_id>/wall/post/", views.wall_post_create, name="wall_post"),
    path("<int:org_id>/wall/<int:post_id>/react/", views.wall_react, name="wall_react"),
    path("<int:org_id>/wall/<int:post_id>/remove/", views.wall_post_remove, name="wall_remove"),
    path("<int:org_id>/wall/<int:post_id>/reply/", views.wall_reply_create, name="wall_reply"),
    path("<int:org_id>/wall/reply/<int:reply_id>/remove/", views.wall_reply_remove, name="wall_reply_remove"),
    # Where the user is. POST, because each one changes state that outlives
    # the request — a GET switcher would be followed by every link prefetcher
    # and page-preview crawler that touched the nav.
    path("<int:org_id>/switch/", views.switch_org, name="switch_org"),
    path("<int:org_id>/groups/<int:group_id>/switch/", views.switch_group, name="switch_group"),
    path("groups/leave-context/", views.leave_group_view, name="leave_group_context"),
    path("notifications/<int:note_id>/dismiss/", views.dismiss_notification, name="dismiss_notification"),
    path("notifications/<int:note_id>/open/", views.notification_open, name="notification_open"),
    path("notifications/read-all/", views.notifications_read_all, name="notifications_read_all"),
    path("notifications/feed.json", views.notifications_feed, name="notifications_feed"),
]
