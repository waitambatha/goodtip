from django.urls import path

from . import views


app_name = "tipping"

urlpatterns = [
    path("<int:org_id>/tip/<int:round_id>/", views.tip_round_view, name="tip_round"),
    path("<int:org_id>/tip/<int:round_id>/save/<int:match_id>/", views.tip_save_partial, name="tip_save"),
    path("<int:org_id>/tips/", views.my_tips_view, name="my_tips"),
    path("<int:org_id>/leaderboard/", views.leaderboard_view, name="leaderboard"),
]
