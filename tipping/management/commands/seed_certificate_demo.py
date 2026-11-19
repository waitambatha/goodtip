"""Give every competition an organisation can tip a finished season with three
placegetters — LOCAL TESTING ONLY (client, 17 Nov 2026: "for this masterclass
just to see how it looks, let's have seeded data so each sport or competition
can have winners").

    manage.py seed_certificate_demo --org 57          # switch on every competition, seed winners
    manage.py seed_certificate_demo --org 57 --undo   # remove everything it added

What it adds is written to .cache/cert-demo-<org>.json so --undo removes exactly
that and nothing else. Refuses to run unless DATABASE_URL points at this
machine — it must never touch staging or live.
"""
import json
import os
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

DEMO_BASE = 9000


class Command(BaseCommand):
    help = "LOCAL ONLY: seed a finished season with three winners in every competition of an organisation."

    def add_arguments(self, parser):
        parser.add_argument("--org", type=int, required=True)
        parser.add_argument("--undo", action="store_true")

    def handle(self, *args, **o):
        host = settings.DATABASES["default"].get("HOST", "")
        if host not in ("127.0.0.1", "localhost", ""):
            raise CommandError(f"Refusing: the database is on {host!r}, not this machine.")
        from orgs.models import OrgMember, Organisation

        org = Organisation.objects.get(pk=o["org"])
        marker = Path(settings.BASE_DIR) / ".cache" / f"cert-demo-{org.pk}.json"
        if o["undo"]:
            return self.undo(org, marker)
        if marker.exists():
            raise CommandError(f"Already seeded ({marker}). Run with --undo first.")

        from catalog.models import Competition
        from orgs.forms import fed_competitions

        from tipping.certificates import competitions_for, season_state
        from tipping.models import Match, Round, Team, Tip

        added_comps = []
        for comp in fed_competitions():
            if comp.season_id == org.season_id and not org.competitions.filter(pk=comp.pk).exists():
                org.competitions.add(comp)
                added_comps.append(comp.pk)

        members = [m.user for m in OrgMember.objects.filter(org=org).select_related("user")[:6]]
        if len(members) < 3:
            raise CommandError("This organisation needs at least three members to have a top three.")
        now = timezone.now()
        rounds, made = [], []
        for series in competitions_for(org):
            if season_state(org, series).final:
                continue
            if Round.objects.filter(org=org, series=series).exclude(round_number__gte=DEMO_BASE).exists():
                self.stdout.write(f"  {series.name}: has a real season in progress; left alone")
                continue
            comp = Competition.objects.filter(series=series, season=org.season).first()
            teams = list(Team.objects.filter(series=series)[:2])
            if comp is None or len(teams) < 2:
                self.stdout.write(f"  {series.name}: no competition or teams to build with; skipped")
                continue
            home, away = teams
            for n, stage, days in ((DEMO_BASE + 1, Round.STAGE_REGULAR, 9), (DEMO_BASE + 2, Round.STAGE_FINALS, 4)):
                r = Round.objects.create(org=org, round_number=n, competition=comp, series=series, stage=stage,
                                         lockout_at=now - timedelta(days=days), status="complete")
                rounds.append(r.pk)
                m = Match.objects.create(round=r, home_team=home, away_team=away,
                                         kickoff_at=now - timedelta(days=days), result="home",
                                         home_score=24, away_score=12, status=Match.STATUS_COMPLETE)
                # A different order per competition, so each has its own podium.
                order = members[made.__len__() % len(members):] + members[:made.__len__() % len(members)]
                for i, u in enumerate(order):
                    right = i < 3 or n % 2 == 0
                    Tip.objects.create(user=u, match=m, org=org, selection="home" if right else "away",
                                       is_correct=right, points_awarded=(6 - i) if right else 0)
            made.append(series.name)
            self.stdout.write(f"  {series.name}: finished season, winners seeded")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"competitions": added_comps, "rounds": rounds}))
        self.stdout.write(self.style.SUCCESS(
            f"{org.name}: {len(added_comps)} competitions switched on, {len(made)} seasons seeded. Undo: --undo"))

    def undo(self, org, marker):
        from tipping.models import Round

        if not marker.exists():
            raise CommandError("Nothing recorded to undo for this organisation.")
        data = json.loads(marker.read_text())
        n = Round.objects.filter(pk__in=data["rounds"], org=org).delete()[0]
        org.competitions.remove(*data["competitions"])
        marker.unlink()
        self.stdout.write(self.style.SUCCESS(f"Removed {len(data['rounds'])} demo rounds ({n} rows) and {len(data['competitions'])} competitions."))
