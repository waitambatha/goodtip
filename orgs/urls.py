from django.urls import path

from . import views


app_name = "orgs"

urlpatterns = [
    path("new/", views.create_org_view, name="create"),
    path("<int:org_id>/created/", views.org_created_view, name="created"),
    path("<int:org_id>/invite/", views.org_invite_view, name="invite"),
    path("<int:org_id>/members/", views.members_view, name="members"),
    path("<int:org_id>/charity-vote/", views.charity_vote_view, name="charity_vote"),
    path("<int:org_id>/charity-vote/cast/", views.cast_charity_vote, name="cast_charity_vote"),
    path("<int:org_id>/charity-vote/close/", views.close_charity_vote_view, name="close_charity_vote"),
]
