from django.urls import path

from . import views


app_name = "tipping"

urlpatterns = [
    path("<int:org_id>/tip/<int:round_id>/", views.tip_round_view, name="tip_round"),
    path("<int:org_id>/tip/<int:round_id>/save/<int:match_id>/", views.tip_save_partial, name="tip_save"),
    path("<int:org_id>/tip/<int:round_id>/confirm/", views.tip_round_confirm, name="tip_confirm"),
    # Cross-round confirm for the dashboard's "everything still to play" slate.
    path("<int:org_id>/tip/confirm/", views.tip_confirm_upcoming, name="tip_confirm_upcoming"),
    # Review what carrying this slate into your other groups would do, then
    # do it. Only ever reached when there IS somewhere else to carry to.
    path("<int:org_id>/tip/carry/", views.tip_carry_view, name="tip_carry"),
    # The same question asked BEFORE the save, as a fragment for the confirm
    # sheet. 204 when there is nothing to carry, so the sheet skips the step.
    path("<int:org_id>/tip/carry/preview/", views.carry_preview, name="carry_preview"),
    path("<int:org_id>/tips/", views.my_tips_view, name="my_tips"),
    # In-play score/clock for one fixture. Not org-scoped: the fragment is
    # identical whoever is looking, and it carries no tip.
    path("match/<int:match_id>/state/", views.match_state_partial, name="match_state"),
    path("<int:org_id>/leaderboard/", views.leaderboard_view, name="leaderboard"),
    # The competition ladder (where the teams sit) — distinct from the
    # leaderboard above, which ranks tippers.
    path("<int:org_id>/ladder/", views.ladder_view, name="ladder"),
    # The detail behind each board: a member's own season, and a club's.
    path("<int:org_id>/leaderboard/me/", views.my_stats_view, name="my_stats"),
    path("<int:org_id>/ladder/team/<int:team_id>/", views.team_stats_view, name="team_stats"),
]
