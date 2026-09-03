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
    # The organisation's own charity list — vetted plus whatever it added.
    path("<int:org_id>/charities/", views.org_charities_view, name="charities"),
    # Fix one up — its name, its website, and the logo the automatic fetch
    # could not find. Scoped to what this organisation can see; who may
    # actually change it is orgs.views._can_edit_charity.
    path("<int:org_id>/charities/<int:charity_id>/edit/", views.org_charity_edit_view,
         name="charity_edit"),
    # A GROUP's election. Separate routes rather than a query string on the
    # org ones: every permission check below is different (the group's members
    # rather than the organisation's), and a scope that important should not
    # depend on a parameter someone can drop.
    path("<int:org_id>/groups/<int:group_id>/charity/",
         views.group_charity_vote_view, name="group_charity_vote"),
    path("<int:org_id>/groups/<int:group_id>/charity/setup/",
         views.group_election_setup_view, name="group_election_setup"),
    path("<int:org_id>/groups/<int:group_id>/charity/cast/",
         views.cast_group_charity_vote, name="cast_group_charity_vote"),
    path("<int:org_id>/groups/<int:group_id>/charity/close/",
         views.close_group_charity_vote, name="close_group_charity_vote"),
    path("<int:org_id>/groups/<int:group_id>/charity/captains-call/",
         views.group_captains_call, name="group_captains_call"),
    path("<int:org_id>/charity-vote/", views.charity_vote_view, name="charity_vote"),
    path("<int:org_id>/charity-vote/cast/", views.cast_charity_vote, name="cast_charity_vote"),
    path("<int:org_id>/charity-vote/close/", views.close_charity_vote_view, name="close_charity_vote"),
    # Breaking a tie is the captain's, not the manager's — see captains_call_view.
    path("<int:org_id>/charity-vote/captains-call/", views.captains_call_view, name="captains_call"),
    path("<int:org_id>/charity-vote/close-time/", views.election_close_time_view, name="election_close_time"),
    path("<int:org_id>/lock-fundraising/", views.lock_fundraising_view, name="lock_fundraising"),
    path("<int:org_id>/election/", views.election_setup_view, name="election_setup"),
    # Member <-> admin messages. The admin's end of the same threads is in
    # /manage/messages/.
    path("<int:org_id>/messages/", views.member_messages_view, name="member_messages"),
    # The three rooms. Each of these OPENS a conversation and redirects to it
    # rather than rendering one, so a room has exactly one address however you
    # arrived at it — which is what makes the thread URL below shareable, and
    # what stops "the organisation room" being a different page from the room
    # a notification links to.
    #
    # Ahead of the <int:thread_id> route, or "room" and "dm" would be tried as
    # thread ids first and 404.
    path("<int:org_id>/messages/room/", views.message_room_view, name="message_room"),
    path("<int:org_id>/messages/room/<int:group_id>/", views.message_group_room_view,
         name="message_group_room"),
    path("<int:org_id>/messages/dm/<int:user_id>/", views.message_direct_view,
         name="message_direct"),
    path("<int:org_id>/messages/<int:thread_id>/", views.member_message_thread_view,
         name="member_message_thread"),
    path("<int:org_id>/messages/<int:thread_id>/people/", views.message_people_view,
         name="message_people"),
    path("<int:org_id>/messages/<int:thread_id>/pin/", views.message_pin, name="message_pin"),
    path("<int:org_id>/messages/<int:thread_id>/react/<int:message_id>/",
         views.message_react, name="message_react"),
    # Attachments, served by a view rather than from /media/ so the same
    # can_read check the thread page makes is made again on every fetch. Not
    # org-scoped: the thread id already decides who may read it, and the org
    # would be a second thing to keep in step for no extra protection.
    path("messages/<int:thread_id>/file/<int:attachment_id>/", views.message_file,
         name="message_file"),
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
