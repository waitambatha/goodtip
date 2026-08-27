from django.urls import path

from . import views


app_name = "accounts"

urlpatterns = [
    path("signup/", views.signup_view, name="signup"),
    path("signup/photo/", views.welcome_photo_view, name="welcome_photo"),
    path("login/", views.login_view, name="login"),
    path("verify/", views.verify_view, name="verify"),
    path("logout/", views.logout_view, name="logout"),
    path("htmx/countdown/<int:org_id>/", views.dashboard_countdown_partial, name="dashboard_countdown"),
    # Put the first-visit walkthrough away. POST-only: it changes state.
    path("onboarding/seen/", views.onboarding_seen, name="onboarding_seen"),
]
