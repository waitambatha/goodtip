from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.generic import TemplateView

from accounts.views import dashboard_view
from billing.views import stripe_webhook
from orgs.views import join_view


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", TemplateView.as_view(template_name="landing.html"), name="landing"),
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
