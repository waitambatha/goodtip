from django.urls import path

from . import views


app_name = "manage"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("approvals/", views.approvals, name="approvals"),
    path("orgs/", views.orgs_list, name="orgs_list"),
    path("org/<int:org_id>/rounds/", views.org_rounds, name="org_rounds"),
    path("org/<int:org_id>/round/<int:round_id>/matches/", views.round_matches, name="round_matches"),
    path("org/<int:org_id>/members/", views.org_members, name="org_members"),
    path("sync/", views.sync_panel, name="sync"),
    path("enquiries/", views.enquiries, name="enquiries"),
    path("enquiries/<int:enquiry_id>/", views.enquiry_detail, name="enquiry_detail"),
    path("news/", views.news_list, name="news"),
    path("news/new/", views.news_new, name="news_new"),
    path("news/upload-image/", views.news_upload_image, name="news_upload_image"),
    path("news/<int:post_id>/edit/", views.news_edit, name="news_edit"),
    path("news/<int:post_id>/toggle/", views.news_toggle, name="news_toggle"),
    path("news/<int:post_id>/announce/", views.news_announce, name="news_announce"),
    path("news/<int:post_id>/delete/", views.news_delete, name="news_delete"),
]
