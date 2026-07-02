from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.generic import TemplateView

from accounts.views import dashboard_view
from billing.views import stripe_webhook
from orgs.views import join_view


urlpatterns = [
    path("admin/", admin.site.urls),
    # Public marketing pages (no login required)
    path("", TemplateView.as_view(
        template_name="public/home.html",
        extra_context={"active": "home"},
    ), name="landing"),
    path("how-it-works/", TemplateView.as_view(
        template_name="public/how_it_works.html",
        extra_context={"active": "how"},
    ), name="how_it_works"),
    path("wall/", TemplateView.as_view(
        template_name="public/wall.html",
        extra_context={"active": "wall"},
    ), name="wall"),
    path("leaderboard/", TemplateView.as_view(
        template_name="public/leaderboard.html",
        extra_context={"active": "leaderboard"},
    ), name="good_list"),
    path("pricing/", TemplateView.as_view(
        template_name="public/pricing.html",
        extra_context={"active": "pricing"},
    ), name="pricing"),
    path("dashboard/", dashboard_view, name="dashboard"),
    path("", include("accounts.urls", namespace="accounts")),
    path("password-reset/", auth_views.PasswordResetView.as_view(
        template_name="auth/password_reset.html",
        email_template_name="auth/password_reset_email.txt",
        subject_template_name="auth/password_reset_subject.txt",
        success_url="/password-reset/done/",
    ), name="password_reset"),
    path("password-reset/done/", auth_views.PasswordResetDoneView.as_view(
        template_name="auth/password_reset_done.html"
    ), name="password_reset_done"),
    path("password-reset/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(
        template_name="auth/password_reset_confirm.html",
        success_url="/password-reset/complete/",
    ), name="password_reset_confirm"),
    path("password-reset/complete/", auth_views.PasswordResetCompleteView.as_view(
        template_name="auth/password_reset_complete.html"
    ), name="password_reset_complete"),
    path("profile/", include("accounts.profile_urls")),
    path("join/<int:org_id>/<str:token>/", join_view, name="join_org"),
    path("leagues/", include("orgs.urls", namespace="orgs")),
    path("billing/", include("billing.urls", namespace="billing")),
    path("stripe/webhook/", stripe_webhook, name="stripe_webhook"),
    path("org/", include("tipping.urls", namespace="tipping")),
    path("manage/", include("admin_panel.urls", namespace="manage")),
]
