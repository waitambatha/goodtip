from django.urls import path

from . import views


app_name = "tipping"

urlpatterns = [
    path("<int:org_id>/tip/<int:round_id>/", views.tip_round_view, name="tip_round"),
    path("<int:org_id>/tip/<int:round_id>/save/<int:match_id>/", views.tip_save_partial, name="tip_save"),
    path("<int:org_id>/tip/<int:round_id>/confirm/", views.tip_round_confirm, name="tip_confirm"),
    # Cross-round confirm for the dashboard's "everything still to play" slate.
    path("<int:org_id>/tip/confirm/", views.tip_confirm_upcoming, name="tip_confirm_upcoming"),
    path("<int:org_id>/tips/", views.my_tips_view, name="my_tips"),
    # In-play score/clock for one fixture. Not org-scoped: the fragment is
    # identical whoever is looking, and it carries no tip.
    path("match/<int:match_id>/state/", views.match_state_partial, name="match_state"),
    path("<int:org_id>/leaderboard/", views.leaderboard_view, name="leaderboard"),
    # The competition ladder (where the teams sit) — distinct from the
    # leaderboard above, which ranks tippers.
    path("<int:org_id>/ladder/", views.ladder_view, name="ladder"),
]
