"""FINAL WHISTLE — write one story per finished game, for every competition
GoodTip follows (tipping.match_reports.generate_match_stories). In the jobs
loop; safe to run again at any time.

    python manage.py generate_match_stories --days 3
"""
from django.core.management.base import BaseCommand

from tipping.match_reports import generate_match_stories


class Command(BaseCommand):
    help = "Write a Final Whistle story for each finished game without one."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=3)
        parser.add_argument("--series", nargs="*", help="only these series slugs")
        parser.add_argument("--no-fetch", action="store_true")
        parser.add_argument("--refresh-photos", action="store_true",
                            help="also re-pick the photos of stories already written in the window")

    def handle(self, *args, **opts):
        if opts["refresh_photos"]:
            from datetime import timedelta

            from django.utils import timezone

            from tipping.match_reports import write_story
            from tipping.models import MatchStory

            old = MatchStory.objects.filter(game__kickoff_at__gte=timezone.now() - timedelta(days=opts["days"])) \
                .select_related("game__series", "game__home_team", "game__away_team")
            if opts["series"]:
                old = old.filter(game__series__slug__in=opts["series"])
            n = 0
            for st in old.iterator():
                write_story(st.game, refresh_photos=True); n += 1
            self.stdout.write(f"{n} stories' photos re-picked.")
        made = generate_match_stories(opts["days"], fetch=not opts["no_fetch"], series=opts["series"])
        self.stdout.write(self.style.SUCCESS(f"{len(made)} Match Recap stories written."))
