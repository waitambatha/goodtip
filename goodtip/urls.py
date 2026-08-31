from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.generic import TemplateView
from django.views.static import serve as static_serve

from accounts.forms import RegisteredEmailPasswordResetForm
from accounts.views import (
    boss_progress_view, coming_soon_view, contact_submit_view, dashboard_view,
    tell_the_boss_view,
)
from admin_panel.views import news_detail, news_index
from billing.views import good_list_view, stripe_webhook
from goodtip.staging_gate import gate_view, robots_view
from orgs.views import join_view, public_wall_reply, public_wall_view
from sysadmin.invite_views import accept as admin_invite_accept


urlpatterns = [
    path("gate/", gate_view, name="staging_gate"),
    # BEFORE admin.site.urls — the admin catches everything under its prefix,
    # so a gate mounted after it would never be reached.
    path("admin/", include("sysadmin.urls", namespace="sysadmin")),
    path("admin/", admin.site.urls),
    # Accepting an administrator invitation. Outside /admin/ on purpose: the
    # person following this link has no password yet and no session, so every
    # guard in there would bounce them to a login they cannot pass.
    path("admin-invite/<str:token>/", admin_invite_accept, name="admin_invite_accept"),
    # Public marketing pages (no login required)
    path("", TemplateView.as_view(
        template_name="public/home.html",
        extra_context={"active": "home"},
    ), name="landing"),
    path("how-it-works/", TemplateView.as_view(
        template_name="public/how_it_works.html",
        extra_context={"active": "how"},
    ), name="how_it_works"),
    # The Wall — live cross-group feed of posts members chose to share.
    path("wall/", public_wall_view, name="wall"),
    path("wall/<int:post_id>/reply/", public_wall_reply, name="public_wall_reply"),
    # The Good List — live, privacy-gated data (no placeholder figures).
    path("leaderboard/", good_list_view, name="good_list"),
    # About and Privacy are ordinary TemplateViews like their neighbours —
    # every word on them is a CMS slot, so the client edits the copy from
    # Manage → Public pages and the view never has to know. See
    # admin_panel.templatetags.pagecms.
    path("about/", TemplateView.as_view(
        template_name="public/about.html",
        extra_context={"active": "about"},
    ), name="about"),
    path("privacy/", TemplateView.as_view(
        template_name="public/privacy.html",
        extra_context={"active": "privacy"},
    ), name="privacy"),
    path("pricing/", TemplateView.as_view(
        template_name="public/pricing.html",
        extra_context={"active": "pricing"},
    ), name="pricing"),
    path("coming-soon/", coming_soon_view, name="coming_soon"),
    path("contact/", contact_submit_view, name="contact_submit"),
    path("tell-the-boss/", tell_the_boss_view, name="tell_the_boss"),
    path("tell-the-boss/progress/", boss_progress_view, name="boss_progress"),
    path("dashboard/", dashboard_view, name="dashboard"),
    # News & blog (members) — full-story pages behind the dashboard cards
    path("news/", news_index, name="news_index"),
    path("news/<slug:slug>/", news_detail, name="news_detail"),
    path("", include("accounts.urls", namespace="accounts")),
    path("password-reset/", auth_views.PasswordResetView.as_view(
        template_name="auth/password_reset.html",
        # Tells the member when the address isn't registered instead of
        # accepting anything silently — see the form for the trade-off.
        form_class=RegisteredEmailPasswordResetForm,
        email_template_name="auth/password_reset_email.txt",
        # Branded HTML alongside the text part, so the reset email matches
        # every other message rather than arriving as bare text.
        html_email_template_name="auth/password_reset_email.html",
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
    # User uploads (profile photos). Served by Django regardless of DEBUG —
    # fine at avatar scale; move behind nginx/S3 if uploads ever grow.
    path("media/<path:path>", static_serve, {"document_root": settings.MEDIA_ROOT}, name="media"),
]


# Staging only. Production keeps whatever its own nginx serves at /robots.txt;
# nothing here should change what a crawler is told about the live site.
if getattr(settings, "IS_STAGING", False):
    urlpatterns += [path("robots.txt", robots_view, name="robots")]
