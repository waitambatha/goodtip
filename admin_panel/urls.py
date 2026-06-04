from django.urls import path

from . import views


app_name = "manage"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("orgs/", views.orgs_list, name="orgs_list"),
    path("org/<int:org_id>/rounds/", views.org_rounds, name="org_rounds"),
    path("org/<int:org_id>/round/<int:round_id>/matches/", views.round_matches, name="round_matches"),
    path("org/<int:org_id>/members/", views.org_members, name="org_members"),
    path("sync/", views.sync_panel, name="sync"),
]
