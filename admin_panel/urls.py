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
    # Pages — the words on the public and members-only pages, edited in place.
    path("pages/", views.pages_list, name="pages"),
    path("pages/save/", views.page_save, name="page_save"),
    path("pages/upload-image/", views.page_upload_image, name="page_upload_image"),
    path("pages/<str:page_key>/", views.page_edits, name="page_edits"),
    path("pages/<str:page_key>/revert/", views.page_revert, name="page_revert"),
    path("news/", views.news_list, name="news"),
    path("news/new/", views.news_new, name="news_new"),
    path("news/upload-image/", views.news_upload_image, name="news_upload_image"),
    path("news/<int:post_id>/edit/", views.news_edit, name="news_edit"),
    path("news/<int:post_id>/toggle/", views.news_toggle, name="news_toggle"),
    path("news/<int:post_id>/announce/", views.news_announce, name="news_announce"),
    path("news/<int:post_id>/delete/", views.news_delete, name="news_delete"),
]
